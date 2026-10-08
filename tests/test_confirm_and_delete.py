"""Behavioral tests for confirm_and_delete.py. Scenarios A/B/D run in sequence against the
tree compress_drive's three runs (run1/run2/run3) left behind, after the "preparation"
mutations described in the test brief. Scenario C and the source/destination-nesting checks
use their own independent, minimal layouts."""

import subprocess
import sys

import pytest

from helpers import (
    HOM_TIME, STUDYNAME, T11_DATE, T11_TIME, T12_TIME, TIMES, VIDEOPROC_DIR,
    dest_path, fname, is_true, latest_run_dir, place, read_log, read_rows,
    run_confirm_and_delete, session_folder_path, source_path,
)


@pytest.fixture(scope="module")
def cd_prepared(run3, layout, clips):
    source_root, dest_root, _ = layout

    place(clips["R30"], source_path(source_root, T12_TIME))
    place(clips["R30"], dest_path(dest_root, T12_TIME))

    place(clips["C30_corrupt"], dest_path(dest_root, TIMES["t01"]))
    place(clips["junk"], dest_path(dest_root, TIMES["t05"]))
    dest_path(dest_root, TIMES["t02"]).unlink()

    return source_root, dest_root


@pytest.fixture(scope="module")
def confirm_a(cd_prepared):
    source_root, dest_root = cd_prepared
    result = run_confirm_and_delete(source_root, dest_root, extra_args=["--min_age_hours", "0", "--decode"])
    assert result.returncode == 0, result.stderr
    run_dir = latest_run_dir(dest_root, "confirm_")
    return {"result": result, "run_dir": run_dir, "log": read_log(run_dir)}


def test_confirm_a_decode_failure_is_flagged(confirm_a):
    row = confirm_a["log"][fname(TIMES["t01"])]
    assert row["status"] == "decode_failed"
    assert is_true(row["action_item"])


def test_confirm_a_missing_destination_is_flagged(confirm_a):
    row = confirm_a["log"][fname(TIMES["t02"])]
    assert row["status"] == "not_at_destination"
    assert is_true(row["action_item"])


def test_confirm_a_clean_match_is_deletable(confirm_a):
    row = confirm_a["log"][fname(TIMES["t03"])]
    assert row["status"] == "already_done"
    assert is_true(row["deletable"])


def test_confirm_a_frame_mismatch_is_collision(confirm_a):
    assert confirm_a["log"][fname(TIMES["t04"])]["status"] == "collision"


def test_confirm_a_unopenable_destination_is_flagged(confirm_a):
    row = confirm_a["log"][fname(TIMES["t05"])]
    assert row["status"] == "destination_invalid"
    assert is_true(row["action_item"])


def test_confirm_a_invalid_source_is_flagged(confirm_a):
    row = confirm_a["log"][fname(TIMES["t09"])]
    assert row["status"] == "invalid_source"
    assert is_true(row["action_item"])


def test_confirm_a_new_raw_at_destination_is_flagged(confirm_a):
    row = confirm_a["log"][fname(T12_TIME)]
    assert row["status"] == "raw_at_destination"
    assert is_true(row["action_item"])


def test_confirm_a_clean_copy_decodes_and_is_deletable(confirm_a):
    row = confirm_a["log"][fname(T11_TIME, date=T11_DATE)]
    assert row["status"] == "already_done"
    assert row["decode_ok"] == "True"
    assert is_true(row["deletable"])


def test_confirm_a_unparseable_filename_is_flagged(confirm_a):
    assert confirm_a["log"]["weird_clip.mp4"]["status"] == "unparseable"


def test_confirm_a_reports_only_nothing_deleted(confirm_a, cd_prepared):
    assert "nothing was deleted" in confirm_a["result"].stdout
    source_root, _ = cd_prepared
    assert source_path(source_root, TIMES["t03"]).exists()


