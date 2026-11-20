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
    silent. Clips started before `settled_at` (rig_settled_at) are not compared."""

    def __init__(self, name: str, rate: Rate, settled_at: float):
        self.name: str = name
        self.rate: Rate = rate
        self.settled_at: float = settled_at
        self.departed: bool = False

    def budget(self, seconds: float) -> float:
        """Frames a clip of this length may differ from the prediction and still be steady."""
        return 1 + self.rate.uncertainty * seconds

    def clip_closed(self, frames: int, seconds: float, clip_started: float) -> str | None:
        """The line to print for a clip of this many frames over this many seconds, or None."""
        if clip_started < self.settled_at or seconds <= 0:
            return None
        expected = self.rate.fps * seconds
        departed = abs(frames - expected) > self.budget(seconds)
        if departed == self.departed:
            return None
        self.departed = departed
        if departed:
            short = expected - frames
            how = f"{short:.0f} short of" if short > 0 else f"{-short:.0f} more than"
            return (
                f"WARNING: camera {self.name} delivered {frames} frames in a {seconds:.1f}-s clip, {how} the "
                + f"{expected:.0f} expected at {self.rate.fps:.2f} fps"
            )
        return f"camera {self.name} delivers {self.rate.fps:.2f} fps again"


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
    gaps = sum(1 for i in intervals if i > 2.5 * typical)
    return rate, uncertainty, gaps


class Grabbed(NamedTuple):
    frame: MatLike
    arrived: float  # the clock (time.time()) the moment the frame came back from the capture: its time stamp
    waited: float  # seconds spent in the capture waiting for it


def grab_frame(capture: cv2.VideoCapture, name: str) -> Grabbed | None:
    """The next frame with the time it arrived, read just after it came back so that the
    stamp is the frame's own and not the moment the wait for it began. Up to three reads,
    since the backend gives up on a slow camera after a while; None, after a line, when the
    camera delivers nothing."""
    asked = time.time()
    for _ in range(3):
        ok, frame = capture.read()
        if ok:
            arrived = time.time()
            return Grabbed(frame, arrived, arrived - asked)
    print(f"Cam_server: Failed to capture a frame from camera {name} at {datetime.now().strftime('%H:%M:%S.%f')}")
    return None


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


FRAME_TIMES_SUFFIX = "_frames.csv"  # beside each clip: one line per frame with its arrival time
FRAME_TIMES_HEADER = "frame,arrived,waited_s,worked_s\n"


def frame_times_filename(clip_filename_: str) -> str:
    """The name of the frame-time file that goes with a clip."""
    stem, _ = os.path.splitext(clip_filename_)
    return stem + FRAME_TIMES_SUFFIX


class FrameTimes:
    """The frame-time file of one clip: for every frame its number, the clock when it
    arrived, the seconds the loop waited for it and the seconds the loop worked between the
    previous frame and asking for this one. A gap after a long wait is a frame the camera
    or the link did not deliver; a gap after long work is a frame that was overwritten while
    the loop was busy; the clip alone cannot tell the two apart."""

    def __init__(self, path: str):
        self.path: str = path
        self.file: TextIO | None = None  # None when the file could not be opened: the clip goes on without it
        try:
            self.file = open(path, "w")
            _ = self.file.write(FRAME_TIMES_HEADER)
        except OSError as e:
            print(f"WARNING: could not open the frame-time file {os.path.basename(path)}: {e.strerror or e}; the clip is recorded without it")

    def add(self, frame: int, arrived: float, waited: float, worked: float) -> None:
        if self.file is None:
            return
        stamp = datetime.fromtimestamp(arrived).strftime("%Y-%m-%d %H:%M:%S.%f")
        _ = self.file.write(f"{frame},{stamp},{waited:.4f},{worked:.4f}\n")

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

    frame_count: int = 0  # frames since the camera started: printed on each frame, paces the still updates
    clip_first_frame: int = 0  # frame_count when the open clip started
    clip_count: int = 0
    file_transfer_processes: list[Process] = []
    writer_state: WriterState | None = None
    last_arrived: float = time.time()  # when the previous frame came back; the loop's work since then goes in the frame-time file

    # this loop is executed once per video frame until the camera is stopped
    while capture.isOpened() and not stop_event.is_set():
        # the frame first: its arrival time is its stamp and decides which clip it opens or joins
        grabbed = grab_frame(capture, params.name)
        if grabbed is None:
            break  # if you fail to capture, interpret that camera is down, fall out of loop
        current_time: float = grabbed.arrived
        current_datetime: datetime = datetime.fromtimestamp(timestamp=current_time)

        # if not started yet, open first video file
        # or if video slice duration has been exceeded, close video file and initialize new one
        if writer_state is None or current_time - start > config.time_slice:
            # close the old writer, compare its clip's frame count with the camera's rate, and
            # let go of the transfers that have finished since the last clip
            if writer_state is not None:
                close_writer(writer_state, file_transfer_processes, manifest, config.save_path)
                message = rate_watch.clip_closed(frame_count - clip_first_frame, current_time - start, start)
                if message is not None:
                    print(message)
                file_transfer_processes = release_finished(file_transfer_processes)
            start = current_time  # the clip's clock starts at its first frame; the clip ends at the first frame after time_slice
            clip_first_frame = frame_count
            clip_count += 1

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

        overlay_text: str = camera_label + ' frame ' + str(frame_count)
        write_frame(writer_state.writer, grabbed.frame, params, current_datetime, overlay_text)
        writer_state.times.add(frame_count, grabbed.arrived, grabbed.waited, grabbed.arrived - grabbed.waited - last_arrived)
        last_arrived = grabbed.arrived

        # once per N sec, try to update the still image
        if frame_count % (config.preview_interval * params.fps) == 0:
            try:
                result = cv2.imwrite(camera_still_path, grabbed.frame)
                if result is False:
                    print(
                        f"WARNING: Failed to write still image to {camera_still_path}"
                    )
            except Exception as e:
                print(type(e), e)
        frame_count += 1  # whether the still update succeeds or fails

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
