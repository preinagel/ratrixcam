"""Run-metadata automation (GitHub #34): the ratrixcam side (terminal log, config copy,
shutdown collection) tested without cameras, and the videoproc side (transfer, verification,
deletion of ratrixcam_run_metadata folders) driven through the CLIs."""

import csv
import json
import re
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from helpers import (
    VIDEOPROC_DIR,
    dest_path, fname, latest_run_dir, place, place_clip, read_rows, run_compress_drive, run_confirm_and_delete, source_path,
)

MOVED_TO_WRAPPER = "carrying ratrixcam run-metadata folders moves to process_ratrixcam_output (part B)"

REPO = VIDEOPROC_DIR.parent
sys.path.insert(0, str(REPO))
from ratrixcam_naming import NOTES_FILENAME, RUN_METADATA_DIRNAME, STILLS_DIRNAME, manifest_warning, manifests_dir  # noqa: E402
import ratrixcam_run_metadata  # noqa: E402
from ratrixcam_run_metadata import RunMetadata  # noqa: E402
from ratrixcam_utils import Config, stills_dir  # noqa: E402


def make_config(tmp_path: Path) -> tuple[Config, Path]:
    temp, save, stills = tmp_path / "temp", tmp_path / "save", tmp_path / "temp" / "status"
    for d in (temp, save, stills):
        d.mkdir(parents=True, exist_ok=True)
    data = {
        "study_label": "Test", "cameras": [{"subjID": "001", "view": "cam1", "position": "A"}],
        "default_fps": 30, "default_width": 640, "default_height": 480, "default_cam_exposure": -8.0,
        "time_slice": 60, "preview_interval": 1, "save_path": str(save), "temp_path": str(temp),
    }
    config_path = tmp_path / "camera_config_in_use.json"
    config_path.write_text(json.dumps(data, indent=2))
    return Config.model_validate(data), config_path


# ----------------------------------------------------------------- ratrixcam side

def test_terminal_log_captures_parent_and_spawned_child_output(tmp_path):
    """Run a tiny program in a subprocess so the fd juggling cannot disturb pytest itself."""
    config, config_path = make_config(tmp_path)
    program = textwrap.dedent(f"""
        import multiprocessing, sys
        sys.path.insert(0, {str(REPO)!r})
        from ratrixcam_run_metadata import RunMetadata
        from ratrixcam_utils import load_settings

        def child():
            print("CHILD says hello")

        if __name__ == "__main__":
            multiprocessing.set_start_method("spawn")
            config = load_settings({str(config_path)!r})
            run = RunMetadata(config, {str(config_path)!r})
            run.start_terminal_log()
            print("PARENT before child")
            p = multiprocessing.Process(target=child); p.start(); p.join()
            print("PARENT after child")
            run.terminal_log.stop()
            print("AFTER STOP: terminal only")
            print(run.log_path)
    """)
    script = tmp_path / "prog.py"
    script.write_text(program)
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    log_path = Path(result.stdout.strip().splitlines()[-1])
    log = log_path.read_text()
    for line in ("code version", "PARENT before child", "CHILD says hello", "PARENT after child"):
        assert line in log, f"missing from log: {line!r}\n{log}"
    assert "AFTER STOP" not in log
    assert "AFTER STOP: terminal only" in result.stdout  # terminal restored after stop
    assert log_path.parent == Path(config.temp_path)


def test_record_start_copies_config_and_writes_notes_template(tmp_path):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path, started=datetime(2026, 1, 2, 3, 4, 5))
    assert re.fullmatch(r"ratrixcam_20260102_030405_[0-9a-f]{8}", run.run_id)
    run.record_start()
    assert run.run_dir == Path(config.save_path) / RUN_METADATA_DIRNAME / run.run_id
    assert json.loads((run.run_dir / Path(config_path).name).read_text())["study_label"] == "Test"
    assert (run.run_dir / NOTES_FILENAME).read_text().startswith("Notes for this recording run")


