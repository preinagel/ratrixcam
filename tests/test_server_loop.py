"""The camera server's frame loop, run against a fake camera and a fake writer: every frame
is stamped with its own arrival time, the clips are cut by the clock and labelled with the
camera's rate, each clip's frame-time file travels with it to the save drive, a hole in the
arrivals is filled slot by slot, and a silent camera is given up on by the clock."""

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

from ratrixcam_naming import frame_times_filename
from ratrixcam_utils import Calibration, CameraCalibration, Config

if TYPE_CHECKING:  # the real module for the checker; at run time the suite skips without OpenCV
    import cv2

    import ratrixcam_server as server
else:
    server = pytest.importorskip("ratrixcam_server")
    cv2 = pytest.importorskip("cv2")


class FakeWriter:
    """Stands in for cv2.VideoWriter: remembers how it was opened, keeps a copy of every
    frame, and leaves a small file at its path when released, so the transfer has something
    to move."""

    opened: list["FakeWriter"] = []

    def __init__(self, path: str, fourcc: int, fps: float, size: tuple[int, int]):
        self.path: str = path
        self.fps: float = fps
        self.size: tuple[int, int] = size
        self.written: list[npt.NDArray[np.uint8]] = []
        FakeWriter.opened.append(self)

    @property
    def frames(self) -> int:
        return len(self.written)

    def write(self, frame: npt.NDArray[np.uint8]) -> None:
        self.written.append(frame.copy())

    def release(self) -> None:
        with open(self.path, "wb") as f:
            _ = f.write(b"clip" * self.frames)


def run_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, frames: int, calibration: Calibration | None = None,
    silences: dict[int, float] | None = None, give_up: float = 1.0,
):
    """Run one camera server over a fake camera that delivers this many frames at 50 fps
    (the 3-s warm-up takes the first 150), with 1-s clips, optionally silent for a while
    after given frames; the config and the writers it opened. The end of the frames looks
    like a freeze to the server, so the give-up is short unless a test needs a longer one,
    and the last clip of every run is filled to its boundary."""
    config, _ = make_config(tmp_path)
    config = config.model_copy(update={"time_slice": 1})
    FakeWriter.opened = []
    monkeypatch.setattr("ratrixcam_server.cv2.VideoCapture", lambda index: OpenableFakeCapture(fps=50, width=64, height=48, frames=frames, silences=silences))
    monkeypatch.setattr("ratrixcam_server.cv2.VideoWriter", FakeWriter)
    monkeypatch.setattr(server, "GIVE_UP_SECONDS", give_up)
    monkeypatch.setattr(server, "rig_settled_at", lambda *_args: 0.0)  # every clip is compared by the watch
    server.run(config, 0, multiprocessing.Event(), None, calibration)
    return config, FakeWriter.opened


def sidecars(config: Config) -> list[list[dict[str, str]]]:
    """The rows of every clip's frame-time file on the save drive, clip by clip."""
    save = Path(config.save_path)
    out: list[list[dict[str, str]]] = []
    for path in sorted(save.glob("Test_subj001_cam1_*/*_frames.csv")):
        with open(path) as f:
            out.append(list(csv.DictReader(f)))
    return out


CALIBRATED = Calibration(
    calibrated="2026-11-19 10:00:00", code_version="x", config_file="cfg.json",
    cameras=[CameraCalibration(
        index=0, name="subj001_cam1", position="A", requested_width=640, requested_height=480, requested_fps=30,
        width=64, height=48, rate=50.0, uncertainty=0.002, frames=15000, seconds=300.0, gaps=0,
    )],
)


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
        assert times_file.name == frame_times_filename(clip.name)
    with open(times[0]) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == writers[0].frames
    assert [int(r["frame"]) for r in rows] == list(range(len(rows)))  # the written counter, from 0
    assert all(r["kind"] == "frame" for r in rows)  # a steady camera: no filler
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


# ------------------------------------------------- holes, freezes and the give-up

