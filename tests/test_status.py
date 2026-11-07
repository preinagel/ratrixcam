"""The status file the viewer reads (ratrixcam_status): every field always present, written
atomically, the drive facts from python's own rules, one terminal line per transition."""

import json
import os
import platform
import shutil
import sys
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from helpers import VIDEOPROC_DIR

sys.path.insert(0, str(VIDEOPROC_DIR.parent))
import ratrixcam_status  # noqa: E402
from ratrixcam_naming import STATUS_FILENAME  # noqa: E402
from ratrixcam_status import StatusWriter  # noqa: E402
from ratrixcam_utils import write_rate  # noqa: E402
from test_run_metadata import make_config  # noqa: E402

Usage = namedtuple("Usage", "total used free")
STARTED = datetime(2026, 1, 2, 3, 4, 5)


def writer(tmp_path: Path, **config_changes: object) -> StatusWriter:
    config, _ = make_config(tmp_path)
    config = config.model_copy(update=config_changes)
    return StatusWriter(config, "ratrixcam_20260102_030405_abcdef01", STARTED)


def read(w: StatusWriter) -> Any:
    return json.loads(w.path.read_text())


def snap(w: StatusWriter, cameras_alive: bool) -> Any:
    return w.snapshot(cameras_alive)


def test_the_file_has_every_field_and_no_temporary_name_left(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    w.write(cameras_alive=True, force=True)
    status = read(w)
    assert status["schema"] == 1
    assert status["recording"] is True
    assert sorted(status["run"]) == ["machine_name", "message", "run_id", "started", "state"]
    assert status["run"]["run_id"] == "ratrixcam_20260102_030405_abcdef01"
    assert status["run"]["started"] == "2026-01-02 03:04:05.000"
    assert status["run"]["state"] == "running" and status["run"]["message"] == ""
    assert status["run"]["machine_name"] == platform.node()  # no machine_name in the config
    for drive in ("save_drive", "temp_drive"):
        assert sorted(status[drive]) == sorted(["path", "found", "total_bytes", "used_bytes", "free_bytes", "seconds_until_full"] + (["accepting_clips"] if drive == "save_drive" else ["clips_waiting"]))
        assert status[drive]["found"] is True and status[drive]["free_bytes"] > 0
    assert status["save_drive"]["accepting_clips"] is True
    assert status["temp_drive"]["clips_waiting"] == 0
    assert sorted(p.name for p in Path(w.config.temp_path).iterdir()) == [STATUS_FILENAME, "status"]  # no .tmp beside it
    assert capsys.readouterr().out == ""  # a healthy drive says nothing


def test_machine_name_comes_from_the_config(tmp_path: Path):
    w = writer(tmp_path, machine_name="Mac 4")
    w.write(cameras_alive=True, force=True)
    assert read(w)["run"]["machine_name"] == "Mac 4"


def test_seconds_until_full_uses_the_rate_scaled_to_the_camera_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - 6 * 10**9, 6 * 10**9))
    w = writer(tmp_path)  # one camera
    status = snap(w, True)
    assert status["save_drive"]["seconds_until_full"] == round(6 * 10**9 / write_rate(1))
    assert status["save_drive"]["seconds_until_full"] == 8 * round(6 * 10**9 / write_rate(8))


def test_a_missing_save_drive_is_reported_once_and_its_return_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    shutil.rmtree(w.config.save_path)
    w.write(cameras_alive=True, force=True)
    status = read(w)
    assert status["save_drive"]["found"] is False and status["save_drive"]["accepting_clips"] is False
    assert status["save_drive"]["total_bytes"] is None and status["save_drive"]["seconds_until_full"] is None
    w.write(cameras_alive=True, force=True)  # still missing: no second line
    os.mkdir(w.config.save_path)
    w.write(cameras_alive=True, force=True)
    assert read(w)["save_drive"]["found"] is True
    out = capsys.readouterr().out
    assert out.count("WARNING: save drive not found") == 1
    assert out.count("Save drive back") == 1


def test_not_accepting_when_the_write_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    monkeypatch.setattr(ratrixcam_status, "PROBE_INTERVAL", 0)  # a probe on every write here
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: "Read-only file system")
    w.write(cameras_alive=True, force=True)
    assert read(w)["save_drive"]["found"] is True and read(w)["save_drive"]["accepting_clips"] is False
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: None)
    w.write(cameras_alive=True, force=True)
    assert read(w)["save_drive"]["accepting_clips"] is True
    out = capsys.readouterr().out
    assert out.count("WARNING: save drive no longer accepting clips") == 1
    assert out.count("Save drive accepting clips again") == 1


