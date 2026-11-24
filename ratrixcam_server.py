import argparse
import multiprocessing
import os
import shutil
import signal
import statistics
import time
import math
from datetime import datetime
from multiprocessing import Process
from multiprocessing.synchronize import Event
from types import FrameType
from collections.abc import Callable
from typing import NamedTuple, TextIO, cast

import cv2
import numpy as np
from cv2.typing import MatLike
from pydantic import BaseModel

from ratrixcam_manifest import ManifestMetadata
from ratrixcam_naming import camera_name, clip_filename, still_path
from ratrixcam_utils import (
    CAMERA_LAUNCH_PAUSE,
    Calibration,
    Config,
    ensure_dir_exists,
    flush_to_device,
    load_settings,
    opens_as_video,
    stills_dir,
)

video_codec = cv2.VideoWriter.fourcc(*"mp4v")


# ----------------------------------------------------------------------
# BEGIN FUNCTION DEFINITIONS
TRANSFER_PAUSE: float = 3  # seconds before the first attempt; hand tuned to prevent race condition failures (fragile)
TRANSFER_RETRY_DELAY: float = 5  # seconds between attempts
TRANSFER_RETRY_WINDOW: float = 180  # seconds of silent retrying before a clip is left in temp: long enough for a
                                    # planned SSD swap (eject, unplug, plug in, mount) without a frame lost; the
                                    # cameras' final transfers at stop wait this long on a missing or full drive,
                                    # so it must stay well under the 10-minute shutdown kill timeout
TRANSFER_SPACE_FACTOR = 10  # free space required at the destination, as a multiple of the clip's size;
                            # generous so that the cameras' near-simultaneous transfers all fit or all wait


def move_file(
    temp_file: str, out_file: str, save_root: str, pause: float | None = None, retry_window: float | None = None
) -> bool:
    """
    Copy a closed clip from its temporary location to its permanent one, verify the copy by
    size, flush it onto the drive, then remove the temporary file. Runs in its own process. Whatever stops a transfer
    (the save folder, which launch requires to exist, is gone; the destination cannot be created or is
    short of space; the copy fails) is retried silently every TRANSFER_RETRY_DELAY seconds
    for TRANSFER_RETRY_WINDOW seconds; after that the clip is left in temp with one WARNING
    naming it, and the shutdown cleanup finds such clips by listing temp and re-requests them
    through this same function with its own pause and retry_window (those clips were closed
    long ago, and the drive was checked just before). Nothing is created unless save_root
    exists, so a vanished mount point never gets a folder of the same name on the system disk.
    Returns:
        True if the clip was moved
    """
    xfer_mode="copy" # hardwire until compression fully supported

    time.sleep(TRANSFER_PAUSE if pause is None else pause)  # pause before first attempt reduces failures
    name = os.path.basename(temp_file)
    if not os.path.exists(temp_file):
        print(f"WARNING: {name} was not found in its temp folder {os.path.basename(os.path.dirname(temp_file))}; nothing to transfer")
        return False
    try:
        size = os.path.getsize(temp_file)
    except OSError as e:
        print(f"WARNING: {name} left in temp; cannot read it ({e.strerror or e})")
        return False

    deadline = time.monotonic() + (TRANSFER_RETRY_WINDOW if retry_window is None else retry_window)
    tries = 0
    while True:
        tries += 1
        problem = _transfer_attempt(temp_file, out_file, save_root, size, xfer_mode)
        if problem is None:
            break
        if problem == SUPERSEDED:
            return False  # final, already reported: the drive keeps its larger clip, this copy stays in temp under another name
        if time.monotonic() >= deadline:
            try:
                if os.path.exists(out_file + PARTIAL_SUFFIX):
                    os.remove(out_file + PARTIAL_SUFFIX)  # no partial copies at the destination
            except OSError:
                pass
            print(f"WARNING: {name} left in temp; gave up after {tries} {'try' if tries == 1 else 'tries'} ({problem})")
            return False
        time.sleep(TRANSFER_RETRY_DELAY)

    try:
        os.remove(temp_file)
    except FileNotFoundError:
        pass  # already gone: a collision at the destination left it under another name, or removed it
    except OSError as e:
        print(f"WARNING: {name} transferred, but its temp copy could not be removed ({e.strerror or e})")
    return True


SUPERSEDED_SUFFIX = "_superseded"  # a damaged or smaller twin of a clip, renamed so it carries no clip's name
SUPERSEDED = "temp copy superseded"  # an attempt's outcome that is final: the temp copy was renamed in place
COMPARE_CHUNK = 1 << 20  # bytes read at a time when two files are compared


