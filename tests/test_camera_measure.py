"""measure_camera: what a camera really delivers at launch, counted from its frames, next
to what the driver reports; the writer is opened with the measured values."""

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
