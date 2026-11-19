"""measure_camera: what a camera really delivers at launch, counted from its frames, next
to what the driver reports; the writer is opened with the measured values. RateWatch: a
camera whose clips depart from that rate during the run is reported once."""

import os
import time
from typing import TYPE_CHECKING, cast

import numpy as np
import numpy.typing as npt
import pytest

if TYPE_CHECKING:  # the real module for the checker; at run time the suite skips without OpenCV
    import cv2

    import ratrixcam_server as server
else:
    server = pytest.importorskip("ratrixcam_server")
    cv2 = pytest.importorskip("cv2")


class FakeCapture:
    """Frames of a given size at a given rate, as cv2.VideoCapture.read() would hand them
    out: each read blocks until the next frame is due. `repeat` makes every frame come back
    a second time, as a backend handing out its latest frame again."""

    def __init__(
        self, fps: float, width: int, height: int, reported: tuple[float, int, int] = (0.0, 0, 0),
        repeat: bool = False, frames: int | None = None, drop_every: int = 0,
    ):
        self.interval: float = 1 / fps
        self.drop_every: int = drop_every  # every n-th frame is lost, as when the reader was busy
        self.width: int = width
        self.height: int = height
        self.reported: tuple[float, int, int] = reported
        self.repeat: bool = repeat
        self.frames_left: int | None = frames
        self.n: int = 0
        self.next_at: float = time.monotonic()
        self.last: npt.NDArray[np.uint8] | None = None

    def get(self, prop: int) -> float:
        import cv2

        return {cv2.CAP_PROP_FPS: self.reported[0], cv2.CAP_PROP_FRAME_WIDTH: self.reported[1], cv2.CAP_PROP_FRAME_HEIGHT: self.reported[2]}[prop]

    def read(self):
        if self.frames_left is not None and self.frames_left <= 0:
            time.sleep(self.interval)
            return False, None
        if self.repeat and self.last is not None and self.n % 2 == 1:
            self.n += 1
            return True, self.last  # the same frame again, at once
        delay = self.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_at += self.interval
        self.n += 1
        if self.drop_every and self.n % self.drop_every == 0:
            self.next_at += self.interval  # that frame went by unread
        if self.frames_left is not None:
            self.frames_left -= 1
        self.last = np.full((self.height, self.width, 3), self.n % 256, dtype=np.uint8)
        return True, self.last


def capture(fake: FakeCapture) -> cv2.VideoCapture:
    """The stand-in handed to measure_camera, which only calls read() and get()."""
    return cast(cv2.VideoCapture, cast(object, fake))


def test_the_rate_and_size_come_from_the_frames_not_the_driver():
    cap = capture(FakeCapture(fps=50, width=64, height=48, reported=(30.0, 640, 480)))
    m = server.measure_camera(cap, seconds=0.5)
    assert m is not None
    assert 45 <= m.fps <= 55
    assert round(m.rate) == m.fps  # the unrounded rate is kept beside the label
    assert (m.width, m.height) == (64, 48)
    assert (m.reported_fps, m.reported_width, m.reported_height) == (30.0, 640, 480)
    assert m.frames >= 20


def test_a_frame_handed_back_twice_is_counted_once():
    cap = capture(FakeCapture(fps=50, width=64, height=48, repeat=True))
    m = server.measure_camera(cap, seconds=0.5)
    assert m is not None
    assert 45 <= m.fps <= 55


def test_no_frame_at_all_is_none():
    cap = capture(FakeCapture(fps=50, width=64, height=48, frames=0))
    assert server.measure_camera(cap, seconds=0.2, first_frame_wait=0.2) is None


def test_the_first_frame_is_awaited_for_its_own_allowance():
    fake = FakeCapture(fps=50, width=64, height=48)
    fake.next_at += 0.4  # a slow starter: the first frame comes after 0.4 s
    m = server.measure_camera(capture(fake), seconds=0.3, first_frame_wait=1.0)
    assert m is not None and 45 <= m.fps <= 55


def test_a_single_frame_gives_a_size_but_no_rate():
    cap = capture(FakeCapture(fps=50, width=64, height=48, frames=1))
    m = server.measure_camera(cap, seconds=0.2, first_frame_wait=0.2)
    assert m is not None
    assert m.fps == 0 and (m.width, m.height) == (64, 48) and m.frames == 1  # the server treats a rate of 0 as no camera


def test_a_frame_is_handed_out_for_the_still_at_the_given_interval():
    cap = capture(FakeCapture(fps=50, width=64, height=48))
    stills: list[tuple[int, ...]] = []
    m = server.measure_camera(cap, seconds=0.5, on_frame=lambda f: stills.append(np.shape(f)), still_every=0.1)
    assert m is not None
    assert 4 <= len(stills) <= 6  # one at the first frame, then every 0.1 s of a 0.5 s warm-up
    assert stills[0] == (48, 64, 3)


def test_frames_the_reader_missed_do_not_lower_the_rate():
    cap = capture(FakeCapture(fps=50, width=64, height=48, drop_every=4))  # a quarter of the frames lost: a count would say 37
    m = server.measure_camera(cap, seconds=0.5)
    assert m is not None
    assert 45 <= m.fps <= 55


def test_a_frame_that_never_arrived_is_a_gap_and_widens_nothing():
    rate, uncertainty, gaps = server.rate_from_intervals([0.02] * 50 + [0.04, 0.02, 0.02])
    assert 49.9 < rate < 50.1 and gaps == 1
    assert uncertainty < 0.01  # identical intervals: the mean is exact; the doubled one is left out