def _same_content(a: str, b: str) -> bool:
    """Whether the two files hold the same bytes, read in chunks and stopped at the first
    difference. No hashing: the identical case reads both to the end, which is no more I/O
    than the copy it saves."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(COMPARE_CHUNK), fb.read(COMPARE_CHUNK)
            if ca != cb:
                return False
            if not ca:
                return True


def _superseded_name(path: str) -> str:
    """path with SUPERSEDED_SUFFIX before the extension, numbered if that name is taken."""
    stem, ext = os.path.splitext(path)
    candidate = f"{stem}{SUPERSEDED_SUFFIX}{ext}"
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = f"{stem}{SUPERSEDED_SUFFIX}{n}{ext}"
    return candidate


def _resolve_collision(temp_file: str, out_file: str, size: int) -> str | None:
    """A file already carries the clip's name at the destination: decide what to do before
    any copy. Returns None when the copy should go ahead, "done" when the drive holds the
    clip and there is nothing to copy (the caller removes the temp copy), or SUPERSEDED
    when the temp copy was renamed in place: the drive keeps the clip, but this copy did
    not transfer, which the caller reports as such with no retry. Within a run no collision can
    arise (names are per camera and per second); this runs on a re-request at cleanup.
    Identical content: the clip was transferred earlier and only its temp copy survived;
    nothing to copy, the caller removes the temp copy. Otherwise one of the two is the clip
    and the other damaged: the one OpenCV can open keeps the name and the other is deleted;
    when both open (or neither), the larger keeps the name and the smaller is renamed with
    SUPERSEDED_SUFFIX where it lies, so it carries no clip's name. Every outcome but the
    identical one is a WARNING naming both files, their sizes and the rule. Raises OSError
    on a failure, which the caller reports as a failed attempt."""
    name = os.path.basename(temp_file)
    dest_size = os.path.getsize(out_file)
    if dest_size == size and _same_content(temp_file, out_file):
        print(f"{name} is already on the drive, identical ({size} bytes); the temp copy is removed")
        return "done"
    temp_ok, dest_ok = opens_as_video(temp_file), opens_as_video(out_file)
    both = f"temp copy {size} bytes, on the drive {dest_size} bytes"
    if dest_ok and not temp_ok:
        print(f"WARNING: {name} is on the drive already and differs ({both}); the temp copy cannot be opened as video and is deleted")
        return "done"  # the caller removes the temp copy
    if temp_ok and not dest_ok:
        os.remove(out_file)
        print(f"WARNING: {name} is on the drive already and differs ({both}); the file on the drive cannot be opened as video and is replaced")
        return None
    opening = "both open as video" if temp_ok else "neither opens as video"
    if size > dest_size:
        renamed = _superseded_name(out_file)
        os.replace(out_file, renamed)
        print(f"WARNING: {name} is on the drive already and differs ({both}, {opening}); the larger wins: the file on the drive is renamed {os.path.basename(renamed)} and the temp copy is transferred")
        return None
    renamed = _superseded_name(temp_file)
    os.replace(temp_file, renamed)
    print(f"WARNING: {name} is on the drive already and differs ({both}, {opening}); the larger wins: the file on the drive keeps the name and the temp copy is renamed {os.path.basename(renamed)}")
    return SUPERSEDED


PARTIAL_SUFFIX = ".part"  # a clip is copied under this suffix and renamed once its size is verified,
                          # so a copy cut short by a vanishing drive never carries a clip's name


def _transfer_attempt(temp_file: str, out_file: str, save_root: str, size: int, xfer_mode: str) -> str | None:
    """One attempt to put the clip at out_file: None on success, otherwise the reason it
    failed (the message alone, no paths)."""
    if not os.path.isdir(save_root):
        return "save folder not found"
    out_dir = os.path.dirname(out_file)
    try:
        if not os.path.isdir(out_dir):
            # one level only, never makedirs: if the save folder vanished since the check
            # above, this fails instead of rebuilding it on the system disk
            try:
                os.mkdir(out_dir)
            except FileExistsError:
                if not os.path.isdir(out_dir):  # a file in the way, not a sibling transfer's folder
                    raise
        free = shutil.disk_usage(out_dir).free
    except OSError as e:
        return f"destination unavailable: {e.strerror or e}"
    if free < size * TRANSFER_SPACE_FACTOR:
        return "destination short of space"
    try:
        if os.path.exists(out_file):
            outcome = _resolve_collision(temp_file, out_file, size)
            if outcome == SUPERSEDED:
                return SUPERSEDED
            if outcome is not None:
                return None  # nothing to copy: the drive holds the clip
        if xfer_mode == "copy":
            partial = out_file + PARTIAL_SUFFIX
            _ = shutil.copy2(temp_file, partial)
            copied = os.path.getsize(partial)
            if copied != size:
                return f"copied {copied} of {size} bytes"
            flush_to_device(partial)  # on the drive before it takes the clip's name
            os.replace(partial, out_file)
            try:
                flush_to_device(out_dir)  # the rename itself; best effort
            except OSError:
                pass
        # NOT FINISHED, KEPT FOR A FUTURE VERSION: transcoding on the fly instead of copying
        # (GitHub #16), as an xfer_mode other than "copy". Commented out, not deleted, so it can
        # be revived. It worked with timeout=3 for 640x480 on most cameras and for two 720p
        # cameras. HEADS-UP before reviving: os.system hands the string to the shell with the two
        # paths unquoted, so a folder name with a space or a shell character breaks or alters the
        # command; use subprocess.run with an argument list instead.
        # else:
        #     conversion_failed: int = os.system(f"ffmpeg -i {temp_file} -c:v hevc_videotoolbox -q:v 65 -tag:v hvc1 -loglevel error {out_file}")
        #     if conversion_failed:  # returns 0 for success, other outcomes
        #         print(f"ffmpeg conversion of {temp_file} failed with exit code {conversion_failed}")
        #     # the transcoded output cannot be verified by size: check it exists
        #     if not os.path.isfile(out_file):
        #         return "no output file"
    except OSError as e:
        return f"copy failed: {e.strerror or e}"
    except Exception as e:
        return f"copy failed: {e}"
    return None


class CameraParams(BaseModel):
    name: str  # constructed camera name: <subject_prefix><subjID>_<view>
    width: int
    height: int
    fps: int
    cam_exposure: float  # read from the config; the exposure setting is not implemented (GitHub #51)
    crf: int  # ffmpeg quality for this camera's clips, recorded for videoproc


WARM_UP_SECONDS = 3.0  # frames read and discarded at launch while the camera's true frame rate and size are
                       # measured, counted from the first frame: 60 to 90 intervals, enough for the median
FIRST_FRAME_WAIT = 5.0  # seconds a camera is given to deliver its first frame before it is declared dead
FPS_TOLERANCE = 1  # a measured rate this close to the requested one is the requested one: cameras drift less
GAP_THRESHOLD = 2.5  # an interval this many times the frame interval is a hole of two or more frames: counted as
                     # a gap by the calibration, filled in the clips by the server. The host hands frames over on
                     # its own tick, slightly off the camera's, so a doubled interval every few dozen frames is
                     # pacing and not a loss; a single lost frame looks the same and is left to the clip's count
GIVE_UP_SECONDS = 15.0  # a camera that delivers no frame for this long is presumed offline: the server exits and
                        # the supervisor relaunches it. A display wake freezes delivery to every server for
                        # about 5 s and then resumes; the limit is measured by the clock, not in reads
SILENT_SAID_AFTER = 3.0  # seconds without a frame before the silence is said, once, and its end after it
DUPLICATE_FILLERS = 1  # the first missing slots of a hole are duplicates of the previous frame, the rest blank
EMPTY_READ_PAUSE = 0.1  # a read that came back empty at once is followed by this pause: a dead capture answers
                        # at once and would otherwise spin until the give-up
                   # than this from nominal, and the standard value is what players expect in the label


def rig_settled_at(started: float, device_id: int, n_cameras: int, time_slice: int) -> float:
    """When every camera of the rig has started its second clip, seen from the server of camera
    `device_id` that started at `started`: the supervisor launches the cameras in index order
    CAMERA_LAUNCH_PAUSE apart, each spends up to FIRST_FRAME_WAIT + WARM_UP_SECONDS before its
    first clip, and its second clip begins one clip length after the first. Until then every
    camera's clips are ragged with the launch of the others."""
    last_first_clip = started + (n_cameras - 1 - device_id) * CAMERA_LAUNCH_PAUSE + FIRST_FRAME_WAIT + WARM_UP_SECONDS
    return last_first_clip + time_slice


