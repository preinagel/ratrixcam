"""The frame-rate calibration: the file the calibration program writes, what the launcher
accepts from it, and the measurement of one camera on its own."""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from test_camera_measure import FakeCapture
from test_run_metadata import make_config

from ratrixcam_utils import Calibration, CameraCalibration, calibration_path, load_calibration

if TYPE_CHECKING:  # the real module for the checker; at run time the suite skips without OpenCV
    import ratrixcam_calibrate as calibrate
else:
    calibrate = pytest.importorskip("ratrixcam_calibrate")


def an_entry(**changes: object) -> CameraCalibration:
    fields: dict[str, object] = dict(
        index=0, name="subj001_cam1", position="A", requested_width=640, requested_height=480, requested_fps=30,
        width=640, height=480, rate=29.603, uncertainty=0.004, frames=8882, seconds=300.1, gaps=0,
    )
    fields.update(changes)
    return CameraCalibration.model_validate(fields)


def test_the_calibration_file_round_trips_and_is_matched_by_index_name_and_mode(tmp_path: Path):
    calibration = Calibration(calibrated="2026-11-19 10:00:00", code_version="abc1234", config_file="cfg.json", cameras=[an_entry()])
    path = tmp_path / "cfg_fps_calibration.json"
    _ = path.write_text(calibration.model_dump_json(indent=2))
    loaded = load_calibration(str(path))
    assert loaded == calibration
    assert loaded is not None
    assert loaded.for_camera(0, "subj001_cam1", 640, 480, 30) == an_entry()
    assert loaded.for_camera(1, "subj001_cam1", 640, 480, 30) is None  # another index
    assert loaded.for_camera(0, "subj002_cam1", 640, 480, 30) is None  # another name: the config changed
    assert loaded.for_camera(0, "subj001_cam1", 1920, 1080, 30) is None  # another mode needs its own calibration


def test_a_calibration_file_that_fails_its_checks_is_refused_with_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "bad.json"
    _ = path.write_text(json.dumps({"calibrated": "x", "code_version": "y", "config_file": "z", "cameras": [{"index": 0}]}))
    assert load_calibration(str(path)) is None
    out = capsys.readouterr().out
    assert "ERROR: the calibration file" in out and "cameras.0.name" in out
    assert load_calibration(str(tmp_path / "missing.json")) is None
    assert "ERROR: cannot read the calibration file" in capsys.readouterr().out


def test_the_calibration_sits_beside_the_config_named_after_it():
    assert calibration_path("/some/where/camera_config.json") == "/some/where/camera_config_fps_calibration.json"


class OpenableFakeCapture(FakeCapture):
    """The fake with what the calibration program also calls: set(), isOpened(), release()."""

    def __init__(self, opened: bool = True, **kwargs: object):
        super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]
        self.opened: bool = opened
        self.released: bool = False

    def set(self, _prop: int, _value: float) -> bool:
        return True

    def isOpened(self) -> bool:
        return self.opened

    def release(self) -> None:
        self.released = True


def test_a_camera_is_measured_on_its_own_and_its_entry_names_the_mode_requested(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config, _ = make_config(tmp_path)
    opened: list[int] = []

    def fake_capture(index: int) -> OpenableFakeCapture:
        opened.append(index)
        return OpenableFakeCapture(fps=50, width=64, height=48, reported=(50.0, 64, 48))

    monkeypatch.setattr("ratrixcam_calibrate.cv2.VideoCapture", fake_capture)
    entry = calibrate.calibrate_camera(config, 0, discard=0.2, measure=0.6)
    assert opened == [0] and entry is not None
    assert (entry.index, entry.name, entry.position) == (0, "subj001_cam1", "A")
    assert (entry.requested_width, entry.requested_height, entry.requested_fps) == (640, 480, 30)
    assert (entry.width, entry.height) == (64, 48) and 48 < entry.rate < 52 and entry.uncertainty < 1
    assert entry.frames >= 20 and 0.5 < entry.seconds < 2 and entry.gaps == 0
    out = capsys.readouterr().out
    assert "settling for 0 s" in out and "measuring for 1 s" in out
    assert "WARNING: camera subj001_cam1 delivers 64x48, not the 640x480 requested" in out


def test_a_camera_that_does_not_open_is_left_out_and_the_file_still_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config, config_path = make_config(tmp_path)
    monkeypatch.setattr("ratrixcam_calibrate.cv2.VideoCapture", lambda index: OpenableFakeCapture(opened=False, fps=50, width=64, height=48))
    calibration = calibrate.calibrate(config, str(config_path), discard=0.1, measure=0.1)
    assert calibration.cameras == [] and calibration.config_file == "camera_config_in_use.json"
    assert "WARNING: camera subj001_cam1 did not open at index 0; left out of the calibration" in capsys.readouterr().out
    assert Calibration.model_validate_json(calibration.model_dump_json()) == calibration  # what main() writes loads back


def test_the_launcher_keeps_the_calibration_with_the_config_copy(tmp_path: Path):
    from ratrixcam_run_metadata import RunMetadata

    config, config_path = make_config(tmp_path)
    calibration_file = tmp_path / "camera_config_in_use_fps_calibration.json"
    _ = calibration_file.write_text("{}")
    run = RunMetadata(config, config_path, calibration_path=calibration_file)
    run.record_start()
    for folder in (run.temp_run_dir, run.run_dir):
        assert (folder / calibration_file.name).read_text() == "{}"
        assert (folder / os.path.basename(config_path)).exists()
