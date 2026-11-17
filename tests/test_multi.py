"""ratrixcam_multi, the supervisor: the camera count that decides launches must never stall
it, and an unknown count launches nothing."""

import multiprocessing
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import pytest

from test_run_metadata import make_config

if TYPE_CHECKING:  # the real module for the checker; at run time the suite skips without OpenCV
    import ratrixcam_multi as multi
else:
    multi = pytest.importorskip("ratrixcam_multi")

PROFILER_OUTPUT = """Camera:

    USB Camera:

      Unique ID: 0x11410000c456366

    USB Camera:

      Unique ID: 0x1430000c456366

    HD Webcam C525:

      Unique ID: 0x14100000046d0826

"""


def test_the_camera_count_takes_every_camera_entry_whatever_its_name(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def answer(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=PROFILER_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", answer)
    assert multi.count_video_devices() == 3  # two "USB Camera" entries and the webcam
    assert seen["timeout"] == multi.DEVICE_COUNT_TIMEOUT


def test_a_count_that_times_out_is_unknown_and_said(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def hang(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, float(str(kwargs.get("timeout"))))

    monkeypatch.setattr(subprocess, "run", hang)
    assert multi.count_video_devices() is None
    assert "WARNING: the camera count timed out after" in capsys.readouterr().out


def test_a_count_that_fails_is_unknown(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError(23, "Too many open files in system")

    monkeypatch.setattr(subprocess, "run", fail)
    assert multi.count_video_devices() is None
    assert "Error detecting video devices" in capsys.readouterr().out


class FakeCameraProcess:
    """Stands in for a camera server's process: records its launch, is never alive."""

    launched: list[int] = []

    def __init__(self, target: object = None, args: tuple[object, ...] = ()):
        self.args: tuple[object, ...] = args

    def start(self) -> None:
        FakeCameraProcess.launched.append(int(str(self.args[1])))  # the camera index

    def is_alive(self) -> bool:
        return False

    def kill(self) -> None:
        pass


def supervise(monkeypatch: pytest.MonkeyPatch, tmp_path: object, count: int | None, seconds: float) -> list[int]:
    """Run the supervisor's loop for this long with the camera count pinned; the cameras it
    launched, by index."""
    config, _ = make_config(tmp_path)  # pyright: ignore[reportArgumentType]  # a tmp_path
    FakeCameraProcess.launched = []
    monkeypatch.setattr(multi, "count_video_devices", lambda: count)
    monkeypatch.setattr(multi, "Process", FakeCameraProcess)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # the supervisor's pause between launches; the same module
    stop = multiprocessing.Event()
    threading.Timer(seconds, stop.set).start()
    multi.run(config, stop)
    return list(FakeCameraProcess.launched)


def test_an_unknown_count_launches_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]):
    assert supervise(monkeypatch, tmp_path, None, 1.5) == []
    assert "Only detected" not in capsys.readouterr().out


def test_a_sufficient_count_launches_the_cameras(monkeypatch: pytest.MonkeyPatch, tmp_path: object):
    assert supervise(monkeypatch, tmp_path, 1, 1.5)[:1] == [0]  # relaunched each pass, since the stand-in dies at once