class Rate(NamedTuple):
    fps: float  # frames per second the camera delivers
    uncertainty: float  # of fps: one frame over the span of the measurement it came from
    source: str  # where it came from, for the terminal


class RateWatch:
    """Compares each closed clip's frame count with what the camera's rate predicts for the
    clip's length and says so once when the clip departs, and once when the clips are back.
    A steady camera's clips hold one of two adjacent counts, the clip boundary falling before
    or after a frame, so the budget is one frame plus what the rate's own uncertainty amounts
    to over the clip: next to nothing for a calibrated rate, several frames for a rate
    measured in a few seconds at launch. A camera changes its rate mid-run on its own, for
    one, when its auto-exposure lengthens the frame in dim light, which nothing on macOS
    controls (GitHub #51), or loses frames to a crowded USB link; without this the loss is
    silent. Clips started before `settled_at` (rig_settled_at) are not compared. A departure
    is said once, so a shortfall that persists leaves no further trace in the log; the tally
    kept here is printed at shutdown (`summary`) so that a steady loss is seen in the end."""

    def __init__(self, name: str, rate: Rate, settled_at: float):
        self.name: str = name
        self.rate: Rate = rate
        self.settled_at: float = settled_at
        self.departed: bool = False
        self.clips: int = 0  # clips compared, from the rig's settling on
        self.short_clips: int = 0  # of them, clips with fewer frames than the budget allows
        self.over_clips: int = 0  # and clips with more
        self.frames_short: int = 0  # frames missing from the short clips, in all
        self.worst: int = 0  # the largest shortfall of one clip

    def budget(self, seconds: float) -> float:
        """Frames a clip of this length may differ from the prediction and still be steady."""
        return 1 + self.rate.uncertainty * seconds

    def clip_closed(self, frames: int, seconds: float, clip_started: float) -> str | None:
        """The line to print for a clip of this many frames over this many seconds, or None."""
        if clip_started < self.settled_at or seconds <= 0:
            return None
        expected = self.rate.fps * seconds
        short = expected - frames
        departed = abs(short) > self.budget(seconds)
        self.clips += 1
        if departed and short > 0:
            self.short_clips += 1
            self.frames_short += round(short)
            self.worst = max(self.worst, round(short))
        elif departed:
            self.over_clips += 1
        if departed == self.departed:
            return None
        self.departed = departed
        if departed:
            how = f"{short:.0f} short of" if short > 0 else f"{-short:.0f} more than"
            return (
                f"WARNING: camera {self.name} delivered {frames} frames in a {seconds:.1f}-s clip, {how} the "
                + f"{expected:.0f} expected at {self.rate.fps:.2f} fps"
            )
        return f"camera {self.name} delivers {self.rate.fps:.2f} fps again"

    def summary(self) -> str:
        """One line for the shutdown: how many of the compared clips held the camera's rate."""
        rate = f"{self.rate.fps:.2f} fps"
        if self.clips == 0:
            return f"camera {self.name}: no clip was compared with its {rate}"
        if self.short_clips == 0 and self.over_clips == 0:
            return f"camera {self.name}: all {self.clips} clips at its {rate}"
        parts = [f"camera {self.name}: {self.short_clips} of {self.clips} clips short of its {rate}"]
        if self.short_clips:
            parts.append(f"{self.frames_short} frames in all, worst {self.worst}")
        if self.over_clips:
            parts.append(f"{self.over_clips} clip(s) over")
        return ", ".join(parts)