def test_a_hole_of_three_intervals_gets_a_duplicate_and_a_blank(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # frame 175 is followed by 40 ms of silence: the next frame comes 60 ms after it, three intervals at 50 fps
    config, writers = run_server(monkeypatch, tmp_path, frames=230, silences={175: 0.04})
    rows = sidecars(config)[0]  # the first clip holds the hole; the last clip of any run is filled to its boundary
    kinds = [r["kind"] for r in rows]
    assert kinds.count("duplicate") == 1 and kinds.count("blank") == 1
    i = kinds.index("duplicate")
    assert kinds[i - 1 : i + 3] == ["frame", "duplicate", "blank", "frame"]
    assert [int(r["frame"]) for r in rows] == list(range(len(rows)))  # the written counter counts the fillers
    assert rows[i]["waited_s"] == "" and rows[i]["worked_s"] == ""  # a filler's row: its slot time only
    before = datetime.fromisoformat(rows[i - 1]["arrived"]); slot = datetime.fromisoformat(rows[i]["arrived"])
    assert 0.015 < (slot - before).total_seconds() < 0.025  # the slot after the last written one
    frames = writers[0].written
    assert np.array_equal(frames[i], frames[i - 1])  # the duplicate: the previous frame's pixels, label and all
    assert not frames[i + 1].any()  # the blank: nothing on it


def test_a_hole_of_two_intervals_is_not_filled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # frame 175 is followed by 20 ms of silence: a two-interval gap, a pacing pause or a single loss, left alone
    config, _ = run_server(monkeypatch, tmp_path, frames=230, silences={175: 0.02})
    assert all(r["kind"] == "frame" for r in sidecars(config)[0])


def test_a_freeze_is_filled_as_it_goes_and_the_boundary_inside_it_closes_the_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    # 3.5 s of silence after frame 175 (0.5 s into the first clip, 1-s clips): the clip closes by the clock
    # during the silence, filled to its boundary; the next clip opens at the frame that resumes, unfilled
    config, writers = run_server(monkeypatch, tmp_path, frames=260, silences={175: 3.5}, give_up=5.0)
    out = capsys.readouterr().out
    assert "has delivered no frame for 3 s" in out and "delivers frames again after 3.5 s" in out
    assert out.index("delivers frames again") < out.index("presumed offline")  # the give-up came only at the end of the frames
    clips = sidecars(config)
    frozen = clips[0]
    kinds = [r["kind"] for r in frozen]
    arrived = kinds.count("frame")
    assert 20 <= arrived <= 30 and kinds.count("duplicate") == 1 and 18 <= kinds.count("blank") <= 30  # filled to the 1-s boundary
    assert 47 <= len(kinds) <= 56  # about a second of slots at 50 fps, arrived and fillers together (the fake camera
    # hands out a small backlog from the warm-up without waiting, so the arrived part can run a little over)
    assert kinds[-1] in ("blank", "duplicate")  # the clip ended inside the silence
    assert all(r["kind"] == "frame" for r in clips[1][:5])  # the next clip starts at the resumed frame, no filler before it
    assert f"delivered {arrived} frames in a 1.0-s clip" in out  # the watch counts arrived frames, not written ones
    assert writers[0].frames == len(frozen)


def test_a_camera_silent_past_the_give_up_is_presumed_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config, writers = run_server(monkeypatch, tmp_path, frames=400, silences={175: 4.0}, give_up=2.0)
    out = capsys.readouterr().out
    assert "no frame from camera subj001_cam1 for 2 s" in out and "presumed offline" in out
    assert "shutdown complete" in out
    assert len(writers) == 1 and sidecars(config)  # the clip in progress was closed and transferred


def test_single_losses_are_not_filled_and_the_clip_stays_short(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    # three single losses in the first clip, each a two-interval gap: nothing fills them, in the loop or at
    # the close; the clip is short and the watch says so with the arrived count
    config, _ = run_server(monkeypatch, tmp_path, frames=230, silences={160: 0.02, 170: 0.02, 180: 0.02}, calibration=CALIBRATED)
    first = sidecars(config)[0]
    assert all(r["kind"] == "frame" for r in first)
    assert 46 <= len(first) <= 49
    assert f"delivered {len(first)} frames in a 1.0-s clip" in capsys.readouterr().out


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
