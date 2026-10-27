"""move_file: the per-clip transfer from temp to the save drive. Failures are retried silently
within a window; after it the clip is left in temp with one WARNING naming it."""

import os
import re
import shutil
from collections import namedtuple
from pathlib import Path

import pytest

server = pytest.importorskip("ratrixcam_server")

Usage = namedtuple("Usage", "total used free")
Clip = namedtuple("Clip", "temp_file out_file save_root")
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
    temp_file.write_bytes(CLIP)
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


def test_short_of_space_leaves_the_clip_in_temp(clip: Clip, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**9, 10**9 - 40000, 40000))  # < 10 x clip
    assert server.move_file(*clip) is False
    assert Path(clip.temp_file).read_bytes() == CLIP
    assert not os.path.exists(clip.out_file)
    assert gave_up(capsys.readouterr().out) == [(1, "destination short of space")]


def test_unreachable_destination_leaves_the_clip_in_temp(clip: Clip, capsys: pytest.CaptureFixture[str]):
    blocker = Path(clip.save_root) / "day"
    blocker.write_bytes(b"")  # a file where the day folder should be: it cannot be created
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
        Path(dst).write_bytes(CLIP[:100])
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

    def partial(src: str, dst: str):
        seen.append(os.path.basename(dst))
        Path(dst).write_bytes(CLIP[:100])
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