class Measured(NamedTuple):
    fps: int  # frames per second the camera really delivers, rounded: compared with the request
    rate: float  # the same, unrounded: frames that arrived, less one, over the span
    uncertainty: float  # of rate: one frame over the span
    gaps: int  # intervals of more than 2.5 typical ones during the measurement: holes of two or more frames
    width: int  # of the frames themselves
    height: int
    reported_fps: float  # what the capture reports for the same properties after the settings
    reported_width: int  # were applied: for comparison only, never fed to the writer
    reported_height: int
    frames: int  # distinct frames counted during the warm-up


def measure_camera(
    capture: cv2.VideoCapture,
    seconds: float = WARM_UP_SECONDS,
    on_frame: Callable[[MatLike], None] | None = None,
    still_every: float = 1.0,
    first_frame_wait: float = FIRST_FRAME_WAIT,
) -> Measured | None:
    """Read and discard frames for about `seconds` and return what the camera really delivers:
    the rate as the distinct frames that arrived, less one, over the span from the first to
    the last, which is what a clip of the run will hold per second; its uncertainty is one
    frame over that span. A frame that did not arrive, whatever the reason, lowers the rate
    a little, which errs toward the clips. The host hands frames out with a pacing of its
    own (a pause of one interval now and then), so a doubled interval says nothing; only an
    interval of more than two and a half typical ones is counted as a gap. A set() the hardware rounded or refused is reported as accepted
    by some drivers and a writer fed the requested values labels every clip wrongly, or
    writes nothing when the size is wrong; so the writer gets these. A frame identical to
    the one before is a backend handing back its latest frame again and is not counted. The
    first frame is awaited for first_frame_wait; the warm-up is counted from it. None when
    no frame arrived, and a rate of 0 when only one did: neither is a camera to record from. on_frame, when given, receives a frame every still_every seconds,
    so the camera's still can be kept fresh while this takes its time."""
    reported_fps = capture.get(cv2.CAP_PROP_FPS)
    reported_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    reported_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = 0
    previous: MatLike | None = None
    last = 0.0
    intervals: list[float] = []
    next_still = 0.0
    deadline = time.monotonic() + first_frame_wait
    while time.monotonic() < deadline:
        ok, frame = capture.read()
        if not ok:
            time.sleep(0.01)  # a camera returning nothing would otherwise spin this loop until the deadline
            continue
        if previous is not None and np.array_equal(frame, previous):
            continue
        now = time.monotonic()
        if previous is None:
            deadline = now + seconds  # the warm-up is counted from the first frame
        else:
            intervals.append(now - last)
        last = now
        previous = frame
        frames += 1
        if on_frame is not None and now >= next_still:
            on_frame(frame)
            next_still = now + still_every
    if previous is None:
        return None
    rate, uncertainty, gaps = rate_from_intervals(intervals)
    height, width = cast(tuple[int, ...], previous.shape)[:2]
    return Measured(round(rate), rate, uncertainty, gaps, width, height, reported_fps, reported_width, reported_height, frames)


def rate_from_intervals(intervals: list[float]) -> tuple[float, float, int]:
    """(rate, its uncertainty, gaps) from the intervals between consecutive frames, as the
    docstring of measure_camera describes: the rate is the frames that arrived, less one,
    over the span from the first to the last; the uncertainty is one frame over that span,
    the counting error (a third of a frame per second over a 3-s warm-up, a few thousandths
    over a 300-s calibration); a gap is an interval of more than two and a half typical ones.
    (0, 0, 0) with fewer than two frames, when there is no interval at all."""
    if not intervals:
        return 0.0, 0.0, 0
    span = sum(intervals)
    rate = len(intervals) / span
    uncertainty = 1 / span
    typical = statistics.median(intervals)
    gaps = sum(1 for i in intervals if i > GAP_THRESHOLD * typical)
    return rate, uncertainty, gaps


class Grabbed(NamedTuple):
    frame: MatLike
    arrived: float  # the clock (time.time()) the moment the frame came back from the capture: its time stamp
    waited: float  # seconds spent in the capture waiting for it


def grab_frame(capture: cv2.VideoCapture, name: str) -> Grabbed | None:
    """The next frame with the time it arrived, read just after it came back so that the
    stamp is the frame's own and not the moment the wait for it began. One read: the backend
    waits about a second for a frame and gives None when none came, which says nothing about
    the camera; the loop decides, by the clock since the last frame, when it is dead. `name`
    is the camera's, for a line about the read should one ever be needed here."""
    _ = name
    asked = time.time()
    ok, frame = capture.read()
    if not ok:
        return None
    arrived = time.time()
    return Grabbed(frame, arrived, arrived - asked)