def test_collect_at_shutdown_gathers_loose_temp_files_and_stills(tmp_path):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path)
    run.record_start()
    temp = Path(config.temp_path)
    (Path(stills_dir(config.temp_path)) / "cam_cam1_status.png").write_bytes(b"png")
    (temp / "Screenshot 2026-01-02.png").write_bytes(b"shot")
    (temp / "manifests").mkdir()
    (temp / "manifests" / "subj001_cam1_manifest.csv").write_text("path,subjID,view,clip_start,row_written,crf\n")
    (temp / ".DS_Store").write_bytes(b"")
    run.log_path.write_text("fake terminal log\n")
    empty_cam_dir = temp / "Test_cam1"
    empty_cam_dir.mkdir()
    busy_cam_dir = temp / "Test_cam2"
    busy_cam_dir.mkdir()
    (busy_cam_dir / "cam2_20260102_03-04-05.mp4").write_bytes(b"untransferred video")

    run.collect_at_shutdown()
    run.collect_at_shutdown()  # idempotent

    assert (run.run_dir / STILLS_DIRNAME / "cam_cam1_status.png").read_bytes() == b"png"
    assert (run.run_dir / "Screenshot 2026-01-02.png").exists()
    assert (run.run_dir / run.log_path.name).read_text() == "fake terminal log\n"
    assert manifests_dir(config.save_path, run.run_id) == run.run_dir / "manifests"
    assert (run.run_dir / "manifests" / "subj001_cam1_manifest.csv").read_text().startswith("path,")
    assert not (temp / "manifests").exists()  # emptied and removed with the rest of temp
    assert not (temp / "Screenshot 2026-01-02.png").exists()
    assert not (Path(stills_dir(config.temp_path)) / "cam_cam1_status.png").exists()  # moved, not copied
    assert not Path(stills_dir(config.temp_path)).exists()  # emptied stills folder removed
    assert not empty_cam_dir.exists()
    assert (busy_cam_dir / "cam2_20260102_03-04-05.mp4").exists()  # never touched
    assert temp.exists()  # kept because a camera folder still holds a file


def test_collect_at_shutdown_adds_rows_the_cameras_could_not_write(tmp_path, capsys):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path)
    run.record_start()
    temp = Path(config.temp_path)
    (temp / "manifests").mkdir()
    (temp / "manifests" / "subj001_cam1_manifest.csv").write_text(
        "path,subjID,view,clip_start,row_written,crf\nTest_subj001_cam1_20260102/a.mp4,001,cam1,2026-01-02 03:04:05.000,2026-01-02 03:05:05.000,25\n"
    )
    run.log_path.write_text(
        "some output\n"
        "WARNING: could not add b.mp4 to the manifest after 3 attempts: boom\n"
        + manifest_warning("subj001_cam1_manifest.csv", "Test_subj001_cam1_20260102/b.mp4", "001", "cam1", "2026-01-02 03:05:05.000", 25) + "\n"
        + manifest_warning("subj001_cam1_manifest.csv", "Test_subj001_cam1_20260102/c.mp4", "001", "cam1", "2026-01-02 03:06:05.000", 25) + "\n"
        + manifest_warning("subj002_cam2_manifest.csv", "Test_subj002_cam2_20260102/z.mp4", "002", "cam2", "2026-01-02 03:04:05.000", 30) + "\n"
        "WARNING: temp folder 'x' still contains files\n"
    )

    run.collect_at_shutdown()

    folder = manifests_dir(config.save_path, run.run_id)
    rows = list(csv.DictReader(open(folder / "subj001_cam1_manifest.csv", newline="")))
    assert [r["path"].rsplit("/", 1)[1] for r in rows] == ["a.mp4", "b.mp4", "c.mp4"]
    assert rows[1]["clip_start"] == "2026-01-02 03:05:05.000" and rows[1]["crf"] == "25"
    assert rows[1]["row_written"] > rows[0]["row_written"]  # written at cleanup, later than the camera's row
    rows2 = list(csv.DictReader(open(folder / "subj002_cam2_manifest.csv", newline="")))  # created at cleanup
    assert len(rows2) == 1 and rows2[0]["view"] == "cam2" and rows2[0]["crf"] == "30"
    assert "Manifest rows added at cleanup: 3" in capsys.readouterr().out
    assert not temp.exists()


