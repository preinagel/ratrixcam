"""Behavioral tests for compress_drive.py, driven as a subprocess against a synthetic
source/destination tree. run1/run2/run3 are the three sequential runs described in the
test brief; each test function checks one outcome from the already-completed run."""

import json

from helpers import (
    HOM_TIME, T11_DATE, T11_TIME, TIMES,
    fname, is_true, latest_run_dir, place, read_log, read_rows, run_compress_drive, source_path,
)


# ---------------------------------------------------------------------------
# run 1: compress_drive <src> <dest> --output_studyname LS --pattern ... --n_threads 4
#        --motion_detection --motion_threshold 0.001
# ---------------------------------------------------------------------------

def test_run1_raw_with_nothing_at_destination_is_compressed(run1):
    assert run1["log"][fname(TIMES["t01"])]["status"] == "compressed"


def test_run1_compressed_source_is_copied(run1):
    assert run1["log"][fname(TIMES["t02"])]["status"] == "copied"


def test_run1_raw_at_destination_is_flagged(run1):
    row = run1["log"][fname(TIMES["t03"])]
    assert row["status"] == "raw_at_destination"
    assert is_true(row["action_item"])


def test_run1_frame_count_mismatch_is_collision(run1):
    row = run1["log"][fname(TIMES["t04"])]
    assert row["status"] == "collision"
    assert is_true(row["action_item"])


def test_run1_unopenable_destination_is_replaced(run1):
    assert run1["log"][fname(TIMES["t05"])]["status"] == "compressed"


def test_run1_invalid_source_nothing_at_destination(run1):
    row = run1["log"][fname(TIMES["t06"])]
    assert row["status"] == "invalid_source"
    assert not is_true(row["action_item"])


def test_run1_invalid_source_unopenable_destination(run1):
    row = run1["log"][fname(TIMES["t07"])]
    assert row["status"] == "invalid_source"
    assert not is_true(row["action_item"])


def test_run1_invalid_source_compressed_destination(run1):
    row = run1["log"][fname(TIMES["t08"])]
    assert row["status"] == "invalid_source"
    assert not is_true(row["action_item"])


def test_run1_invalid_source_raw_destination_is_flagged(run1):
    row = run1["log"][fname(TIMES["t09"])]
    assert row["status"] == "invalid_source"
    assert is_true(row["action_item"])
    assert "raw (uncompressed)" in row["skipped_reason"]


def test_run1_corrupted_raw_source_yields_invalid_output(run1):
    row = run1["log"][fname(TIMES["t10"])]
    assert row["status"] == "output_invalid"
    assert is_true(row["action_item"])


def test_run1_readonly_destination_folder_is_an_error(run1):
    row = run1["log"][fname(T11_TIME, date=T11_DATE)]
    assert row["status"] == "error"
    assert is_true(row["action_item"])


def test_run1_unparseable_filename_is_flagged(run1):
    row = run1["log"]["weird_clip.mp4"]
    assert row["status"] == "unparseable"
    assert is_true(row["action_item"])


def test_run1_misspelt_view_is_compressed_and_warned(run1):
    row = run1["log"][fname(HOM_TIME, view="hom")]
    assert row["status"] == "compressed"
    assert "unrecognized camera view 'hom'" in run1["result"].stdout
    assert "1 file(s) had an unrecognized camera view" in run1["result"].stdout


def test_run1_pattern_mismatches_are_listed_individually(run1):
    for name in ("stray_one.mp4", "stray_two.mp4"):
        row = run1["log"][name]
        assert row["status"] == "pattern_mismatch"
        assert is_true(row["action_item"])


def test_run1_non_mp4_files_are_counted_in_summary(run1):
    assert "non-.mp4 files in source (not checked or copied): 1" in run1["result"].stdout


def test_run1_motion_timeseries_has_one_row_per_motion_detected_clip(run1):
    # t03 is skipped as raw_at_destination before reaching compression/motion detection
    rows = read_rows(run1["run_dir"] / "motion_timeseries.csv")
    expected_names = {
        fname(TIMES["t01"]), fname(TIMES["t05"]),
        fname(TIMES["t10"]), fname(HOM_TIME, view="hom"),
    }
    assert {row["clip_filename"] for row in rows} == expected_names
    for row in rows:
        assert [float(x) for x in row["motion_values"].split()]


# ---------------------------------------------------------------------------
# run 2: same as run 1 + --overwrite_raw
# ---------------------------------------------------------------------------

def test_run2_raw_destination_is_replaced(run2):
    assert run2["log"][fname(TIMES["t03"])]["status"] == "compressed"


def test_run2_already_compressed_destination_is_already_done(run2):
    assert run2["log"][fname(TIMES["t01"])]["status"] == "already_done"


def test_run2_already_copied_destination_is_already_done(run2):
    assert run2["log"][fname(TIMES["t02"])]["status"] == "already_done"


def test_run2_collision_unaffected_by_overwrite_raw(run2):
    assert run2["log"][fname(TIMES["t04"])]["status"] == "collision"


def test_run2_now_writable_destination_is_copied(run2):
    assert run2["log"][fname(T11_TIME, date=T11_DATE)]["status"] == "copied"


def test_run2_invalid_source_unaffected_by_overwrite_raw(run2):
    assert run2["log"][fname(TIMES["t09"])]["status"] == "invalid_source"


# ---------------------------------------------------------------------------
# run 3: same as run 1 + --overwrite_compressed
# ---------------------------------------------------------------------------

def test_run3_raw_source_is_recompressed(run3):
    assert run3["log"][fname(TIMES["t01"])]["status"] == "compressed"


def test_run3_compressed_source_is_recopied(run3):
    assert run3["log"][fname(TIMES["t02"])]["status"] == "copied"


def test_run3_collision_wins_over_overwrite_compressed(run3):
    assert run3["log"][fname(TIMES["t04"])]["status"] == "collision"


# ---------------------------------------------------------------------------
# motion detection on/off, independent of the three-run sequence
# ---------------------------------------------------------------------------

def test_motion_detection_off_leaves_motion_columns_blank(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place(clips["R30"], source_path(source_root, "10-00-00"))

    result = run_compress_drive(source_root, dest_root, extra_args=["--n_threads", "4"])
    assert result.returncode == 0, result.stderr

    run_dir = latest_run_dir(dest_root, "videoproc_")
    row = read_log(run_dir)[fname("10-00-00")]
    assert row["status"] == "compressed"
    assert row["found_motion"] == ""
    assert row["motion_perc"] == ""
    assert not (run_dir / "motion_timeseries.csv").exists()

    config = json.loads((run_dir / "config.json").read_text())
    assert config["motion_detection"]["enabled"] is False


def test_motion_detection_on_compressed_source_is_skipped(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place(clips["C30"], source_path(source_root, "10-00-00"))

    result = run_compress_drive(source_root, dest_root, extra_args=[
        "--n_threads", "4", "--motion_detection", "--motion_threshold", "0.001", "--recompress",
    ])
    assert result.returncode == 0, result.stderr
    assert "motion detection skipped" in result.stdout
    assert "skipped for 1 clip(s) from already-compressed sources" in result.stdout