def write_frame(writer: cv2.VideoWriter, frame: MatLike, params: CameraParams, stamp: datetime, overlay_text: str) -> None:
    """Overlay the text and the frame's time stamp, then write the frame to the clip."""
    date_text = stamp.strftime("%Y%m%d")
    time_text = stamp.strftime("%H:%M:%S.%f")[:-4]  # truncated to .01 s to reflect the accuracy of the stamps

    font = cv2.FONT_HERSHEY_PLAIN
    font_scale = params.height / 480

    def draw(text: str, x: int, y: int) -> None:
        # white text, anti-aliased, positioned as a fraction of the frame size
        _ = cv2.putText(frame, text, (x, y), font, font_scale, (255, 255, 255), thickness=1, lineType=cv2.LINE_AA)

    draw(overlay_text, math.ceil(0.015 * params.width), math.floor(0.97 * params.height))  # bottom left
    draw(date_text, math.floor(0.8 * params.width), math.floor(0.94 * params.height))  # bottom right, upper line
    draw(time_text, math.floor(0.8 * params.width), math.floor(0.97 * params.height))  # bottom right, lower line

    writer.write(frame)


FRAME_TIMES_SUFFIX = "_frames.csv"  # beside each clip: one line per frame written, with its arrival time
FRAME_TIMES_HEADER = "frame,arrived,waited_s,worked_s,kind\n"
FRAME_KINDS = ("frame", "duplicate", "blank")  # a frame the camera delivered; the fillers for a hole


def frame_times_filename(clip_filename_: str) -> str:
    """The name of the frame-time file that goes with a clip."""
    stem, _ = os.path.splitext(clip_filename_)
    return stem + FRAME_TIMES_SUFFIX


class FrameTimes:
    """The frame-time file of one clip: for every frame written its number, the clock when
    it arrived, the seconds the loop waited for it, the seconds the loop worked between the
    previous read's return and asking for this one, and its kind. A filler's row carries the
    nominal time of its slot and no wait or work. A gap after a long wait is a frame the
    camera or the link did not deliver; a gap after long work is a frame that was overwritten
    while the loop was busy; the clip alone cannot tell the two apart."""

    def __init__(self, path: str):
        self.path: str = path
        self.file: TextIO | None = None  # None when the file could not be opened: the clip goes on without it
        try:
            self.file = open(path, "w")
            _ = self.file.write(FRAME_TIMES_HEADER)
        except OSError as e:
            print(f"WARNING: could not open the frame-time file {os.path.basename(path)}: {e.strerror or e}; the clip is recorded without it")

    def add(self, frame: int, arrived: float, waited: float, worked: float) -> None:
        """A frame the camera delivered."""
        if self.file is None:
            return
        stamp = datetime.fromtimestamp(arrived).strftime("%Y-%m-%d %H:%M:%S.%f")
        _ = self.file.write(f"{frame},{stamp},{waited:.4f},{worked:.4f},frame\n")

    def add_filler(self, frame: int, slot: float, kind: str) -> None:
        """A filler written for a slot no frame arrived in: its nominal time, and its kind."""
        if self.file is None:
            return
        stamp = datetime.fromtimestamp(slot).strftime("%Y-%m-%d %H:%M:%S.%f")
        _ = self.file.write(f"{frame},{stamp},,,{kind}\n")

    def close(self) -> None:
        if self.file is not None:
            self.file.close()


class WriterState(NamedTuple):
    writer: cv2.VideoWriter
    day_folder: str  # the clip's destination folder on the save drive, one per camera and day
    temp_dir: str
    file_name: str
    started: datetime  # time of the first frame written to this clip
    times: FrameTimes  # the clip's frame-time file, transferred with it


def close_writer(
    writer_state: WriterState, file_transfer_processes: list[Process], manifest: ManifestMetadata, save_root: str, transfer: bool = True
):
    """Release the clip and close its frame-time file, write its manifest row, then hand the
    clip and the frame-time file to a transfer process each. With transfer=False (the run is
    being aborted) the release and the row still happen, in that order, so the clip left in
    temp is a valid, listed clip; only the transfers are skipped."""
    writer_state.writer.release()
    writer_state.times.close()
    _ = manifest.append(os.path.basename(writer_state.day_folder), writer_state.file_name, writer_state.started)
    if not transfer:
        print(f"Cam_server: {writer_state.file_name} closed and left in temp (run aborted)")
        return

    # spawn a separate process to move each closed temp file to its permanent location
    print(f"Cam_server: Starting transfer of file:{writer_state.file_name}")
    for file_name in (writer_state.file_name, os.path.basename(writer_state.times.path)):
        temp_path = os.path.join(writer_state.temp_dir, file_name)
        if not os.path.isfile(temp_path):
            continue  # a frame-time file that could not be opened
        out_path = os.path.join(writer_state.day_folder, file_name)
        p = Process(target=move_file, args=(temp_path, out_path, save_root))
        p.start()
        # keep track of process to clean up later
        file_transfer_processes.append(p)


def release_finished(processes: list[Process]) -> list[Process]:
    """The transfer processes still running. Each finished one is joined and closed, which
    releases the pipe descriptors its process object holds; kept for the whole run, those
    pile up two per clip until the machine's file table is full and nothing can open a
    file any more. Called at every clip boundary and before the final wait."""
    running: list[Process] = []
    for p in processes:
        if p.is_alive():
            running.append(p)
        else:
            p.join()
            p.close()
    return running


