"""Run-metadata automation (GitHub #34): the ratrixcam side (terminal log, config copy,
shutdown collection) tested without cameras, and the videoproc side (transfer, verification,
deletion of ratrixcam_run_metadata folders) driven through the CLIs."""

import csv
import json
import re
import shutil
import subprocess
import sys
import textwrap
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import pytest

from helpers import VIDEOPROC_DIR

REPO = VIDEOPROC_DIR.parent
sys.path.insert(0, str(REPO))
from ratrixcam_manifest import write_row  # noqa: E402
from ratrixcam_naming import NOTES_FILENAME, RUN_METADATA_DIRNAME, STILLS_DIRNAME, manifest_warning, manifests_dir, temp_manifests_dir  # noqa: E402
import ratrixcam_run_metadata  # noqa: E402
import ratrixcam_server  # noqa: E402
from ratrixcam_run_metadata import STOP_SPACE_FACTOR, RunMetadata, save_drive_problem  # noqa: E402
from ratrixcam_utils import Config, not_writable, stills_dir, temp_is_clear  # noqa: E402


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
        import multiprocessing, os, sys
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
            tee_detached = os.getsid(run.terminal_log._tee.pid) != os.getsid(0)
            run.terminal_log.stop()
            print("AFTER STOP: terminal only")
            print("TEE DETACHED", tee_detached)
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
    assert "TEE DETACHED True" in result.stdout  # a terminal Ctrl-C must not reach the log writer
    assert log_path.parent == Path(config.temp_path)


def test_record_start_lays_down_the_run_folder_in_temp_and_a_provisional_copy(tmp_path):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path, started=datetime(2026, 1, 2, 3, 4, 5))
    assert re.fullmatch(r"ratrixcam_20260102_030405_[0-9a-f]{8}", run.run_id)
    run.record_start()
    assert run.temp_run_dir == Path(config.temp_path) / RUN_METADATA_DIRNAME
    assert run.run_dir == Path(config.save_path) / RUN_METADATA_DIRNAME / run.run_id
    for folder in (run.temp_run_dir, run.run_dir):
        assert json.loads((folder / Path(config_path).name).read_text())["study_label"] == "Test"
    assert (run.temp_run_dir / NOTES_FILENAME).read_text().startswith("Notes for this recording run")
    assert not (run.run_dir / NOTES_FILENAME).exists()  # one notes file only, in temp


def test_temp_is_clear(tmp_path):
    assert temp_is_clear(str(tmp_path / "absent"))
    (tmp_path / ".DS_Store").write_bytes(b"")
    assert temp_is_clear(str(tmp_path))
    (tmp_path / "leftover.txt").write_text("x")
    assert not temp_is_clear(str(tmp_path))


