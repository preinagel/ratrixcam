"""The ratrixcam wrapper: compress_drive once per run found in a folder written by ratrixcam,
each run's metadata folder carried to the destination in the compression run's own records,
and the runs that could not be processed reported on the terminal."""

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
    stray video and a stray text file outside the run folders. Returns the manifests folder."""
    manifests = add_run(root, clips, RUN_ID, times)
    place(clips["R30"], root / "not_a_day_folder" / "stray.mp4")
    (root / "readme.txt").write_text("not a video\n")
    return manifests


def run_wrapper(root: Path, dest: Path, extra_args=(), timeout=300) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "process_ratrixcam_output.py", "compress", str(root), str(dest), "--n_threads", "4", *extra_args]
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input="\n", capture_output=True, text=True, timeout=timeout)


def compression_runs(dest: Path) -> list[Path]:
    return sorted(p for p in (dest / "videoproc_run_metadata").iterdir() if p.name.startswith("compression_"))



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
    assert "stray.mp4" not in log and "stray" not in result.stdout  # files outside the run folders are not inspected
    terminal = (run_dir / "terminal.log").read_text()
    assert "==== run summary ====" in terminal
    assert f"ratrixcam run {RUN_ID}: run metadata folder" in terminal
    assert terminal.index("==== run summary ====") < terminal.index("run metadata folder")
    assert "runs not processed" not in terminal and "runs not processed" in result.stdout  # terminal only
    assert "runs processed: 1 of 1" in result.stdout
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
    runs = compression_runs(dest)
    assert len(runs) == 2
    assert (dest / SUBJECT / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
    assert (dest / SUBJECT / f"Pilot_{SUBJECT}_face_{DATE}" / fname("11-00-00", view="face")).exists()
    terminals = {p: (p / "terminal.log").read_text() for p in runs}
    # each compression run carries its own run's folders ...
    assert sum(f"ratrixcam run {RUN_ID}: run metadata folder" in t for t in terminals.values()) == 1
    assert sum(f"ratrixcam run {second}: run metadata folder" in t for t in terminals.values()) == 1
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()
    assert (dest / "ratrixcam_run_metadata" / second / "manifests" / f"{SUBJECT}_face_manifest.csv").exists()
    assert "runs processed: 2 of 2" in result.stdout


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
    assert "runs processed: 1 of 2" in result.stdout
    for run in compression_runs(dest):  # the skip is not written into the processed run's records
        assert "skipped" not in (run / "terminal.log").read_text()


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
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()  # carried all the same


def test_studyname_and_prefix_arguments_override_the_config_copy(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)
    (root / "ratrixcam_run_metadata" / RUN_ID / "camera_config_test.json").unlink()  # no config copy at all

    result = run_wrapper(root, dest, extra_args=["--studyname", "Pilot", "--subject_prefix", ""])
    assert result.returncode == 0, result.stderr + result.stdout
    assert (dest / "901" / f"Pilot_901_home_{DATE}" / fname("10-00-00")).exists()  # clip names unchanged


def test_compress_without_run_folders_stops(tmp_path):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    root.mkdir()
    dest.mkdir()
    result = run_wrapper(root, dest)
    assert result.returncode == 1
    assert "nothing to process" in result.stdout


# ----------------------------------------------------------------------------- confirm

def run_confirm(
    root: Path, dest: Path, extra_args=(), n_delete_prompts=0, write_leftovers="n", timeout=300
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "process_ratrixcam_output.py", "confirm", str(root), str(dest), "--no_pause", *extra_args]
    stdin = "\n" + "DELETE\n" * n_delete_prompts + write_leftovers + "\n"
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input=stdin, capture_output=True, text=True, timeout=timeout)


def confirm_runs(dest: Path) -> list[Path]:
    return sorted(p for p in (dest / "videoproc_run_metadata").iterdir() if p.name.startswith("confirm_"))


def test_confirm_reports_the_listed_clips_of_each_run(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)
    assert run_wrapper(root, dest).returncode == 0

    result = run_confirm(root, dest, extra_args=["--min_age_hours", "0"])
    assert result.returncode == 0, result.stderr + result.stdout
    runs = confirm_runs(dest)
    assert len(runs) == 1
    log = read_log(runs[0])
    assert set(log) == {fname("10-00-00"), fname("10-01-00")}  # the stray video is not considered
    assert all(row["status"] == "already_done" and row["deletable"] == "True" for row in log.values())
    assert "nothing was deleted" in result.stdout and "runs processed: 1 of 1" in result.stdout
    assert (root / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
    terminal = (runs[0] / "terminal.log").read_text()
    assert "run folder verified byte for byte" in terminal and "run folder kept: --delete not set" in terminal
    assert (root / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()
    # the verdict is explicit, and clips that --delete would remove are not leftovers
    assert "every listed clip present at the source is verified: --delete would delete them all" in result.stdout
    assert "listed clips not deletable: 0" in result.stdout
    assert "run folders that would remain: 0 (plus 1 that --delete would remove)" in result.stdout
    assert result.stdout.count("checking") == 1  # one line per run, no per-clip lines


def test_confirm_report_only_lists_what_delete_partial_would_leave(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)  # two clips listed
    assert run_wrapper(root, dest).returncode == 0
    (dest / SUBJECT / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-01-00")).unlink()  # one destination copy gone

    result = run_confirm(root, dest, extra_args=["--min_age_hours", "0"], write_leftovers="y")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 clip(s) would block --delete; --delete_partial would delete the 1 verified one(s) and leave those 1" in result.stdout
    assert "decode check was not run" in result.stdout
    clips_left = read_rows(root / "leftover_listed_clips.csv")
    assert [r["path"].rsplit("/", 1)[1] for r in clips_left] == [fname("10-01-00")]  # the verified one is not listed
    folders = read_rows(root / "leftover_run_folders.csv")
    assert len(folders) == 1 and folders[0]["reason"] == "1 listed clip(s) not deletable"


def test_confirm_pauses_between_runs_unless_told_not_to(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    add_run(root, clips, RUN_ID, ("10-00-00",))
    add_run(root, clips, "ratrixcam_20250101_110000_ef567890", ("11-00-00",), view="face")
    assert run_wrapper(root, dest).returncode == 0
    cmd = [sys.executable, "process_ratrixcam_output.py", "confirm", str(root), str(dest), "--min_age_hours", "0"]
    result = subprocess.run(cmd, cwd=VIDEOPROC_DIR, input="\n\nn\n", capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.count("Press Enter to continue to the next run") == 1


def test_confirm_delete_asks_once_per_run_and_deletes_only_listed_clips(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    second = "ratrixcam_20250101_110000_ef567890"
    make_drive(root, clips, times=("10-00-00",))
    add_run(root, clips, second, ("11-00-00",), view="face")
    assert run_wrapper(root, dest).returncode == 0

    result = run_confirm(root, dest, extra_args=["--min_age_hours", "0", "--no-decode", "--delete"], n_delete_prompts=4)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.count("Type DELETE to proceed") == 4  # clips, then the run folder, for each run
    assert not (root / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
    assert not (root / f"{STUDY}_{SUBJECT}_face_{DATE}" / fname("11-00-00", view="face")).exists()
    assert (root / "not_a_day_folder" / "stray.mp4").exists()  # not listed: untouched
    assert not (root / "ratrixcam_run_metadata" / RUN_ID).exists()  # verified, clips gone: deleted
    assert not (root / "ratrixcam_run_metadata" / second).exists()
    assert (dest / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()  # destination copy untouched
    runs = confirm_runs(dest)
    assert len(runs) == 2
    for run in runs:
        rows = read_rows(run / "deletion_log.csv")
        assert len(rows) == 2 and rows[1]["input_path"].endswith(("/" + RUN_ID, "/" + second))  # clip, then folder
        assert "Run folder deleted" in (run / "terminal.log").read_text()
    # what remains: only the strays; the leftover files were declined
    assert "listed clips not deletable: 0" in result.stdout
    assert "run folders that would remain: 0" in result.stdout
    assert "video files not listed in any manifest: 1" in result.stdout and "stray.mp4" in result.stdout
    assert "other ratrixcam-looking files outside the run folders: 1" in result.stdout  # readme.txt
    assert "leftover files not written" in result.stdout
    assert not list(root.glob("leftover_*.csv"))


def test_confirm_keeps_the_run_folder_when_a_listed_clip_remains(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)  # two clips listed
    assert run_wrapper(root, dest).returncode == 0
    (dest / SUBJECT / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-01-00")).unlink()  # one destination copy gone

    result = run_confirm(
        root, dest, extra_args=["--min_age_hours", "0", "--no-decode", "--delete", "--delete_partial"],
        n_delete_prompts=1, write_leftovers="y",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.count("Type DELETE to proceed") == 1  # the clips only; no folder prompt
    assert not (root / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
    assert (root / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-01-00")).exists()
    assert (root / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()
    assert "run folder kept: 1 of its 2 listed clip(s) remain at the source" in result.stdout

    # the leftovers, on the terminal and in the three files
    assert "listed clips not deletable: 1" in result.stdout and "run folders that would remain: 1" in result.stdout
    clips = read_rows(root / "leftover_listed_clips.csv")
    assert len(clips) == 1 and clips[0]["path"].endswith(fname("10-01-00"))
    assert clips[0]["run_id"] == RUN_ID and clips[0]["status"] == "not_at_destination" and clips[0]["subjID"] == "901"
    folders = read_rows(root / "leftover_run_folders.csv")
    assert len(folders) == 1 and folders[0]["path"] == f"ratrixcam_run_metadata/{RUN_ID}" and folders[0]["n_files"] == "5"
    assert "1 of its 2 listed clip(s) remain" in folders[0]["reason"]
    unlisted = [r["path"] for r in read_rows(root / "leftover_unlisted_files.csv")]
    assert unlisted == ["not_a_day_folder/stray.mp4", "readme.txt"]

    # a second pass does not count its own leftover files as leftovers
    again = run_confirm(root, dest, extra_args=["--min_age_hours", "0"], write_leftovers="n")
    assert again.returncode == 0, again.stderr + again.stdout
    assert "no other files; empty folders: 0" in again.stdout


def test_confirm_lists_the_clips_of_a_skipped_run_as_not_checked(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)
    assert run_wrapper(root, dest).returncode == 0
    (root / "ratrixcam_run_metadata" / RUN_ID / "camera_config_test.json").unlink()  # the run can no longer be read

    result = run_confirm(root, dest, extra_args=["--min_age_hours", "0"], write_leftovers="y")
    assert result.returncode == 1  # the skip is reported in the exit code
    assert "listed clips not deletable: 2" in result.stdout
    clips_left = read_rows(root / "leftover_listed_clips.csv")
    assert {r["status"] for r in clips_left} == {"not checked"}
    assert all("run skipped: study label and subject prefix unknown" in r["reason"] for r in clips_left)
    folders = read_rows(root / "leftover_run_folders.csv")
    assert len(folders) == 1 and folders[0]["reason"] == "study label and subject prefix unknown"


def test_confirm_keeps_a_run_folder_whose_destination_copy_differs(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)
    assert run_wrapper(root, dest).returncode == 0
    (dest / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").write_text("edited\n")  # same size, different bytes

    result = run_confirm(root, dest, extra_args=["--min_age_hours", "0", "--no-decode", "--delete"], n_delete_prompts=2)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.count("Type DELETE to proceed") == 1
    assert "run folder NOT verified" in result.stdout and "differs: notes.txt" in result.stdout
    assert (root / "ratrixcam_run_metadata" / RUN_ID / "notes.txt").exists()


def test_confirm_delete_refuses_when_a_listed_clip_is_not_at_the_destination(tmp_path, clips):
    root, dest = tmp_path / "drive", tmp_path / "dest"
    dest.mkdir()
    make_drive(root, clips)  # never compressed

    result = run_confirm(root, dest, extra_args=["--min_age_hours", "0", "--delete"], n_delete_prompts=1)
    assert result.returncode == 0, result.stderr + result.stdout  # the run completed; the refusal is the tool's verdict
    assert "REFUSING to delete" in result.stdout
    assert (root / f"{STUDY}_{SUBJECT}_home_{DATE}" / fname("10-00-00")).exists()
