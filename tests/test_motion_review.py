"""Motion review: peak stills for no-motion clips, the review flag for no-motion clips with
a sampled frame at the threshold, and which clips keep a motion time series."""

from pathlib import Path

from helpers import (
    fname, is_true, last_index_row, latest_run_dir, place_clip, read_log, read_rows, run_compress_drive,
)

MOVING, STATIC, BLIP = "10-00-00", "10-01-00", "10-02-00"
# 90th percentile: with 87 sampled frames per test clip, one frame over the threshold must
# not count as motion (at 99.9 it would, see the manual)
ARGS = ["--n_threads", "4", "--motion_detection", "--motion_threshold", "0.001", "--motion_percentile", "90"]


def _run(tmp_path, clips, extra=()):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    place_clip(clips["R30"], source_root, MOVING)
    place_clip(clips["R30_static"], source_root, STATIC)
    place_clip(clips["R30_blip"], source_root, BLIP)
    result = run_compress_drive(source_root, dest_root, extra_args=[*ARGS, *extra])
    assert result.returncode == 0, result.stderr
    return result, latest_run_dir(dest_root, "compression_")


def test_review_classifies_moving_static_and_blip(tmp_path, clips):
    result, run_dir = _run(tmp_path, clips)
    log = read_log(run_dir)
    moving, static, blip = log[fname(MOVING)], log[fname(STATIC)], log[fname(BLIP)]

    assert is_true(moving["found_motion"]) and moving["motion_review_flag"] == ""
    assert not is_true(static["found_motion"]) and not is_true(static["motion_review_flag"])
    assert static["n_frames_over_threshold"] == "0"
    assert not is_true(blip["found_motion"]) and is_true(blip["motion_review_flag"])
    assert int(blip["n_frames_over_threshold"]) >= 1
    assert float(blip["motion_max"]) >= 0.001

    stills = {p.name for p in (run_dir / "no_motion_stills").glob("*.jpg")}
    assert len(stills) == 2
    assert any(name.startswith(Path(fname(STATIC)).stem) for name in stills)
    assert any(name.startswith(Path(fname(BLIP)).stem) for name in stills)
    assert not any(name.startswith(Path(fname(MOVING)).stem) for name in stills)

    series = {row["clip_filename"] for row in read_rows(run_dir / "motion_timeseries.csv")}
    assert series == {fname(MOVING), fname(BLIP)}

    review = read_rows(run_dir / "motion_review.csv")
    assert [row["clip_filename"] for row in review] == [fname(BLIP)]
    still = Path(review[0]["still_path"])
    assert still.exists() and still.name.endswith(f"_frame{review[0]['peak_frame_number']}.jpg")

    assert "no-motion clips: 2" in result.stdout
    assert "flagged for review (>= 1 sampled frame at the threshold): 1 (home 1)" in result.stdout
    assert "motion review ON" in result.stdout


def test_review_off_keeps_every_timeseries_and_no_stills(tmp_path, clips):
    result, run_dir = _run(tmp_path, clips, extra=["--no-motion_review"])
    assert not (run_dir / "no_motion_stills").exists()
    assert not (run_dir / "motion_review.csv").exists()
    series = {row["clip_filename"] for row in read_rows(run_dir / "motion_timeseries.csv")}
    assert series == {fname(MOVING), fname(STATIC), fname(BLIP)}
    log = read_log(run_dir)
    assert log[fname(BLIP)]["motion_review_flag"] == ""
    assert "motion review OFF" in result.stdout
    assert last_index_row(run_dir.parents[1])["motion_threshold"] == "0.001"
