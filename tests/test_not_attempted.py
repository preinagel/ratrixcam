"""compress_drive.main() logs every matched file as not_attempted if connectivity never
recovers once the run has started (the initial pre-run check succeeds; every per-file
recheck inside the loop fails)."""

import sys

import pytest

from helpers import STUDYNAME, SUBJECT_PREFIX, VIDEOPROC_DIR, latest_run_dir, manifest_path, place_clip, read_log

sys.path.insert(0, str(VIDEOPROC_DIR))
compress_drive = pytest.importorskip("compress_drive", exc_type=ImportError)


def test_connectivity_lost_after_start_logs_every_file_not_attempted(tmp_path, clips, monkeypatch):
    source_root, dest_root = tmp_path / "source", tmp_path / "dest"
    dest_root.mkdir()
    for time in ("10-01-00", "10-02-00", "10-03-00"):
        place_clip(clips["R30"], source_root, time)

    calls = {"n": 0}

    def fake_wait_for_connectivity(*_args):
        calls["n"] += 1
        return calls["n"] == 1  # the pre-run gate succeeds; every later check fails

    monkeypatch.setattr(compress_drive, "wait_for_connectivity", fake_wait_for_connectivity)

    compress_drive.main(
        input=source_root, output=dest_root, manifest=[manifest_path(source_root)],
        studyname=STUDYNAME, subject_prefix=SUBJECT_PREFIX,
        motion_percentile=99.9, motion_threshold=0.0, n_threads=4,
        compress_spd="veryfast", recompress=False,
        overwrite_raw=False, overwrite_compressed=False,
        motion_detection=False, motion_review=True,
    )

    run_dir = latest_run_dir(dest_root, "compression_")
    log = read_log(run_dir)

    assert len(log) == 3
    for row in log.values():
        assert row["status"] == "not_attempted"
        assert row["action_item"] == "True"