def test_not_writable_probes_with_a_file_it_removes_again(tmp_path, capsys):
    assert not_writable(str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().out == ""


def test_not_writable_names_the_reason(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("builtins.open", refuse)
    assert not_writable(str(tmp_path)) == "Read-only file system"


def test_not_writable_tolerates_a_probe_it_cannot_remove(tmp_path, monkeypatch, capsys):
    def no_remove(_p):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr("os.remove", no_remove)
    assert not_writable(str(tmp_path)) is None
    assert "WARNING: could not remove the write-test file" in capsys.readouterr().out


def test_collect_at_shutdown_moves_the_run_folder_with_stills_and_loose_files(tmp_path):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path)
    run.record_start()
    temp = Path(config.temp_path)
    (run.temp_run_dir / NOTES_FILENAME).write_text("notes edited during the run\n")
    (Path(stills_dir(config.temp_path)) / "cam_cam1_status.png").write_bytes(b"png")
    (temp / "Screenshot 2026-01-02.png").write_bytes(b"shot")
    (run.temp_run_dir / "manifests").mkdir()
    (run.temp_run_dir / "manifests" / "subj001_cam1_manifest.csv").write_text("path,subjID,view,clip_start,row_written,crf\n")
    (temp / ".DS_Store").write_bytes(b"")
    run.log_path.write_text("fake terminal log\n")
    empty_cam_dir = temp / "Test_cam1"
    empty_cam_dir.mkdir()
    busy_cam_dir = temp / "Test_cam2"
    busy_cam_dir.mkdir()
    (busy_cam_dir / "cam2_20260102_03-04-05.mp4").write_bytes(b"not a video, and no manifest row")  # a clip cut off by a kill

    run.collect_at_shutdown()
    run.collect_at_shutdown()  # idempotent

    assert (run.run_dir / STILLS_DIRNAME / "cam_cam1_status.png").read_bytes() == b"png"
    assert (run.run_dir / "Screenshot 2026-01-02.png").exists()
    assert (run.run_dir / run.log_path.name).read_text() == "fake terminal log\n"
    assert (run.run_dir / NOTES_FILENAME).read_text() == "notes edited during the run\n"
    assert (run.run_dir / Path(config_path).name).exists()
    assert manifests_dir(config.save_path, run.run_id) == run.run_dir / "manifests"
    assert (run.run_dir / "manifests" / "subj001_cam1_manifest.csv").read_text().startswith("path,")
    assert not run.temp_run_dir.exists()  # moved, not copied
    assert not (temp / "Screenshot 2026-01-02.png").exists()
    assert not Path(stills_dir(config.temp_path)).exists()  # emptied stills folder removed
    assert not run.log_path.exists()
    assert not empty_cam_dir.exists()
    assert not busy_cam_dir.exists()  # its rowless, unopenable clip was deleted, then the emptied folder
    assert not temp.exists()


def test_collect_at_shutdown_adds_rows_the_cameras_could_not_write(tmp_path, capsys):
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path)
    run.record_start()
    temp = Path(config.temp_path)
    (run.temp_run_dir / "manifests").mkdir()
    (run.temp_run_dir / "manifests" / "subj001_cam1_manifest.csv").write_text(
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


def test_shorten_path_replaces_the_roots():
    from ratrixcam_naming import shorten_path

    roots = [("/t/x/", "<temp>"), ("/v/s", "<save>")]
    assert shorten_path("/t/x/status/a.png", roots) == "<temp>/status/a.png"
    assert shorten_path("/v/s/ratrixcam_run_metadata/r1", roots) == "<save>/ratrixcam_run_metadata/r1"
    assert shorten_path("/v/s", roots) == "<save>"
    assert shorten_path(Path("/t/x"), roots) == "<temp>"
    assert shorten_path("/v/stuff/a", roots) == "/v/stuff/a"  # a sibling, not under the root


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


def start_run_with_a_still(tmp_path):
    """A started run whose temp holds one still, one loose screenshot and the terminal log."""
    config, config_path = make_config(tmp_path)
    run = RunMetadata(config, config_path)
    run.record_start()
    temp = Path(config.temp_path)
    (Path(stills_dir(config.temp_path)) / "cam_cam1_status.png").write_bytes(b"png")
    (temp / "Screenshot.png").write_bytes(b"shot")
    run.log_path.write_text("fake terminal log\n")
    return run, temp


def test_abort_adds_manifest_rows_and_touches_nothing_else(tmp_path, capsys):
    run, temp = start_run_with_a_still(tmp_path)
    run.log_path.write_text(
        manifest_warning("subj001_cam1_manifest.csv", "Test_subj001_cam1_20260102/b.mp4", "001", "cam1", "2026-01-02 03:05:05.000", 25) + "\n"
    )

    run.collect_at_shutdown(abort=True)

    assert (Path(stills_dir(run.config.temp_path)) / "cam_cam1_status.png").read_bytes() == b"png"  # where the camera left it
    assert (temp / "Screenshot.png").exists()
    assert run.log_path.exists()  # stays in temp
    assert sorted(p.name for p in run.temp_run_dir.iterdir()) == sorted(["manifests", NOTES_FILENAME, Path(run.config_path).name])
    assert (temp_manifests_dir(run.config.temp_path) / "subj001_cam1_manifest.csv").read_text().count("\n") == 2  # header + the row
    assert sorted(p.name for p in run.run_dir.iterdir()) == [Path(run.config_path).name]  # provisional copy untouched
    out = capsys.readouterr().out
    assert "Run aborted: nothing is moved to the save drive; everything stays in <temp>" in out
    assert "Manifest rows added at cleanup: 1" in out
    assert "WARNING" not in out and "Gathered" not in out


def test_metadata_folder_stays_when_the_save_folder_is_gone(tmp_path, capsys):
    run, temp = start_run_with_a_still(tmp_path)
    shutil.rmtree(run.config.save_path)  # the drive was pulled

    run.collect_at_shutdown()

    assert (run.temp_run_dir / STILLS_DIRNAME / "cam_cam1_status.png").exists()
    assert run.log_path.exists()
    assert not Path(run.config.save_path).exists()  # nothing rebuilt on the system disk
    out = capsys.readouterr().out
    assert "WARNING: run metadata folder not moved to <save>/ratrixcam_run_metadata/" in out
    assert ": save folder not found; left in <temp>/ratrixcam_run_metadata" in out


def test_metadata_folder_is_not_copied_onto_a_drive_short_of_space(tmp_path, monkeypatch, capsys):
    run, _ = start_run_with_a_still(tmp_path)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**9, 10**9 - 10, 10))

    def no_copy(*_a, **_k):
        raise AssertionError("copytree must not be attempted without room")

    monkeypatch.setattr(shutil, "copytree", no_copy)

    run.collect_at_shutdown()

    assert (run.temp_run_dir / "Screenshot.png").exists()
    out = capsys.readouterr().out
    assert "not moved to <save>/ratrixcam_run_metadata/" in out
    assert ": save drive short of space (0 MB needed, 0 MB free); left in <temp>/" in out


