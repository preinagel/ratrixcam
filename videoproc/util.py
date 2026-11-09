"""Helper functions needed only by videoproc, not used during recording"""

import contextlib
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the repo root, where ratrixcam_naming lives
from ratrixcam_naming import code_version as _code_version, shorten_path  # noqa: E402

LOCK_TIMEOUT_SECONDS = 300  # how long a run waits for a locked file before giving up on it
LOCK_STALE_SECONDS = 600  # a lock is held for milliseconds; one this old belongs to a dead run
LOCK_POLL_SECONDS = 0.5


@contextlib.contextmanager
def file_lock(
    lock_path: Path,
    timeout: float = LOCK_TIMEOUT_SECONDS,
    stale_after: float = LOCK_STALE_SECONDS,
    poll_interval: float = LOCK_POLL_SECONDS,
):
    """Cross-platform mutual-exclusion lock using atomic exclusive file creation
    (os.O_CREAT | os.O_EXCL is atomic on both NTFS and POSIX filesystems, unlike
    fcntl.flock which is POSIX-only and wouldn't work on Windows). A lock file older than
    `stale_after` was left behind by a run that died holding it and is removed. Used for
    the shared index.csv by compress_drive, confirm_and_delete and rebuild_index."""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue  # released between our attempt and the stat; retry at once
            if age > stale_after:
                print(f"WARNING: removing stale lock {lock_path} (age {age:.0f} s)")
                lock_path.unlink(missing_ok=True)
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"could not acquire lock {lock_path} within {timeout}s")
            time.sleep(poll_interval)
    try:
        os.write(fd, f"{socket.gethostname()} pid {os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n".encode())
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def code_version() -> str:
    """Git commit of the videoproc checkout ('+dirty' if edited), printed at the start of
    every run's terminal log."""
    return _code_version(Path(__file__))


# FourCC ratrixcam writes when it records; any other openable codec has been through
# downstream processing and is treated as "compressed"
RAW_CODEC = "FMP4"


def get_codec_nframes(path: Path):
    """(FourCC string, frame count) of a video, or (None, None) if it cannot be opened."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None, None
    codec = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((codec >> 8 * i) & 0xFF) for i in range(4)])
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return codec, n_frames


def shorten(path: Path | str, source: Path, destination: Path) -> str:
    """A path for the terminal, with the source or destination root replaced by <source> or
    <destination>; other paths unchanged."""
    return shorten_path(path, [(destination.resolve(), "<destination>"), (source.resolve(), "<source>")])


def build_output_path(
    output_root: Path,
    subject_prefix: str,
    studyname: str | None,
    subjID: str,
    view: str,
    clip_start: datetime,
    filename: str,
) -> Path:
    """Where a clip goes in the archive: <output_root>/<subject>/<day folder>/<filename>, with
    subject = subject_prefix + subjID and day folder = [studyname_]subject_view_YYYYMMDD.
    The file keeps its name."""
    subject = f"{subject_prefix}{subjID}"
    day_folder = f"{subject}_{view}_{clip_start:%Y%m%d}"
    if studyname:
        day_folder = f"{studyname}_{day_folder}"
    return Path(output_root) / subject / day_folder / filename