def test_start_terminal_log_fails_after_retries_when_the_log_cannot_be_created(tmp_path, monkeypatch, capsys):
    config, config_path = make_config(tmp_path)
    monkeypatch.setattr(ratrixcam_run_metadata, "LOG_START_RETRY_DELAY", 0)
    run = RunMetadata(config, config_path)
    run.log_path.mkdir()  # a folder where the log file should go: every open fails
    assert run.start_terminal_log() is False
    assert run.terminal_log is None
    assert "ERROR" in capsys.readouterr().out
    run.log_path.rmdir()
    assert run.start_terminal_log() is True
    assert run.terminal_log is not None
    run.terminal_log.stop()


def test_collect_at_shutdown_removes_an_emptied_temp_folder(tmp_path):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path)
    run.record_start()
    temp = Path(config.temp_path)
    (Path(stills_dir(config.temp_path)) / "cam_cam1_status.png").write_bytes(b"png")
    (temp / ".DS_Store").write_bytes(b"")
    run.log_path.write_text("fake terminal log\n")
    (temp / "Test_cam1").mkdir()

    run.collect_at_shutdown()

    assert (run.run_dir / STILLS_DIRNAME / "cam_cam1_status.png").exists()
    assert (run.run_dir / run.log_path.name).read_text() == "fake terminal log\n"
    assert not temp.exists()


# ----------------------------------------------------------------- videoproc side

@pytest.fixture
def source_with_run_metadata(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place(clips["R30"], source_path(source_root, "10-00-00"))
    run_dir = source_root / RUN_METADATA_DIRNAME / "20260102_030405_Test_Rack"
    (run_dir / STILLS_DIRNAME).mkdir(parents=True)
    (run_dir / "camera_config.json").write_text('{"rack_name": "Test Rack"}')
    (run_dir / NOTES_FILENAME).write_text("notes\n")
    (run_dir / "terminal_20260102_030405_Test_Rack.log").write_text("log\n")
    (run_dir / STILLS_DIRNAME / "cam_cam1_status.png").write_bytes(b"png")
    return source_root, dest_root, run_dir


@pytest.mark.skip(reason=MOVED_TO_WRAPPER)
def test_confirm_and_delete_verifies_blocks_and_finally_deletes_run_metadata(source_with_run_metadata):
    source_root, dest_root, run_dir = source_with_run_metadata
    assert run_compress_drive(source_root, dest_root, extra_args=["--n_threads", "4"]).returncode == 0
    dest_run = dest_root / RUN_METADATA_DIRNAME / run_dir.name

    # a tampered archive copy blocks deletion
    (dest_run / NOTES_FILENAME).write_text("tampered, different size\n")
    blocked = run_confirm_and_delete(source_root, dest_root, extra_args=["--min_age_hours", "0"], delete=True)
    assert "1 NOT verified (block deletion)" in blocked.stdout
    assert "REFUSING to delete" in blocked.stdout
    assert run_dir.exists() and source_path(source_root, "10-00-00").exists()

    # restored archive copy: verified, and deleted together with the sources
    (dest_run / NOTES_FILENAME).write_text("notes\n")
    done = run_confirm_and_delete(source_root, dest_root, extra_args=["--min_age_hours", "0", "--no-decode"], delete=True)
    assert done.returncode == 0, done.stderr
    assert "ratrixcam run-metadata folders: 1 verified at the archive" in done.stdout
    assert "and 1 run-metadata folder(s)" in done.stdout
    assert not run_dir.exists()
    assert not (source_root / RUN_METADATA_DIRNAME).exists()
    assert not source_root.exists()  # nothing left: whole source folder removed
    rows = read_rows(latest_run_dir(dest_root, "confirm_") / "deletion_log.csv")
    assert len(rows) == 2 and rows[1]["input_path"].endswith(run_dir.name)
    assert dest_run.exists()  # archive copy untouched
