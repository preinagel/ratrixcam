"""move_file: the per-clip transfer from temp to the save drive. Failures are retried silently
within a window; after it the clip is left in temp with one WARNING naming it."""

import fcntl
import multiprocessing
import os
import re
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

if TYPE_CHECKING:  # the real module for the checker; at run time the suite skips without OpenCV
    import ratrixcam_server as server
else:
    server = pytest.importorskip("ratrixcam_server")
from ratrixcam_utils import flush_to_device  # noqa: E402


class Usage(NamedTuple):
    """What shutil.disk_usage returns, for the tests that stand in for it."""

    total: int
    used: int
    free: int


class Clip(NamedTuple):
    """The three paths move_file takes, as strings."""

    temp_file: str
    out_file: str
    save_root: str


CLIP = b"x" * 5000


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch):
    """No pauses, and a zero retry window: one attempt, then give up. Tests that want retries
    set TRANSFER_RETRY_WINDOW themselves."""
    monkeypatch.setattr(server, "TRANSFER_PAUSE", 0)
    monkeypatch.setattr(server, "TRANSFER_RETRY_DELAY", 0)
    monkeypatch.setattr(server, "TRANSFER_RETRY_WINDOW", 0)


@pytest.fixture
def clip(tmp_path: Path) -> Clip:
    """A clip in a camera temp folder, its destination in a day folder that does not exist
    yet, and the save root (which exists, as it does after launch)."""
    temp = tmp_path / "temp" / "cam"
    temp.mkdir(parents=True)
    temp_file = temp / "c.mp4"
    _ = temp_file.write_bytes(CLIP)
    save_root = tmp_path / "save"
    save_root.mkdir()
    return Clip(str(temp_file), str(save_root / "day" / "c.mp4"), str(save_root))


def gave_up(captured: str) -> list[tuple[int, str]]:
    """(tries, reason) from each 'left in temp' warning line."""
    return [
        (int(m.group(1)), m.group(2))
        for m in re.finditer(r"WARNING: c\.mp4 left in temp; gave up after (\d+) tr(?:y|ies) \((.*)\)", captured)
    ]


def test_moves_the_clip_and_creates_the_day_folder(clip: Clip, capsys: pytest.CaptureFixture[str]):
    assert server.move_file(*clip) is True
    assert not os.path.exists(clip.temp_file)
    assert Path(clip.out_file).read_bytes() == CLIP
    assert os.listdir(os.path.dirname(clip.out_file)) == ["c.mp4"]  # no .part left behind
    assert capsys.readouterr().out == ""  # success is silent


def test_unreadable_temp_file_is_reported_not_raised(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def no_size(_p: str) -> int:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os.path, "getsize", no_size)
    assert server.move_file(*clip) is False
    assert "WARNING: c.mp4 left in temp; cannot read it (Input/output error)" in capsys.readouterr().out
    assert not os.path.exists(clip.out_file)


def test_temp_copy_that_cannot_be_removed_is_reported(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def no_remove(_p: str) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "remove", no_remove)
    assert server.move_file(*clip) is True
    assert Path(clip.out_file).read_bytes() == CLIP
    assert "WARNING: c.mp4 transferred, but its temp copy could not be removed (Operation not permitted)" in capsys.readouterr().out


def test_missing_temp_file_is_reported(clip: Clip, capsys: pytest.CaptureFixture[str]):
    os.remove(clip.temp_file)
    assert server.move_file(*clip) is False
    assert "was not found" in capsys.readouterr().out
    assert not os.path.exists(clip.out_file)


def test_missing_save_root_creates_nothing(clip: Clip, capsys: pytest.CaptureFixture[str]):
    """A vanished mount point: the save folder is gone, so no folder is created in its place."""
    os.rmdir(clip.save_root)
    assert server.move_file(*clip) is False
    assert Path(clip.temp_file).read_bytes() == CLIP
    assert not os.path.exists(clip.save_root)
    assert gave_up(capsys.readouterr().out) == [(1, "save folder not found")]


def test_save_root_vanishing_after_the_check_is_not_rebuilt(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """The mount point disappears between the save-folder check and the day-folder creation:
    nothing is created in its place on the system disk."""
    os.rmdir(clip.save_root)
    real_isdir = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda p: True if p == clip.save_root else real_isdir(p))
    assert server.move_file(*clip) is False
    assert not os.path.exists(clip.save_root)
    assert Path(clip.temp_file).read_bytes() == CLIP
    ((_, reason),) = gave_up(capsys.readouterr().out)
    assert reason.startswith("destination unavailable: ")