def test_a_failed_copy_is_reported_without_the_raw_error_list(tmp_path, monkeypatch, capsys):
    run, _ = start_run_with_a_still(tmp_path)

    def partial(src, dst, **_k):
        raise shutil.Error([
            (f"{src}/a.png", f"{dst}/a.png", f"[Errno 28] No space left on device: '{src}/a.png' -> '{dst}/a.png'"),
            (f"{src}/b.png", f"{dst}/b.png", f"[Errno 28] No space left on device: '{src}/b.png' -> '{dst}/b.png'"),
        ])

    monkeypatch.setattr(shutil, "copytree", partial)

    run.collect_at_shutdown()

    out = capsys.readouterr().out
    assert ": copy failed: No space left on device (2 file(s) could not be copied); left in <temp>/" in out
    assert "a.png" not in out and "Errno" not in out and "[(" not in out
    assert run.log_path.exists()  # the log stays with the folder


def test_a_failed_gathering_skips_the_move(tmp_path, monkeypatch, capsys):
    run, temp = start_run_with_a_still(tmp_path)

    def cannot_move(src, dst):
        raise OSError(5, "Input/output error")

    def no_copy(*_a, **_k):
        raise AssertionError("copytree must not be attempted after a failed gathering")

    monkeypatch.setattr(shutil, "move", cannot_move)
    monkeypatch.setattr(shutil, "copytree", no_copy)

    run.collect_at_shutdown()

    out = capsys.readouterr().out
    assert "WARNING: gathering stills and loose temp files into the run metadata folder failed after 0 file(s): Input/output error" in out
    assert "WARNING: run metadata folder not moved because the gathering failed; left in <temp>/" in out
    assert (temp / "Screenshot.png").exists()
    assert run.log_path.exists()


# ------------------------------------------------- the save drive check at the stop click

def test_save_drive_check_passes_on_a_good_drive(tmp_path):
    config, _ = make_config(tmp_path)
    assert save_drive_problem(config) is None


def test_save_drive_check_reports_a_missing_drive(tmp_path):
    config, _ = make_config(tmp_path)
    shutil.rmtree(config.save_path)
    assert save_drive_problem(config) == "SSD drive not reachable"


