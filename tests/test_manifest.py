"""The per-clip manifest a camera server appends to as it records (ratrixcam_manifest)."""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

from helpers import VIDEOPROC_DIR

sys.path.insert(0, str(VIDEOPROC_DIR.parent))
import ratrixcam_manifest  # noqa: E402
from ratrixcam_manifest import MANIFEST_COLUMNS, ManifestMetadata  # noqa: E402

TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")


def test_header_once_then_one_row_per_clip(tmp_path):
    m = ManifestMetadata(str(tmp_path), "rat999_home", "999", "home", 25)
    assert Path(m.path) == tmp_path / "rat999_home_manifest.csv"
    assert m.append("Test_rat999_home_20261019", "rat999_home_20261019_15-23-29.mp4", datetime(2026, 10, 19, 15, 23, 29, 123456))
    assert m.append("Test_rat999_home_20261019", "rat999_home_20261019_15-24-29.mp4", datetime(2026, 10, 19, 15, 24, 29))

    with open(m.path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == MANIFEST_COLUMNS
    assert len(rows) == 3
    assert rows[1][:4] == [
        "Test_rat999_home_20261019/rat999_home_20261019_15-23-29.mp4", "999", "home", "2026-10-19 15:23:29.123",
    ]
    assert rows[2][3] == "2026-10-19 15:24:29.000"
    for row in rows[1:]:
        assert TIME.fullmatch(row[4]), row  # row_written
        assert row[5] == "25"


def test_write_failure_warns_and_returns_false(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ratrixcam_manifest, "RETRY_DELAY", 0)
    m = ManifestMetadata(str(tmp_path / "no_such_folder"), "rat999_home", "999", "home", 25)
    assert m.append("Test_rat999_home_20261019", "clip.mp4", datetime.now()) is False
    out = capsys.readouterr().out
    assert "WARNING" in out and "clip.mp4" in out and "3 attempts" in out
    assert not Path(m.path).exists()