def test_day_folder_made_by_a_sibling_transfer_meanwhile_is_fine(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Two transfers of one camera race to create the same new day folder: the loser of the
    mkdir carries on with the folder the winner made, on the same attempt."""
    out_dir = os.path.dirname(clip.out_file)
    real_mkdir = os.mkdir

    def sibling_first(path: str, *a, **k):
        if path == out_dir:
            real_mkdir(path)  # the sibling gets there first
        return real_mkdir(path, *a, **k)  # and this one's own call raises FileExistsError

    monkeypatch.setattr(os, "mkdir", sibling_first)
    assert server.move_file(*clip) is True
    assert Path(clip.out_file).read_bytes() == CLIP
    assert capsys.readouterr().out == ""


def test_short_of_space_leaves_the_clip_in_temp(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**9, 10**9 - 40000, 40000))  # < 10 x clip
    assert server.move_file(*clip) is False
    assert Path(clip.temp_file).read_bytes() == CLIP
    assert not os.path.exists(clip.out_file)
    assert gave_up(capsys.readouterr().out) == [(1, "destination short of space")]


def test_unreachable_destination_leaves_the_clip_in_temp(clip: Clip, capsys: pytest.CaptureFixture[str]):
    blocker = Path(clip.save_root) / "day"
    _ = blocker.write_bytes(b"")  # a file where the day folder should be: it cannot be created
    assert server.move_file(*clip) is False
    assert Path(clip.temp_file).read_bytes() == CLIP
    ((_, reason),) = gave_up(capsys.readouterr().out)
    assert reason.startswith("destination unavailable: ")
    assert "/" not in reason  # the message alone, no paths


def test_retries_silently_until_the_destination_comes_back(
    clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The save folder is absent for the first attempts and appears later, as after an SSD swap."""
    monkeypatch.setattr(server, "TRANSFER_RETRY_WINDOW", 30)
    os.rmdir(clip.save_root)
    real_isdir = os.path.isdir
    checks: list[int] = []

    def isdir_then_mount(path: str) -> bool:
        if path == clip.save_root:
            checks.append(1)
            if len(checks) == 3:
                os.mkdir(clip.save_root)  # the fresh drive mounts
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", isdir_then_mount)
    assert server.move_file(*clip) is True
    assert len(checks) == 3
    assert Path(clip.out_file).read_bytes() == CLIP
    assert not os.path.exists(clip.temp_file)
    assert capsys.readouterr().out == ""  # nothing said about the retries


def test_gives_up_after_the_window_and_removes_the_partial_copy(
    clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(server, "TRANSFER_RETRY_WINDOW", 0.05)

    def partial(src: str, dst: str):
        _ = Path(dst).write_bytes(CLIP[:100])
        raise OSError(28, "No space left on device", src, None, dst)

    monkeypatch.setattr(shutil, "copy2", partial)
    assert server.move_file(*clip) is False
    assert Path(clip.temp_file).read_bytes() == CLIP
    assert os.listdir(os.path.dirname(clip.out_file)) == []  # neither the clip nor its .part
    out = capsys.readouterr().out
    ((tries, reason),) = gave_up(out)
    assert tries >= 2
    assert reason == "copy failed: No space left on device"
    assert out.count("WARNING") == 1


def test_partial_copy_never_carries_the_clip_name(clip: Clip, monkeypatch: pytest.MonkeyPatch):
    """The copy is cut short (as when the drive is yanked mid-copy): what is on the drive is
    c.mp4.part, never a truncated c.mp4."""
    seen: list[str] = []

    def partial(_src: str, dst: str):
        seen.append(os.path.basename(dst))
        _ = Path(dst).write_bytes(CLIP[:100])
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(shutil, "copy2", partial)
    assert server.move_file(*clip) is False
    assert seen == ["c.mp4.part"]


def test_short_copy_counts_as_a_failure(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(shutil, "copy2", lambda src, dst: Path(dst).write_bytes(CLIP[:100]))
    assert server.move_file(*clip) is False
    assert Path(clip.temp_file).read_bytes() == CLIP
    assert not os.path.exists(clip.out_file)
    assert gave_up(capsys.readouterr().out) == [(1, "copied 100 of 5000 bytes")]


def test_copy_is_flushed_to_the_device_before_and_after_the_rename(clip: Clip, monkeypatch: pytest.MonkeyPatch):
    """The .part file is pushed out of the OS cache before it takes the clip's name, and the
    folder entry after, so a drive pulled right after the transfer still holds the clip."""
    events: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr(server, "flush_to_device", lambda p: events.append(f"flush {os.path.basename(p)}"))
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("rename"), real_replace(a, b)))
    assert server.move_file(*clip) is True
    assert events == ["flush c.mp4.part", "rename", "flush day"]


def test_flush_falls_back_to_fsync_where_full_sync_is_rejected(clip: Clip, monkeypatch: pytest.MonkeyPatch):
    def reject(_fd: int, _cmd: int):
        raise OSError(45, "Operation not supported")

    synced: list[int] = []
    monkeypatch.setattr(fcntl, "fcntl", reject)
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    flush_to_device(clip.temp_file)
    assert len(synced) == 1


def test_a_copy_that_cannot_be_flushed_is_a_failed_attempt(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def cannot(_p: str):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(server, "flush_to_device", cannot)
    assert server.move_file(*clip) is False
    assert os.listdir(os.path.dirname(clip.out_file)) == []  # neither the clip nor its .part
    assert gave_up(capsys.readouterr().out) == [(1, "copy failed: Input/output error")]


# ------------------------------------------------- the wait for transfers at a server's shutdown

def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _sleep_ignoring_term(seconds: float) -> None:
    """What a real transfer process looks like: SIGTERM ignored, inherited from the server."""
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(seconds)


def test_wait_for_transfers_waits_for_them_all_without_an_abort(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "TRANSFER_POLL", 0.01)
    processes = [multiprocessing.Process(target=_sleep, args=(0.3,)) for _ in range(2)]
    for p in processes:
        p.start()
    assert server.wait_for_transfers(processes, None) == (2, 0)
    assert not any(p.is_alive() for p in processes)


def test_wait_for_transfers_kills_them_when_the_abort_is_already_set(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(server, "TRANSFER_POLL", 0.01)
    processes = [multiprocessing.Process(target=_sleep_ignoring_term, args=(30,)) for _ in range(2)]
    for p in processes:
        p.start()
    time.sleep(0.5)  # let them install the ignore, as the real ones have from birth
    abort = multiprocessing.Event()
    abort.set()
    started = time.monotonic()
    assert server.wait_for_transfers(processes, abort) == (0, 2)
    assert time.monotonic() - started < 5
    assert not any(p.is_alive() for p in processes)
    assert "run aborted; 2 transfer(s) stopped, their clips stay in temp" in capsys.readouterr().out


def test_wait_for_transfers_honours_an_abort_that_arrives_while_waiting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "TRANSFER_POLL", 0.01)
    processes = [multiprocessing.Process(target=_sleep_ignoring_term, args=(30,))]
    processes[0].start()
    abort = multiprocessing.Event()
    threading.Timer(0.5, abort.set).start()
    started = time.monotonic()
    assert server.wait_for_transfers(processes, abort) == (0, 1)
    assert 0.4 < time.monotonic() - started < 5
    assert not processes[0].is_alive()


def test_close_writer_without_transfer_releases_and_lists_the_clip(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """An abort: the clip is finalised and gets its manifest row, but no transfer is started."""
    import cv2
    import numpy as np
    from datetime import datetime

    from ratrixcam_manifest import ManifestMetadata

    temp = tmp_path / "temp"
    cam_dir = temp / "Test_subj001_cam1"
    cam_dir.mkdir(parents=True)
    writer = cv2.VideoWriter(str(cam_dir / "c.mp4"), cv2.VideoWriter.fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened()
    writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    state = server.WriterState(writer, str(tmp_path / "save" / "Test_subj001_cam1_20260102"), str(cam_dir), "c.mp4", datetime(2026, 1, 2, 3, 4, 5))
    manifest = ManifestMetadata(str(temp), "subj001_cam1", "001", "cam1", 25)
    processes: list[multiprocessing.Process] = []

    server.close_writer(state, processes, manifest, str(tmp_path / "save"), transfer=False)

    assert processes == []
    assert "c.mp4 closed and left in temp (run aborted)" in capsys.readouterr().out
    assert "Test_subj001_cam1_20260102/c.mp4" in Path(manifest.path).read_text()
    assert cv2.VideoCapture(str(cam_dir / "c.mp4")).isOpened()  # finalised, so it opens


# ------------------------------------------------- a file already carrying the clip's name at the destination

def openable_clip(path: Path, frames: int) -> int:
    """A real mp4 of `frames` frames at path; returns its size."""
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened()
    for i in range(frames):
        writer.write(np.full((48, 64, 3), i * 7 % 256, dtype=np.uint8))
    writer.release()
    return path.stat().st_size


def test_an_identical_clip_on_the_drive_means_nothing_to_copy(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    day = Path(clip.out_file).parent
    day.mkdir()
    _ = Path(clip.out_file).write_bytes(CLIP)  # transferred earlier; only the temp copy survived
    monkeypatch.setattr(shutil, "copy2", lambda *a: pytest.fail("nothing should be copied"))
    assert server.move_file(*clip) is True
    assert not os.path.exists(clip.temp_file)
    assert Path(clip.out_file).read_bytes() == CLIP
    assert os.listdir(day) == ["c.mp4"]
    out = capsys.readouterr().out
    assert "c.mp4 is already on the drive, identical (5000 bytes); the temp copy is removed" in out
    assert "WARNING" not in out


def test_an_unopenable_file_on_the_drive_is_replaced_by_the_openable_temp_clip(clip: Clip, capsys: pytest.CaptureFixture[str]):
    size = openable_clip(Path(clip.temp_file), frames=5)
    Path(clip.out_file).parent.mkdir()
    _ = Path(clip.out_file).write_bytes(b"junk that carries the clip's name")
    assert server.move_file(*clip) is True
    assert Path(clip.out_file).stat().st_size == size
    assert not os.path.exists(clip.temp_file)
    assert sorted(os.listdir(Path(clip.out_file).parent)) == ["c.mp4"]
    out = capsys.readouterr().out
    assert "WARNING: c.mp4 is on the drive already and differs" in out and "cannot be opened as video and is replaced" in out


def test_an_unopenable_temp_clip_is_deleted_when_the_drive_holds_an_openable_one(clip: Clip, capsys: pytest.CaptureFixture[str]):
    size = openable_clip(Path(clip.out_file), frames=5)  # the temp copy is the fixture's 5000 bytes of 'x'
    assert server.move_file(*clip) is True
    assert not os.path.exists(clip.temp_file)
    assert Path(clip.out_file).stat().st_size == size
    assert sorted(os.listdir(Path(clip.out_file).parent)) == ["c.mp4"]
    out = capsys.readouterr().out
    assert "WARNING: c.mp4 is on the drive already and differs" in out and "the temp copy cannot be opened as video and is deleted" in out


def test_when_both_open_the_larger_keeps_the_name_and_the_smaller_is_renamed(clip: Clip, capsys: pytest.CaptureFixture[str]):
    big = openable_clip(Path(clip.temp_file), frames=40)
    small = openable_clip(Path(clip.out_file), frames=3)
    assert big > small
    assert server.move_file(*clip) is True
    day = Path(clip.out_file).parent
    assert Path(clip.out_file).stat().st_size == big
    assert (day / "c_superseded.mp4").stat().st_size == small  # left where it was found, under no clip's name
    assert not os.path.exists(clip.temp_file)
    out = capsys.readouterr().out
    assert "both open as video" in out and "the file on the drive is renamed c_superseded.mp4 and the temp copy is transferred" in out


def test_when_both_open_and_the_drive_has_the_larger_the_temp_copy_is_renamed_in_place(clip: Clip, capsys: pytest.CaptureFixture[str]):
    small = openable_clip(Path(clip.temp_file), frames=3)
    big = openable_clip(Path(clip.out_file), frames=40)
    assert server.move_file(*clip) is False  # the drive keeps its clip; this copy did not transfer
    assert Path(clip.out_file).stat().st_size == big  # untouched
    temp_dir = Path(clip.temp_file).parent
    assert sorted(os.listdir(temp_dir)) == ["c_superseded.mp4"]
    assert (temp_dir / "c_superseded.mp4").stat().st_size == small
    out = capsys.readouterr().out
    assert "the file on the drive keeps the name and the temp copy is renamed c_superseded.mp4" in out
    assert "could not be removed" not in out and "gave up" not in out  # final at once: no retries, no give-up line