def test_save_drive_check_reports_an_unwritable_drive(tmp_path, monkeypatch):
    config, _ = make_config(tmp_path)
    monkeypatch.setattr(ratrixcam_run_metadata, "not_writable", lambda _f: "Read-only file system")
    assert save_drive_problem(config) == "SSD drive not writable (Read-only file system)"


def test_save_drive_check_demands_room_for_temp_plus_the_margin(tmp_path, monkeypatch):
    """Temp holds 3 MB in clips (largest 2 MB) and 1 MB of other files: the drive must have
    4 MB + 11 x 2 MB = 26 MB free."""
    config, _ = make_config(tmp_path)
    temp = Path(config.temp_path)
    (temp / "cam").mkdir()
    (temp / "cam" / "a.mp4").write_bytes(b"x" * 2_000_000)
    (temp / "cam" / "b.mp4").write_bytes(b"x" * 1_000_000)
    (temp / "notes.txt").write_bytes(b"x" * 1_000_000)
    needed = 4_000_000 + STOP_SPACE_FACTOR * 2_000_000
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - needed, needed))
    assert save_drive_problem(config) is None
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - needed + 1, needed - 1))
    assert save_drive_problem(config) == "SSD drive full: 26 MB needed to finish this run, 26 MB free"


# ------------------------------------------------- clips left in temp at shutdown

DAY = "Test_subj001_cam1_20260102"
CLIP = "subj001_cam1_20260102_03-04-05.mp4"


def write_clip(path: Path) -> None:
    """A real, openable mp4 of a few frames."""
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened(), "this OpenCV build cannot write mp4v; the fixture would be an empty file"
    for i in range(5):
        writer.write(np.full((48, 64, 3), i * 40, dtype=np.uint8))
    writer.release()


def start_run_with_a_stranded_clip(tmp_path, with_row=True, openable=True):
    """A started run whose camera folder in temp holds one clip the camera did not transfer,
    with its manifest row unless with_row=False."""
    run, temp = start_run_with_a_still(tmp_path)
    clip = temp / "Test_subj001_cam1" / CLIP
    if openable:
        write_clip(clip)
    else:
        clip.parent.mkdir()
        clip.write_bytes(b"cut off by a kill: no index")
    if with_row:
        write_row(str(temp_manifests_dir(run.config.temp_path) / "subj001_cam1_manifest.csv"), f"{DAY}/{CLIP}", "001", "cam1", "2026-01-02 03:04:05.000", 25)
    return run, temp, clip


@pytest.fixture
def no_transfer_waiting(monkeypatch):
    """A re-request that fails gives up at once."""
    monkeypatch.setattr(ratrixcam_server, "TRANSFER_RETRY_DELAY", 0)
    monkeypatch.setattr(ratrixcam_run_metadata, "RE_REQUEST_RETRY_WINDOW", 0)


def test_a_stranded_clip_with_a_manifest_row_is_transferred_at_cleanup(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)

    run.collect_at_shutdown()

    assert (Path(run.config.save_path) / DAY / CLIP).stat().st_size > 0
    assert not temp.exists()  # the clip left, its folder was emptied and removed, so was temp
    out = capsys.readouterr().out
    assert f"Re-requesting the transfer of {CLIP}\n{CLIP} transferred\n" in out
    assert "Clips found in temp: 1; 1 transferred, 0 not transferred (they travel with their folder into the run metadata folder); without a manifest row: 0 deleted, 0 kept" in out
    assert "WARNING" not in out


def test_a_rowless_clip_that_opens_is_kept_and_travels_with_its_folder(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path, with_row=False)

    run.collect_at_shutdown()

    assert (run.run_dir / "Test_subj001_cam1" / CLIP).exists()  # inside the metadata folder on the drive
    assert not (Path(run.config.save_path) / DAY).exists()
    assert not temp.exists()
    out = capsys.readouterr().out
    assert f"WARNING: {CLIP} has no manifest row but opens as video; kept" in out
    assert "WARNING: folder Test_subj001_cam1 with 1 file(s) moved into the run metadata folder" in out
    assert "without a manifest row: 0 deleted, 1 kept" in out