def test_a_single_interval_has_a_rate_but_no_measure_of_its_error():
    rate, uncertainty, gaps = server.rate_from_intervals([0.025])
    assert rate == 40 and uncertainty == rate and gaps == 0
    assert server.rate_from_intervals([]) == (0.0, 0.0, 0)


# ------------------------------------------------- the frame count of each clip against the camera's rate

def calibrated(fps: float, uncertainty: float = 0.004) -> server.Rate:
    return server.Rate(fps, uncertainty, "the test")

def test_the_rig_is_settled_once_the_last_camera_has_started_its_second_clip():
    from ratrixcam_utils import CAMERA_LAUNCH_PAUSE

    launch = CAMERA_LAUNCH_PAUSE
    first_clip = server.FIRST_FRAME_WAIT + server.WARM_UP_SECONDS  # what a camera spends before its first clip
    # the first of seven cameras, 60-s clips: the last camera starts six pauses later
    assert server.rig_settled_at(1000.0, 0, 7, 60) == 1000.0 + 6 * launch + first_clip + 60
    # the last camera: nobody launches after it
    assert server.rig_settled_at(1000.0, 6, 7, 60) == 1000.0 + first_clip + 60
    # a one-camera rig with 10-minute clips: its own second clip
    assert server.rig_settled_at(1000.0, 0, 1, 600) == 1000.0 + first_clip + 600


def test_rate_watch_leaves_the_clips_before_the_rig_settled_alone():
    watch = server.RateWatch("cam1", calibrated(30.0), settled_at=1120.0)
    assert watch.clip_closed(1497, 60.0, clip_started=1000.0) is None  # the ragged launch clips
    assert watch.clip_closed(1497, 60.0, clip_started=1060.0) is None
    assert watch.clip_closed(1497, 60.0, clip_started=1120.0) is not None


def test_a_steady_camera_holds_one_of_two_adjacent_counts():
    watch = server.RateWatch("cam1", calibrated(29.6), settled_at=0.0)  # 29.6 x 60.03 = 1776.9 frames expected
    assert watch.clip_closed(1777, 60.03, clip_started=10.0) is None  # the boundary fell after a frame
    assert watch.clip_closed(1776, 60.03, clip_started=70.0) is None  # or before it
    assert watch.clip_closed(1775, 60.03, clip_started=130.0) is not None  # any other count is a departure


def test_rate_watch_says_a_departure_once_and_the_return_once():
    watch = server.RateWatch("cam1", calibrated(29.6), settled_at=100.0)
    assert watch.clip_closed(1777, 60.03, clip_started=120.0) is None  # its own rate
    warning = watch.clip_closed(1497, 60.03, clip_started=180.0)
    assert warning == "WARNING: camera cam1 delivered 1497 frames in a 60.0-s clip, 280 short of the 1777 expected at 29.60 fps"
    assert watch.clip_closed(1497, 60.03, clip_started=240.0) is None  # still low: silent
    assert watch.clip_closed(1776, 60.03, clip_started=300.0) == "camera cam1 delivers 29.60 fps again"
    assert watch.clip_closed(1777, 60.03, clip_started=360.0) is None
    assert watch.clip_closed(1497, 60.03, clip_started=420.0) is not None  # a second departure is said again
    assert "more than" in str(server.RateWatch("cam2", calibrated(29.6), settled_at=0.0).clip_closed(1900, 60.03, clip_started=10.0))


def test_an_uncalibrated_rate_widens_the_budget_by_its_uncertainty():
    # a rate measured in a 3-s warm-up is good to about 0.15 fps: nine frames over a minute
    quick = server.RateWatch("cam1", server.Rate(30.1, 0.15, "the launch measurement"), settled_at=0.0)
    assert quick.budget(60.0) == 1 + 9.0
    assert quick.clip_closed(1800, 60.0, clip_started=10.0) is None  # six frames short: within the budget
    assert quick.clip_closed(1704, 60.0, clip_started=70.0) is not None  # a hundred short: a departure
    exact = server.RateWatch("cam1", calibrated(30.1, 0.0), settled_at=0.0)
    assert exact.budget(60.0) == 1


def test_rate_watch_ignores_an_empty_clip():
    watch = server.RateWatch("cam1", calibrated(30.0), settled_at=0.0)
    assert watch.clip_closed(0, 0.0, clip_started=120.0) is None


# ------------------------------------------------- the frame-time file beside each clip

def test_frame_times_go_beside_the_clip_one_line_per_frame(tmp_path: object):
    path = os.path.join(str(tmp_path), server.frame_times_filename("rat1_cam1_20261119_10-00-00.mp4"))
    assert path.endswith("rat1_cam1_20261119_10-00-00_frames.csv")
    times = server.FrameTimes(path)
    times.add(0, 1763550000.123456, 0.0310, 0.0021)
    times.add(1, 1763550000.156789, 0.0333, 0.0019)
    times.close()
    with open(path) as f:
        lines = f.read().splitlines()
    assert lines[0] == "frame,arrived,waited_s,worked_s"
    assert len(lines) == 3 and lines[1].startswith("0,") and lines[1].endswith(",0.0310,0.0021")
    assert lines[2].split(",")[1].endswith(".156789")  # the arrival to the microsecond


def test_a_frame_time_file_that_cannot_be_opened_is_reported_and_skipped(tmp_path: object, capsys: pytest.CaptureFixture[str]):
    times = server.FrameTimes(os.path.join(str(tmp_path), "no_such_folder", "x_frames.csv"))
    times.add(0, 1763550000.0, 0.03, 0.002)  # nothing to write to, nothing raised
    times.close()
    assert "WARNING: could not open the frame-time file x_frames.csv" in capsys.readouterr().out