@pytest.fixture(scope="module")
def confirm_b(cd_prepared, confirm_a):
    source_root, dest_root = cd_prepared
    result = run_confirm_and_delete(source_root, dest_root, delete=True)
    assert result.returncode == 0, result.stderr
    run_dir = latest_run_dir(dest_root, "confirm_")
    return {"result": result, "run_dir": run_dir, "log": read_log(run_dir)}


def test_confirm_b_default_min_age_makes_fresh_output_too_recent(confirm_b):
    row = confirm_b["log"][fname(TIMES["t03"])]
    assert row["status"] == "too_recent"
    assert is_true(row["action_item"])


def test_confirm_b_refuses_and_skips_decode(confirm_b):
    assert "REFUSING to delete" in confirm_b["result"].stdout
    assert "Decode check skipped" in confirm_b["result"].stdout
    assert "decoding" not in confirm_b["result"].stdout


def test_confirm_b_deletes_nothing(confirm_b, cd_prepared):
    source_root, _ = cd_prepared
    assert not (confirm_b["run_dir"] / "deletion_log.csv").exists()
    assert source_path(source_root, TIMES["t03"]).exists()


def test_confirm_destination_inside_own_source_is_an_error(tmp_path, clips):
    dest2 = tmp_path / "dest2"
    source_c = dest2 / "rat903"
    place(clips["R30"], source_path(source_c, "10-00-00", subject="rat903"))

    result = run_confirm_and_delete(source_c, dest2, extra_args=["--min_age_hours", "0"])
    assert result.returncode == 0, result.stderr

    run_dir = latest_run_dir(dest2, "confirm_")
    row = read_log(run_dir)[fname("10-00-00", subject="rat903")]
    assert row["status"] == "error"
    assert is_true(row["action_item"])
    assert "inside the source folder" in row["skipped_reason"]


def test_destination_equal_to_source_is_rejected_before_running(tmp_path):
    same_dir = tmp_path / "same"
    same_dir.mkdir()
    result = subprocess.run(
        [sys.executable, "confirm_and_delete.py", str(same_dir), str(same_dir), "--output_studyname", STUDYNAME],
        cwd=VIDEOPROC_DIR, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "ValueError" in result.stderr


def test_destination_inside_source_is_rejected_before_running(tmp_path):
    source_dir = tmp_path / "source"
    dest_dir = source_dir / "nested_dest"
    dest_dir.mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, "confirm_and_delete.py", str(source_dir), str(dest_dir), "--output_studyname", STUDYNAME],
        cwd=VIDEOPROC_DIR, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "ValueError" in result.stderr


@pytest.fixture(scope="module")
def confirm_d(cd_prepared, confirm_b):
    source_root, dest_root = cd_prepared
    result = run_confirm_and_delete(source_root, dest_root, extra_args=[
        "--min_age_hours", "0", "--delete_partial", "--decode",
    ], delete=True)
    assert result.returncode == 0, result.stderr
    run_dir = latest_run_dir(dest_root, "confirm_")
    return {"result": result, "run_dir": run_dir}


def test_confirm_d_deletes_exactly_the_three_verified_clips(confirm_d, cd_prepared):
    deletion_rows = read_rows(confirm_d["run_dir"] / "deletion_log.csv")
    assert len(deletion_rows) == 3

    source_root, _ = cd_prepared
    assert not source_path(source_root, TIMES["t03"]).exists()
    assert not source_path(source_root, T11_TIME, date=T11_DATE).exists()
    assert not source_path(source_root, HOM_TIME, view="hom").exists()


def test_confirm_d_removes_emptied_session_folders(confirm_d, cd_prepared):
    source_root, _ = cd_prepared
    assert not session_folder_path(source_root, date=T11_DATE).exists()
    assert not session_folder_path(source_root, view="hom").exists()


def test_confirm_d_keeps_session_folder_with_leftovers(confirm_d, cd_prepared):
    source_root, _ = cd_prepared
    default_folder = session_folder_path(source_root)
    assert default_folder.exists()
    assert any(default_folder.iterdir())
    assert source_path(source_root, TIMES["t01"]).exists()


def test_confirm_d_reports_remaining_files(confirm_d):
    assert "remain under" in confirm_d["result"].stdout