def test_not_accepting_when_free_space_is_under_the_transfer_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    cam = Path(w.config.temp_path) / "Test_cam"
    cam.mkdir()
    (cam / "a.mp4").write_bytes(b"x" * 2_000_000)  # the largest clip sets the size a transfer needs ten times over
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - 19_000_000, 19_000_000))
    assert snap(w, True)["save_drive"]["accepting_clips"] is False
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - 20_000_000, 20_000_000))
    assert snap(w, True)["save_drive"]["accepting_clips"] is True


def test_with_no_clip_in_temp_the_assumed_clip_size_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)  # one camera, time_slice 60: a clip is write_rate(1) * 60 bytes
    clip = write_rate(1) * 60
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - int(clip * 9), int(clip * 9)))
    assert snap(w, True)["save_drive"]["accepting_clips"] is False
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - int(clip * 11), int(clip * 11)))
    assert snap(w, True)["save_drive"]["accepting_clips"] is True


def test_clips_waiting_excludes_the_open_clip_of_each_running_camera(tmp_path: Path):
    w = writer(tmp_path)  # one camera
    cam = Path(w.config.temp_path) / "Test_cam"
    cam.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (cam / name).write_bytes(b"x")
    (Path(w.config.temp_path) / "ratrixcam_run_metadata").mkdir()
    (Path(w.config.temp_path) / "ratrixcam_run_metadata" / "not_a_clip.mp4").write_bytes(b"x")  # the metadata folder is not a camera folder
    assert snap(w, True)["temp_drive"]["clips_waiting"] == 2
    assert snap(w, False)["temp_drive"]["clips_waiting"] == 3


def test_writes_are_limited_to_one_per_preview_interval_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)  # preview_interval 1 s
    replaced: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append(str(b)), real_replace(a, b)))
    w.write(cameras_alive=True)
    w.write(cameras_alive=True)
    assert len(replaced) == 1
    w.write(cameras_alive=True, state="stopping", force=True)
    assert len(replaced) == 2
    assert read(w)["run"]["state"] == "stopping"


def test_a_state_given_to_a_limited_write_is_kept_for_the_next(tmp_path: Path):
    w = writer(tmp_path)
    w.write(cameras_alive=True, force=True)
    w.write(cameras_alive=True, state="stopping", message="m")  # within the interval: nothing written
    assert read(w)["run"]["state"] == "running"
    w.write(cameras_alive=True, force=True)
    assert read(w)["run"]["state"] == "stopping" and read(w)["run"]["message"] == "m"


def test_a_closed_writer_writes_nothing_and_remove_takes_the_file(tmp_path: Path):
    w = writer(tmp_path)
    w.write(cameras_alive=True, force=True)
    w.close()
    w.path.unlink()
    w.write(cameras_alive=True, force=True)
    assert not w.path.exists()
    w.remove()  # already gone: silent
    assert not w.path.exists()


def test_an_unwritable_temp_folder_is_reported_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)

    def no_room(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("builtins.open", no_room)
    w.write(cameras_alive=True, force=True)
    w.write(cameras_alive=True, force=True)
    assert capsys.readouterr().out.count("WARNING: could not write the status file") == 1


def test_the_write_probe_runs_once_per_interval_and_at_once_after_the_drive_was_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    probes: list[int] = []
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: (probes.append(1), None)[1])
    _ = snap(w, True)
    _ = snap(w, True)
    assert len(probes) == 1  # within the interval: the first result is reused
    shutil.rmtree(w.config.save_path)
    _ = snap(w, True)  # missing: no probe
    os.mkdir(w.config.save_path)
    _ = snap(w, True)  # back: probed at once
    assert len(probes) == 2


def test_a_drive_back_but_read_only_reports_both_transitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    monkeypatch.setattr(ratrixcam_status, "PROBE_INTERVAL", 0)
    shutil.rmtree(w.config.save_path)
    w.write(cameras_alive=True, force=True)  # gone: one line, "not accepting" goes without saying
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: "Read-only file system")
    os.mkdir(w.config.save_path)
    w.write(cameras_alive=True, force=True)  # back, but read-only: both lines
    out = capsys.readouterr().out
    assert out.count("WARNING: save drive not found") == 1
    assert out.count("Save drive back") == 1
    assert out.count("WARNING: save drive no longer accepting clips") == 1
