"""The per-clip manifest a camera server appends to as it records (ratrixcam_manifest)."""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from helpers import VIDEOPROC_DIR

sys.path.insert(0, str(VIDEOPROC_DIR.parent))
import ratrixcam_manifest  # noqa: E402
from ratrixcam_manifest import MANIFEST_COLUMNS, ManifestMetadata  # noqa: E402
from ratrixcam_naming import manifest_warning, parse_manifest_warning  # noqa: E402

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
    tagged = [parse_manifest_warning(line) for line in out.splitlines()]
    tagged = [t for t in tagged if t is not None]
    assert len(tagged) == 1
    assert tagged[0]["manifest"] == "rat999_home_manifest.csv"
    assert tagged[0]["path"] == "Test_rat999_home_20261019/clip.mp4"
    assert tagged[0]["crf"] == "25"


def test_manifest_warning_round_trip():
    line = manifest_warning("rat999_home_manifest.csv", "Test_rat999_home_20261021/rat999_home_20261021_08-51-48.mp4", "999", "home", "2026-10-21 08:51:48.297", 25)
    assert line.startswith("WARNING MANIFEST_ROW_NOT_WRITTEN ")
    assert parse_manifest_warning(line) == {
        "manifest": "rat999_home_manifest.csv",
        "path": "Test_rat999_home_20261021/rat999_home_20261021_08-51-48.mp4",
        "subjID": "999", "view": "home", "clip_start": "2026-10-21 08:51:48.297", "crf": "25",
    }
    assert parse_manifest_warning("prefix from another process " + line + "\n") is not None  # interleaved output
    assert parse_manifest_warning("WARNING: could not add clip.mp4 to the manifest after 3 attempts") is None
    assert parse_manifest_warning("Manifest rows added at cleanup: 2") is None


def test_retry_succeeds_once_the_folder_appears(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "late_folder"
    monkeypatch.setattr(ratrixcam_manifest, "time", SimpleNamespace(sleep=lambda _s: folder.mkdir()))
    m = ManifestMetadata(str(folder), "rat999_home", "999", "home", 25)
    assert m.append("Test_rat999_home_20261019", "clip.mp4", datetime.now()) is True
    assert "WARNING" not in capsys.readouterr().out
    assert Path(m.path).read_text().count("\n") == 2  # header + one row


def test_close_writer_still_transfers_the_clip_when_the_manifest_fails(tmp_path, monkeypatch, capsys):
    import ratrixcam_server

    class StandInWriter:
        def release(self):
            pass

    monkeypatch.setattr(ratrixcam_manifest, "RETRY_DELAY", 0)
    temp_dir, save_dir = tmp_path / "temp" / "Test_rat999_home", tmp_path / "save" / "Test_rat999_home_20261021"
    temp_dir.mkdir(parents=True)
    save_dir.mkdir(parents=True)
    (temp_dir / "clip.mp4").write_bytes(b"clip")
    manifest = ManifestMetadata(str(tmp_path / "temp" / "no_such_folder"), "rat999_home", "999", "home", 25)
    state = ratrixcam_server.WriterState(
        StandInWriter(),  # pyright: ignore[reportArgumentType]  (only release() is called)
        str(save_dir), str(temp_dir), "clip.mp4", datetime.now(),
    )
    transfers = []

    ratrixcam_server.close_writer(state, transfers, manifest)

    assert "WARNING" in capsys.readouterr().out
    assert len(transfers) == 1
    transfers[0].join(30)
    assert (save_dir / "clip.mp4").read_bytes() == b"clip"
    assert not (temp_dir / "clip.mp4").exists()
    assert not Path(manifest.path).exists()  # the clip is on disk with no manifest row
