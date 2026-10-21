"""Behavioral tests for compress_drive.py, driven as a subprocess against a synthetic
source/destination tree. run1/run2/run3 are the three sequential runs described in the
test brief; each test function checks one outcome from the already-completed run."""

from helpers import (
    HOM_TIME, STUDYNAME, SUBJECT, T11_DATE, T11_TIME, TIMES,
    add_manifest_row, dest_path, fname, is_true, last_index_row, latest_run_dir, manifest_path, place,
    place_clip, read_log, read_rows, run_compress_drive, session_folder_path, source_path,
)


# ---------------------------------------------------------------------------
# run 1: compress_drive <src> <dest> --manifest ... --studyname LS --subject_prefix rat
#        --n_threads 4 --motion_detection --motion_threshold 0.001
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


def test_run1_files_not_in_the_manifest_are_left_alone(run1, layout):
    source_root, _, _ = layout
    for name in ("weird_clip.mp4", "stray_one.mp4", "stray_two.mp4"):
        assert name not in run1["log"]
    assert (session_folder_path(source_root) / "weird_clip.mp4").exists()
    assert (source_root / "not_a_session_folder" / "stray_one.mp4").exists()
    assert (source_root / "notes.txt").exists()
    assert "clips listed in the manifest(s): 12" in run1["result"].stdout


def test_run1_any_view_string_is_accepted(run1, layout):
    _, dest_root, _ = layout
    row = run1["log"][fname(HOM_TIME, view="hom")]
    assert row["status"] == "compressed"
    assert row["view"] == "hom"
    assert dest_path(dest_root, HOM_TIME, view="hom").exists()


def test_run1_destination_layout_from_manifest_and_arguments(run1, layout):
    _, dest_root, _ = layout
    row = run1["log"][fname(TIMES["t01"])]
    assert row["output_path"] == str(dest_root / SUBJECT / f"{STUDYNAME}_{SUBJECT}_home_20250101" / fname(TIMES["t01"]))
    assert row["subjID"] == "901" and row["crf"] == "25"


def test_run1_summary_and_index_row(run1, layout):
    _, dest_root, _ = layout
    summary = read_rows(run1["run_dir"] / "summary_by_subj_and_view.csv")
    assert [(r["subjID"], r["view"], r["file_count"]) for r in summary] == [("901", "hom", "1"), ("901", "home", "11")]
    index = last_index_row(dest_root)
    assert index["run_id"] == run1["run_dir"].name and index["run_id"].startswith("compression_")
    assert index["n_files"] == "12" and index["n_streams"] == "2"
    assert index["motion_detection"] == "True" and index["motion_threshold"] == "0.001"
    assert not (run1["run_dir"] / "compression_config.json").exists()


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
    place_clip(clips["R30"], source_root, "10-00-00")

    result = run_compress_drive(source_root, dest_root, extra_args=["--n_threads", "4"])
    assert result.returncode == 0, result.stderr

    run_dir = latest_run_dir(dest_root, "compression_")
    row = read_log(run_dir)[fname("10-00-00")]
    assert row["status"] == "compressed"
    assert row["found_motion"] == ""
    assert row["motion_perc"] == ""
    assert not (run_dir / "motion_timeseries.csv").exists()
    assert last_index_row(dest_root)["motion_detection"] == "False"


# ---------------------------------------------------------------------------
# the manifest as the only source of truth
# ---------------------------------------------------------------------------

def test_manifest_crf_is_used_for_motion_clips_and_overridden_for_static_ones(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place_clip(clips["R30"], source_root, "10-00-00", crf=28)
    place_clip(clips["R30_static"], source_root, "10-01-00", crf=28)
    place_clip(clips["R30"], source_root, "10-02-00")  # no crf column value: DEFAULT_CRF

    result = run_compress_drive(source_root, dest_root, extra_args=[
        "--n_threads", "4", "--motion_detection", "--motion_threshold", "0.001", "--motion_percentile", "90",
    ])
    assert result.returncode == 0, result.stderr
    log = read_log(latest_run_dir(dest_root, "compression_"))
    assert log[fname("10-00-00")]["crf"] == "28"
    assert log[fname("10-01-00")]["crf"] == "40"
    assert log[fname("10-02-00")]["crf"] == "25"


def test_listed_file_missing_at_source_is_flagged(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place_clip(clips["R30"], source_root, "10-00-00")
    add_manifest_row(source_root, "Longitudinal Study_rat901_home_20250101/missing.mp4", "901", "home", "2025-01-01 10:05:00")

    result = run_compress_drive(source_root, dest_root, extra_args=["--n_threads", "4"])
    assert result.returncode == 0, result.stderr
    log = read_log(latest_run_dir(dest_root, "compression_"))
    assert log[fname("10-00-00")]["status"] == "compressed"
    assert log["missing.mp4"]["status"] == "missing_source"
    assert is_true(log["missing.mp4"]["action_item"])


def test_no_studyname_and_blank_prefix_shape_the_destination(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place_clip(clips["R30"], source_root, "10-00-00")
    import subprocess, sys
    from helpers import VIDEOPROC_DIR
    cmd = [sys.executable, "compress_drive.py", str(source_root), str(dest_root), "--manifest", str(manifest_path(source_root)),
           "--subject_prefix", "", "--n_threads", "4"]
    result = subprocess.run(cmd, cwd=VIDEOPROC_DIR, input="\n", capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert (dest_root / "901" / "901_home_20250101" / fname("10-00-00")).exists()


def test_manifest_missing_a_required_column_stops_the_run(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place_clip(clips["R30"], source_root, "10-00-00")
    manifest_path(source_root).write_text("path,subjID,clip_start\nx.mp4,901,2025-01-01 10:00:00\n")
    result = run_compress_drive(source_root, dest_root, extra_args=["--n_threads", "4"])
    assert result.returncode == 0, result.stderr
    assert "ERROR" in result.stdout and "missing column(s) view" in result.stdout
    assert not (latest_run_dir(dest_root, "compression_") / "compression_log.csv").exists()


def test_a_path_listed_twice_is_processed_once(tmp_path, clips):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    target = place_clip(clips["R30"], source_root, "10-00-00")
    add_manifest_row(source_root, target.relative_to(source_root).as_posix(), "901", "home", "2025-01-01 10:00:00")
    result = run_compress_drive(source_root, dest_root, extra_args=["--n_threads", "4"])
    assert result.returncode == 0, result.stderr
    assert "duplicate" in result.stdout.lower() or "already listed" in result.stdout
    assert "clips listed in the manifest(s): 1" in result.stdout