def test_a_rowless_clip_that_does_not_open_is_deleted(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path, with_row=False, openable=False)

    run.collect_at_shutdown()

    assert not clip.exists()
    assert not temp.exists()
    out = capsys.readouterr().out
    assert f"WARNING: {CLIP} has no manifest row and cannot be opened as video; deleted" in out
    assert "without a manifest row: 1 deleted, 0 kept" in out


def test_a_clip_whose_re_request_fails_travels_with_its_folder(tmp_path, no_transfer_waiting, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)
    (Path(run.config.save_path) / DAY).write_bytes(b"")  # a file where the day folder should be

    run.collect_at_shutdown()

    assert (run.run_dir / "Test_subj001_cam1" / CLIP).exists()
    assert not temp.exists()
    out = capsys.readouterr().out
    assert f"Re-requesting the transfer of {CLIP}\nWARNING: {CLIP} left in temp; gave up after 1 try (destination unavailable: " in out
    assert "Clips found in temp: 1; 0 transferred, 1 not transferred" in out
    assert "WARNING: folder Test_subj001_cam1 with 1 file(s) moved into the run metadata folder" in out


def test_re_requests_are_skipped_as_a_whole_when_the_save_folder_is_gone(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)
    shutil.rmtree(run.config.save_path)

    run.collect_at_shutdown()  # with the full 15-second window: the test is quick only if no re-request runs

    assert (run.temp_run_dir / "Test_subj001_cam1" / CLIP).exists()
    out = capsys.readouterr().out
    assert "WARNING: 1 clip(s) not re-requested: save folder not found" in out
    assert "Re-requesting" not in out


def test_re_requests_stop_when_the_drive_runs_short_of_space_mid_pass(tmp_path, monkeypatch, capsys):
    """Two stranded clips; the drive has room for the first re-request only. Checked before each
    clip, so the second costs no retry window."""
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)
    second = clip.parent / "subj001_cam1_20260102_03-05-05.mp4"
    write_clip(second)
    write_row(str(temp_manifests_dir(run.config.temp_path) / "subj001_cam1_manifest.csv"), f"{DAY}/{second.name}", "001", "cam1", "2026-01-02 03:05:05.000", 25)
    Usage = namedtuple("Usage", "total used free")
    real_usage = shutil.disk_usage
    calls: list[int] = []

    def usage(path):
        calls.append(1)
        return real_usage(path) if len(calls) <= 2 else Usage(10**9, 10**9 - 10, 10)  # the cleanup's check, then the transfer's; then full

    monkeypatch.setattr(shutil, "disk_usage", usage)

    run.collect_at_shutdown()  # with the full 15-second window: quick only if the second clip is never re-requested

    out = capsys.readouterr().out
    assert f"{CLIP} transferred" in out
    assert "WARNING: 1 clip(s) not re-requested: destination short of space" in out
    assert f"Re-requesting the transfer of {second.name}" not in out
    assert "Clips found in temp: 2; 1 transferred, 1 not transferred" in out


def test_the_metadata_folder_move_flushes_every_file_and_folder(tmp_path, monkeypatch):
    run, temp = start_run_with_a_still(tmp_path)
    flushed: list[str] = []
    monkeypatch.setattr(ratrixcam_run_metadata, "flush_to_device", lambda p: flushed.append(p))

    run.collect_at_shutdown()

    files = {str(p) for p in run.run_dir.rglob("*") if p.is_file() and p.name != run.log_path.name}  # the log is moved after
    folders = {str(p) for p in run.run_dir.rglob("*") if p.is_dir()} | {str(run.run_dir)}
    assert set(flushed) == files | folders
    assert not run.temp_run_dir.exists()


