"""The camera server's frame loop, run against a fake camera and a fake writer: every frame
is stamped with its own arrival time, the clips are cut by the clock and labelled with the
camera's rate, and each clip's frame-time file travels with it to the save drive."""

import csv
import multiprocessing
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
import numpy.typing as npt
import pytest

from test_calibration import OpenableFakeCapture
from test_run_metadata import make_config

from ratrixcam_utils import Calibration, CameraCalibration

if TYPE_CHECKING:  # the real module for the checker; at run time the suite skips without OpenCV
    import cv2

    import ratrixcam_server as server
else:
    server = pytest.importorskip("ratrixcam_server")
    cv2 = pytest.importorskip("cv2")


class FakeWriter:
    """Stands in for cv2.VideoWriter: remembers how it was opened, counts the frames, and
    leaves a small file at its path when released, so the transfer has something to move."""

    opened: list["FakeWriter"] = []

    def __init__(self, path: str, fourcc: int, fps: float, size: tuple[int, int]):
        self.path: str = path
        self.fps: float = fps
        self.size: tuple[int, int] = size
        self.frames: int = 0
        FakeWriter.opened.append(self)

    def write(self, _frame: object) -> None:
        self.frames += 1

    def release(self) -> None:
        with open(self.path, "wb") as f:
            _ = f.write(b"clip" * self.frames)


def run_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, frames: int, calibration: Calibration | None = None):
    """Run one camera server over a fake camera that delivers this many frames at 50 fps
    (the 3-s warm-up takes the first 150), with 1-s clips; the config and the writers it
    opened."""
    config, _ = make_config(tmp_path)
    config = config.model_copy(update={"time_slice": 1})
    FakeWriter.opened = []
    monkeypatch.setattr("ratrixcam_server.cv2.VideoCapture", lambda index: OpenableFakeCapture(fps=50, width=64, height=48, frames=frames))
    monkeypatch.setattr("ratrixcam_server.cv2.VideoWriter", FakeWriter)
    server.run(config, 0, multiprocessing.Event(), None, calibration)
    return config, FakeWriter.opened


def test_the_loop_cuts_clips_by_the_clock_and_stamps_each_frame_with_its_arrival(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config, writers = run_server(monkeypatch, tmp_path, frames=270)  # 150 in the warm-up, then 2.4 s: three clips
    out = capsys.readouterr().out
    assert "records at" in out and "from the launch measurement, uncalibrated" in out
    assert 2 <= len(writers) <= 3
    assert all(48 < w.fps < 52 and w.size == (64, 48) for w in writers)  # labelled with the measured rate, not the requested 30
    assert 45 <= writers[0].frames <= 55  # a 1-s clip at 50 fps

    save = Path(config.save_path)
    clips = sorted(save.glob("Test_subj001_cam1_*/*.mp4"))
    times = sorted(save.glob("Test_subj001_cam1_*/*_frames.csv"))
    assert len(clips) == len(writers) == len(times)  # every clip and its frame-time file arrived
    assert not list(Path(config.temp_path).glob("Test_subj001_cam1/*"))  # and left temp
    for clip, times_file in zip(clips, times):
        assert times_file.name == server.frame_times_filename(clip.name)
    with open(times[0]) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == writers[0].frames
    assert [int(r["frame"]) for r in rows] == list(range(len(rows)))  # the global counter, from 0
    arrivals = [r["arrived"] for r in rows]
    assert arrivals == sorted(arrivals) and arrivals[0] != arrivals[1]  # each frame's own time, increasing
    assert all(0 <= float(r["waited_s"]) < 0.1 and 0 <= float(r["worked_s"]) < 0.1 for r in rows)
    manifest = next(Path(config.temp_path).rglob("*_manifest.csv"))
    assert len(manifest.read_text().splitlines()) == len(writers) + 1  # a row per clip


def test_a_calibrated_camera_records_at_its_calibration_rate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    entry = CameraCalibration(
        index=0, name="subj001_cam1", position="A", requested_width=640, requested_height=480, requested_fps=30,
        width=64, height=48, rate=49.95, uncertainty=0.002, frames=15000, seconds=300.0, gaps=0,
    )
    calibration = Calibration(calibrated="2026-11-19 10:00:00", code_version="x", config_file="cfg.json", cameras=[entry])
    _, writers = run_server(monkeypatch, tmp_path, frames=190, calibration=calibration)
    assert "records at 49.950 fps (uncertainty 0.002) from the calibration of 2026-11-19 10:00:00" in capsys.readouterr().out
    assert writers and all(w.fps == 49.95 for w in writers)


def test_a_camera_without_an_entry_is_said_and_measured_instead(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    calibration = Calibration(calibrated="2026-11-19 10:00:00", code_version="x", config_file="cfg.json", cameras=[])
    _, writers = run_server(monkeypatch, tmp_path, frames=190, calibration=calibration)
    out = capsys.readouterr().out
    assert "WARNING: camera subj001_cam1 has no calibration for 640x480 at 30 fps under this name" in out
    assert "from the launch measurement, uncalibrated" in out
    assert writers and all(48 < w.fps < 52 for w in writers)


def test_the_frame_handed_to_the_writer_carries_the_overlay():
    """write_frame draws onto the frame it is given; a frame of zeros comes back with white pixels."""
    written: list[object] = []

    class Writer:
        def write(self, frame: object) -> None:
            written.append(frame)

    params = server.CameraParams(name="subj001_cam1", width=64, height=48, fps=30, cam_exposure=0.0, crf=25)
    frame: npt.NDArray[np.uint8] = np.zeros((48, 64, 3), dtype=np.uint8)
    server.write_frame(cast(cv2.VideoWriter, cast(object, Writer())), frame, params, datetime(2026, 1, 2, 3, 4, 5), "text")
    assert len(written) == 1 and written[0] is frame
    assert bool(frame.any())  # the text, anti-aliased, is the only thing on it