TRANSFER_POLL = 0.1  # seconds between looks at the transfer processes while a server waits for them


def wait_for_transfers(processes: list[Process], abort_event: Event | None) -> tuple[int, int]:
    """Wait for the transfer processes to end, looking every TRANSFER_POLL seconds. If the
    abort event is set, now or while waiting, every transfer still alive is killed instead:
    its temp copy is intact (the temp file is removed only after a verified copy), and the
    partial copy it may leave on the drive is replaced by a later cleanup. kill(), not
    terminate(): these processes inherit the servers' ignore of SIGTERM. Returns
    (finished, killed)."""
    killed = 0
    alive = [p for p in processes if p.is_alive()]
    while alive:
        if abort_event is not None and abort_event.is_set():
            for p in alive:
                p.kill()
                p.join()
            killed = len(alive)
            print(f"Cam_server: run aborted; {killed} transfer(s) stopped, their clips stay in temp")
            break
        time.sleep(TRANSFER_POLL)
        alive = [p for p in alive if p.is_alive()]
    return len(processes) - killed, killed


def run(
    config: Config, device_id: int, stop_event: Event, abort_event: Event | None = None, calibration: Calibration | None = None
):
    """Record from the camera at device_id until stop_event. calibration, when given, holds
    the rate each camera delivers on its own, measured by the calibration program; a camera
    with no valid entry records at the rate measured in its warm-up instead, with the wider
    uncertainty that implies."""
    camera = config.cameras[device_id]
    params = CameraParams(
        name=camera_name(config.subject_prefix, camera.subjID, camera.view),
        width=(camera.width or config.default_width),
        height=(camera.height or config.default_height),
        fps=(camera.fps or config.default_fps),
        cam_exposure=(camera.exposure or config.default_cam_exposure),
        crf=(camera.crf if camera.crf is not None else config.default_crf),
    )
    camera_label: str = f"{config.study_label}_{params.name}"  # names the temp folder, the day folders and the overlay
    manifest = ManifestMetadata(config.temp_path, params.name, camera.subjID, camera.view, params.crf)

    temp_dir = os.path.join(config.temp_path, camera_label)
    if not ensure_dir_exists(temp_dir):
        print("ERROR: Temporary streaming folder does not exist and cannot be created")
        return

    stills_folder = stills_dir(config.temp_path)
    if not ensure_dir_exists(stills_folder):
        print("ERROR: Still image folder does not exist and cannot be created")
        return

    # create full path filename for updating still images (used for GUI display)
    camera_still_path = still_path(stills_folder, params.name)

    # try to connect to the camera
    capture = cv2.VideoCapture(int(device_id))  # hardware address
    start = time.time() # indicates time this videocapture was opened

    _ = capture.set(cv2.CAP_PROP_FRAME_WIDTH, params.width)
    _ = capture.set(cv2.CAP_PROP_FRAME_HEIGHT, params.height)
    _ = capture.set(cv2.CAP_PROP_FPS, params.fps)
    # UNTESTED, KEPT FOR A FUTURE VERSION: the camera's exposure setting (GitHub #51).
    # Commented out, not deleted. The AVFoundation backend on macOS does not implement
    # CAP_PROP_EXPOSURE or CAP_PROP_AUTO_EXPOSURE (set() returns False, get() returns 0); the
    # cameras run their own auto-exposure. To revive on V4L2 or DirectShow: set CAP_PROP_AUTO_EXPOSURE
    # to manual first where the backend needs it, set CAP_PROP_EXPOSURE in that backend's units,
    # check set()'s return value and read the value back before trusting it.
    # _ = capture.set(cv2.CAP_PROP_EXPOSURE, params.cam_exposure)

    if not capture.isOpened():
        print(f"Cam_server: Camera {params.name} Failed to open recording device {device_id}")
        return

    # what the camera really delivers, measured; the writer is opened with these, so every
    # clip is labelled with its true rate and size (a requested value the hardware rounded
    # or refused would label it wrongly, and a wrong size would write nothing at all)
    def write_still(frame: MatLike) -> None:
        _ = cv2.imwrite(camera_still_path, frame)  # the viewer sees the camera live through the warm-up

    measured = measure_camera(capture, on_frame=write_still, still_every=config.preview_interval)
    if measured is None or measured.fps == 0:
        frames = measured.frames if measured is not None else 0
        print(f"Cam_server: Camera {params.name} delivered {frames} frame(s) in its warm-up, no rate to record at; closing it")
        capture.release()
        return
    # the driver's own report is kept on record only when it disagrees with the measurement:
    # a long record of silence is the case for trusting it one day
    if (round(measured.reported_fps), measured.reported_width, measured.reported_height) != (measured.fps, measured.width, measured.height):
        print(
            f"Cam_server: the driver reports {measured.reported_fps:g} fps at {measured.reported_width}x{measured.reported_height} "
            + f"for camera {params.name}, not what it delivers"
        )
    fps = params.fps if abs(measured.fps - params.fps) <= FPS_TOLERANCE else measured.fps
    if (fps, measured.width, measured.height) != (params.fps, params.width, params.height):
        # nothing is said when the camera delivers what was asked
        print(
            f"WARNING: camera {params.name} delivers {fps} fps at {measured.width}x{measured.height}, not the "
            + f"{params.fps} fps at {params.width}x{params.height} requested"
        )
        print(f"WARNING: camera {params.name}'s clips are labelled with the true values, {measured.width}x{measured.height} at its own rate")
    # the rate the clips are labelled with and compared against: the calibration's for this
    # camera in this requested mode, else the warm-up's, which is less certain
    entry = None
    if calibration is not None:
        entry = calibration.for_camera(device_id, params.name, params.width, params.height, params.fps)
        if entry is None:
            print(f"WARNING: camera {params.name} has no calibration for {params.width}x{params.height} at {params.fps} fps under this name")
    if calibration is not None and entry is not None:
        rate = Rate(entry.rate, entry.uncertainty, f"the calibration of {calibration.calibrated}")
        if abs(measured.rate - entry.rate) > 3 * (measured.uncertainty + entry.uncertainty):
            print(
                f"WARNING: camera {params.name} measures {measured.rate:.2f} fps at launch, not the {entry.rate:.2f} fps of its "
                + "calibration; the calibration is used and every clip is compared with it"
            )
    else:
        rate = Rate(measured.rate, measured.uncertainty, "the launch measurement, uncalibrated")
    print(f"Cam_server: camera {params.name} records at {rate.fps:.3f} fps (uncertainty {rate.uncertainty:.3f}) from {rate.source}")
    params = params.model_copy(update={"fps": fps, "width": measured.width, "height": measured.height})
    rate_watch = RateWatch(params.name, rate, rig_settled_at(time.time(), device_id, len(config.cameras), config.time_slice))

    # Two counts. Frames that ARRIVED pace the still and are what the watch compares with the
    # camera's rate. Frames WRITTEN, arrived and fillers, are the burned-in label and the
    # frame-time file's first column, so that a clip's frames and its rows match one to one.
    interval: float = 1 / rate.fps  # one slot of a clip: the camera's frame interval
    arrived_frames: int = 0
    written_frames: int = 0
    clip_arrived: int = 0  # frames that arrived in the open clip
    clip_count: int = 0
    file_transfer_processes: list[Process] = []
    writer_state: WriterState | None = None
    last_arrived: float = time.time()  # when the previous frame came back: the give-up and the silence lines count from it
    pass_end: float = last_arrived  # when the previous read came back, frame or not: the loop's work since then goes in the frame-time file
    silent_said: bool = False
    # A hole in the arrivals is filled slot by slot from the nominal time of the last written
    # slot: a duplicate of the last frame for the first DUPLICATE_FILLERS slots, blanks after.
    # Fillers are written as the hole goes on (each empty read fills the slots that have fully
    # elapsed) and the remainder when the next frame arrives, so a frame that lands in a slot
    # already filled adds nothing. A clip is opened only by an arrived frame and named by it:
    # slots that fall between a clip's close and the next frame are not filled.
    slot_time: float = 0.0  # the nominal time of the last written slot of the open clip
    in_hole: bool = False  # fillers have been written since the last arrival
    hole_fillers: int = 0  # fillers written in the current hole
    last_written: MatLike | None = None  # the last arrived frame as written, overlay and all
    blank: MatLike = np.zeros((params.height, params.width, 3), dtype=np.uint8)  # no label at all

    def fill(n: int) -> None:
        """Write n fillers to the open clip for the slots after the last written one."""
        nonlocal written_frames, slot_time, hole_fillers, in_hole
        if writer_state is None or last_written is None:
            return
        for _ in range(n):
            slot_time += interval
            kind = "duplicate" if hole_fillers < DUPLICATE_FILLERS else "blank"
            writer_state.writer.write(last_written if kind == "duplicate" else blank)
            writer_state.times.add_filler(written_frames, slot_time, kind)
            written_frames += 1
            hole_fillers += 1
            in_hole = True

    def close_clip_at_boundary() -> None:
        """The clock has passed the open clip's slice: fill its hole, if any, with the slots
        that fell due up to the boundary, release the clip, compare its arrived count with
        the camera's rate, and let go of the transfers that have finished since the last
        clip. Nothing else is appended: a clip short of the clock's count by single lost
        frames stays short, and the sidecar and the watch record it. The slots after the
        boundary belong to no clip until a frame opens the next one."""
        nonlocal writer_state, file_transfer_processes
        if writer_state is None:
            return
        boundary = start + config.time_slice
        slots = math.floor((boundary - slot_time) / interval)
        if slots > 0 and (in_hole or boundary - slot_time > GAP_THRESHOLD * interval):
            fill(slots)
        seconds = boundary - start
        close_writer(writer_state, file_transfer_processes, manifest, config.save_path)
        message = rate_watch.clip_closed(clip_arrived, seconds, start)
        if message is not None:
            print(message)
        file_transfer_processes = release_finished(file_transfer_processes)
        writer_state = None

    # one pass per read until the camera is stopped: a frame, or an empty read after the
    # backend's wait of about a second, which is when the clock alone drives the clip
    while capture.isOpened() and not stop_event.is_set():
        asked = time.time()
        grabbed = grab_frame(capture, params.name)
        if grabbed is None:
            now = time.time()
            if now - last_arrived > GIVE_UP_SECONDS:
                print(f"Cam_server: no frame from camera {params.name} for {GIVE_UP_SECONDS:.0f} s at {datetime.now().strftime('%H:%M:%S')}; presumed offline")
                break
            if not silent_said and now - last_arrived > SILENT_SAID_AFTER:
                print(f"Cam_server: camera {params.name} has delivered no frame for {now - last_arrived:.0f} s")
                silent_said = True
            if writer_state is not None:
                if now - start > config.time_slice:
                    close_clip_at_boundary()
                else:
                    # the slots that have fully elapsed, less the one the next frame may still land in
                    elapsed = math.floor((now - slot_time) / interval) - 1
                    if elapsed > 0 and (in_hole or now - slot_time > GAP_THRESHOLD * interval):
                        fill(elapsed)
            pass_end = now
            if now - asked < EMPTY_READ_PAUSE:
                time.sleep(EMPTY_READ_PAUSE)
            continue

        now = grabbed.arrived  # the frame's stamp, and the clock the clip boundary is judged by
        current_datetime: datetime = datetime.fromtimestamp(timestamp=now)
        if silent_said:
            print(f"Cam_server: camera {params.name} delivers frames again after {now - last_arrived:.1f} s")
            silent_said = False
        if writer_state is not None and now - start > config.time_slice:
            close_clip_at_boundary()
        if writer_state is None:
            start = now  # the clip's clock starts at its first frame; the clip ends when the clock passes time_slice
            clip_arrived = 0
            clip_count += 1
            slot_time, in_hole, hole_fillers = now, False, 0

            # open a new writer; the clip's destination day folder is created by move_file
            # when the clip is transferred, so this loop never touches the save drive
            day_folder = os.path.join(
                config.save_path, f"{camera_label}_{current_datetime.strftime('%Y%m%d')}"
            )
            current_file_name = clip_filename(params.name, current_datetime)
            # Create video writer, labelled with the camera's own rate so that playback keeps clock time
            writer_state = WriterState(
                cv2.VideoWriter(
                    os.path.join(temp_dir, current_file_name),
                    video_codec,
                    rate.fps,
                    (params.width, params.height),
                ),
                day_folder,
                temp_dir,
                current_file_name,
                current_datetime,
                FrameTimes(os.path.join(temp_dir, frame_times_filename(current_file_name))),
            )
            print(f"Cam_server: Camera {params.name} will now stream to {current_file_name}")
        else:
            gap = now - slot_time
            if in_hole or gap > GAP_THRESHOLD * interval:
                fill(max(0, round(gap / interval) - 1))
            slot_time, in_hole, hole_fillers = now, False, 0

        overlay_text: str = camera_label + ' frame ' + str(written_frames)
        write_frame(writer_state.writer, grabbed.frame, params, current_datetime, overlay_text)
        writer_state.times.add(written_frames, now, grabbed.waited, asked - pass_end)
        last_written = grabbed.frame.copy()  # the duplicate filler, should the next slot stay empty
        written_frames += 1
        clip_arrived += 1
        last_arrived = pass_end = now

        # once per N sec, try to update the still image
        if arrived_frames % (config.preview_interval * params.fps) == 0:
            try:
                result = cv2.imwrite(camera_still_path, grabbed.frame)
                if result is False:
                    print(
                        f"WARNING: Failed to write still image to {camera_still_path}"
                    )
            except Exception as e:
                print(type(e), e)
        arrived_frames += 1  # whether the still update succeeds or fails

    # reach this line whenever camera fails to capture a frame (camera presumed offline)
    print(f"Camera server {device_id+1} attempting to shut down nicely")
    capture.release()
    if writer_state is not None:
        # on an abort the clip is still released and listed, but not transferred
        aborted = abort_event is not None and abort_event.is_set()
        close_writer(writer_state, file_transfer_processes, manifest, config.save_path, transfer=not aborted)

    file_transfer_processes = release_finished(file_transfer_processes)
    print(
        f"Cam_server Waiting for {len(file_transfer_processes)} file transfer processes to finish"
    )
    _ = wait_for_transfers(file_transfer_processes, abort_event)  # an abort arriving meanwhile is honoured too

    print(rate_watch.summary())  # a shortfall is said once when it starts; the tally says whether it persisted
    print(f"ratrixcam server {device_id+1}: shutdown complete; saved {clip_count} files during this run")


def main():
    stop_event = multiprocessing.Event()

    def int_handler(_sig: int, _frame: FrameType | None):
        print(
            "Received signal to terminate, shutting down gracefully.\nTo force exit, press Ctrl+C again."
        )
        stop_event.set()
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(signal.SIGTERM, lambda sig, frame: stop_event.set())

    parser = argparse.ArgumentParser(description="ratrixcam camera server", add_help=False)
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    _ = parser.add_argument("-i", "--index", type=int, required=True)
    args = vars(parser.parse_args())

    config = load_settings(args["config"])  # pyright: ignore[reportAny]  # argparse hands back Any
    if config is None:
        print("ERROR: Cannot load settings")
        return

    device_id = int(args["index"]) - 1  # pyright: ignore[reportAny]  # argparse hands back Any
    if device_id < 0:
        print("ERROR: Invalid device index. Must be a positive integer.")
        return

    run(config, device_id, stop_event)


if __name__ == "__main__":
    main()