def test_a_metadata_folder_that_cannot_be_flushed_stays_in_temp(tmp_path, monkeypatch, capsys):
    run, temp = start_run_with_a_still(tmp_path)

    def cannot(_p):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(ratrixcam_run_metadata, "flush_to_device", cannot)

    run.collect_at_shutdown()

    assert (run.temp_run_dir / STILLS_DIRNAME / "cam_cam1_status.png").exists()
    assert run.log_path.exists()
    assert ": copy failed: Input/output error; left in <temp>/" in capsys.readouterr().out


def test_nothing_is_deleted_when_a_manifest_cannot_be_read(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path, with_row=False, openable=False)
    temp_manifests_dir(run.config.temp_path).mkdir()
    (temp_manifests_dir(run.config.temp_path) / "subj001_cam1_manifest.csv").write_text("not,a,manifest\n1,2,3\n")  # no 'path' column

    run.collect_at_shutdown()

    assert (run.run_dir / "Test_subj001_cam1" / CLIP).exists()  # kept, travelled with its folder
    out = capsys.readouterr().out
    assert "WARNING: could not read <temp>/ratrixcam_run_metadata/manifests/subj001_cam1_manifest.csv" in out
    assert f"WARNING: {CLIP} has no manifest row and cannot be opened as video; kept because a manifest could not be read" in out
    assert "deleted" not in out.replace("0 deleted", "")


def test_a_stale_partial_copy_on_the_drive_is_overwritten_by_the_re_request(tmp_path):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)
    day = Path(run.config.save_path) / DAY
    day.mkdir()
    (day / (CLIP + ".part")).write_bytes(b"cut short earlier")

    run.collect_at_shutdown()

    assert sorted(p.name for p in day.iterdir()) == [CLIP]
    assert (day / CLIP).stat().st_size > 100


def test_a_complete_partial_copy_with_no_temp_copy_is_renamed(tmp_path, monkeypatch, capsys):
    """The drive was pulled after the data flush and before the rename: the .part is the clip."""
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)
    day = Path(run.config.save_path) / DAY
    day.mkdir()
    shutil.move(str(clip), str(day / (CLIP + ".part")))  # moves it out of temp as well
    flushed: list[str] = []
    monkeypatch.setattr(ratrixcam_run_metadata, "flush_to_device", lambda p: flushed.append(p))

    run.collect_at_shutdown()

    assert sorted(p.name for p in day.iterdir()) == [CLIP]
    assert flushed[0] == str(day)  # the rename pushed onto the drive, as the transfer does; the metadata move flushes after
    assert not temp.exists()
    assert f"{CLIP} transferred (its complete partial copy on the drive was renamed)" in capsys.readouterr().out


def test_an_unopenable_partial_copy_with_no_temp_copy_is_left_alone(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path)
    clip.unlink()
    day = Path(run.config.save_path) / DAY
    day.mkdir()
    (day / (CLIP + ".part")).write_bytes(b"truncated")

    run.collect_at_shutdown()

    assert sorted(p.name for p in day.iterdir()) == [CLIP + ".part"]
    assert f"WARNING: <save>/{DAY}/{CLIP}.part is a partial copy that cannot be opened as video; left in place" in capsys.readouterr().out


def test_abort_neither_transfers_nor_deletes_stranded_clips(tmp_path, capsys):
    run, temp, clip = start_run_with_a_stranded_clip(tmp_path, with_row=False, openable=False)
    write_clip(clip.parent / "second.mp4")
    write_row(str(temp_manifests_dir(run.config.temp_path) / "subj001_cam1_manifest.csv"), f"{DAY}/second.mp4", "001", "cam1", "2026-01-02 03:05:05.000", 25)

    run.collect_at_shutdown(abort=True)

    assert sorted(p.name for p in clip.parent.iterdir()) == ["second.mp4", CLIP]  # both still where the camera wrote them
    assert not (Path(run.config.save_path) / DAY).exists()
    out = capsys.readouterr().out
    assert "Re-requesting" not in out and "deleted" not in out and "moved into" not in out
