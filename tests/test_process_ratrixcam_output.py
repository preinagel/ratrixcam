"""The ratrixcam wrapper: compress_drive over the runs found in a folder written by ratrixcam,
then the run-metadata and manifests folders carried to the destination and the files outside
the manifests inventoried, all appended to the compression run's own records."""

import csv
import json
import subprocess
import sys
from pathlib import Path

from helpers import DATE, SUBJECT, VIDEOPROC_DIR, clip_start, fname, latest_run_dir, place, read_log, read_rows

RUN_ID = "ratrixcam_20250101_095900_abcd1234"
STUDY = "LS"


def add_run(root: Path, clips: dict[str, Path], run_id: str, times, study=STUDY, view="home") -> Path:
    """One recording run as ratrixcam leaves it: a day folder with the clips, and the run's
    metadata folder holding the config copy, notes, log, stills and manifests. Returns the
    manifests folder."""
    day = root / f"{study}_{SUBJECT}_{view}_{DATE}"
    meta = root / "ratrixcam_run_metadata" / run_id
    manifests = meta / "manifests"
    manifests.mkdir(parents=True)
    with open(manifests / f"{SUBJECT}_{view}_manifest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "subjID", "view", "clip_start", "row_written", "crf"])
        for t in times:
            place(clips["R30"], day / fname(t, view=view))
            w.writerow([f"{day.name}/{fname(t, view=view)}", "901", view, clip_start(t), clip_start(t), 25])
    (meta / "stills").mkdir(parents=True)
    (meta / "camera_config_test.json").write_text(json.dumps({"study_label": study, "subject_prefix": "rat", "cameras": []}))
    (meta / "notes.txt").write_text("notes\n")
    (meta / f"terminal_{run_id}.log").write_text("log\n")
    (meta / "stills" / f"cam_rat901_{view}_status.png").write_bytes(b"png")
    return manifests


def make_drive(root: Path, clips: dict[str, Path], times=("10-00-00", "10-01-00")) -> Path:
    """A folder laid out as ratrixcam leaves a save_path drive: one run (see add_run) plus a
    stray video and a stray text file outside the manifests. Returns the manifests folder."""
    manifests = add_run(root, clips, RUN_ID, times)
    place(clips["R30"], root / "not_a_day_folder" / "stray.mp4")
    (root / "readme.txt").write_text("not a video\n")
    return manifests


def run_wrapper(root: Path, dest: Path, extra_args=(), timeout=300) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "process_ratrixcam_output.py", "compress", str(root), str(dest), "--n_threads", "4", *extra_args]
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input="\n", capture_output=True, text=True, timeout=timeout)


def test_compress_processes_listed_clips_and_carries_the_folders(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)

    result = run_wrapper(root, dest)
    assert result.returncode == 0, result.stderr + result.stdout

    assert (dest / SUBJECT / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "camera_config_test.json").exists()
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "stills" / "cam_rat901_home_status.png").exists()
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "manifests" / f"{SUBJECT}_home_manifest.csv").exists()
    assert "ratrixcam run-metadata folders transferred: 1 (5 file(s) copied)" in result.stdout

    run_dir = latest_run_dir(dest, "compression_")
    log = read_log(run_dir)
    assert log[fname("10-00-00")]["status"] == "compressed"
    assert log["stray.mp4"]["status"] == "not_in_manifest" and log["stray.mp4"]["action_item"] == "True"
    assert "stray.mp4" in {Path(r["input_path"]).name for r in read_rows(run_dir / "action_items.csv")}
    assert "1 video file(s) under" in result.stdout and "other files under" in result.stdout
    terminal = (run_dir / "terminal.log").read_text()
    assert "==== run summary ====" in terminal
    assert f"ratrixcam run {RUN_ID}: run metadata folder" in terminal
    assert "everything outside the processed runs" in terminal
    assert terminal.index("==== run summary ====") < terminal.index("run metadata folder") < terminal.index("outside the processed runs")
    assert (root / "not_a_day_folder" / "stray.mp4").exists() and (root / "readme.txt").exists()  # untouched

    # second run: everything already there, nothing copied, nothing overwritten
    result2 = run_wrapper(root, dest)
    assert "ratrixcam run-metadata folders transferred: 1 (0 file(s) copied)" in result2.stdout


def test_compress_never_overwrites_a_differing_destination_file(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    make_drive(root, clips)
    dest_meta = dest / "ratrixcam_run_metadata" / RUN_ID
    dest_meta.mkdir(parents=True)
    (dest_meta / "notes.txt").write_text("a different, longer note\n")

    result = run_wrapper(root, dest)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (dest_meta / "notes.txt").read_text() == "a different, longer note\n"
    assert "1 file(s) differ at the destination and were not overwritten" in result.stdout


def test_each_recording_run_is_its_own_compression_run(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    second = "ratrixcam_20250101_110000_ef567890"
    add_run(root, clips, RUN_ID, ("10-00-00",))
    add_run(root, clips, second, ("11-00-00",), study="Pilot", view="face")

    result = run_wrapper(root, dest)
    assert result.returncode == 0, result.stderr + result.stdout
    runs = sorted(p for p in (dest / "videoproc_run_metadata").iterdir() if p.name.startswith("compression_"))
    assert len(runs) == 2
    assert (dest / SUBJECT / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
    assert (dest / SUBJECT / f"Pilot_{SUBJECT}_face_{DATE}" / fname("11-00-00", view="face")).exists()
    terminals = {p: (p / "terminal.log").read_text() for p in runs}
    # each compression run carries its own run's folders ...
    assert sum(f"ratrixcam run {RUN_ID}: run metadata folder" in t for t in terminals.values()) == 1
    assert sum(f"ratrixcam run {second}: run metadata folder" in t for t in terminals.values()) == 1
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()
    assert (dest / "ratrixcam_run_metadata" / second / "manifests" / f"{SUBJECT}_face_manifest.csv").exists()
    # ... and the inventory of everything else is appended once, to the last one
    assert sum("outside the processed runs" in t for t in terminals.values()) == 1


def test_a_run_folder_without_manifests_is_skipped_but_still_carried(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    add_run(root, clips, RUN_ID, ("10-00-00",))
    empty_run = root / "ratrixcam_run_metadata" / "ratrixcam_20250101_120000_00000000"
    empty_run.mkdir()
    (empty_run / "camera_config_test.json").write_text(json.dumps({"study_label": STUDY, "subject_prefix": "rat"}))
    (empty_run / "notes.txt").write_text("recorded nothing\n")

    result = run_wrapper(root, dest)
    assert result.returncode == 1, result.stderr + result.stdout  # a skipped run is reported in the exit code
    assert "skipped: no manifest" in result.stdout
    assert (dest / "ratrixcam_run_metadata" / "ratrixcam_20250101_120000_00000000" / "notes.txt").exists()
    assert "ratrixcam run-metadata (not processed) folders transferred: 1 (2 file(s) copied)" in result.stdout


def test_compress_without_a_config_copy_skips_the_run(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)
    (root / "ratrixcam_run_metadata" / RUN_ID / "camera_config_test.json").unlink()

    result = run_wrapper(root, dest)
    assert result.returncode == 1
    assert "expected one camera config copy" in result.stdout
    assert f"run {RUN_ID} skipped" in result.stdout
    assert not (dest / SUBJECT).exists()


def test_compress_without_run_folders_stops(tmp_path):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    root.mkdir()
    dest.mkdir()
    result = run_wrapper(root, dest)
    assert result.returncode == 1
    assert "nothing to process" in result.stdout
