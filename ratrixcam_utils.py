import fcntl
import os
import shutil
import subprocess
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ratrixcam_naming import camera_name, still_path

Position = Literal["A", "B", "C", "D", "E", "F", "G", "H"]


class CameraConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    subjID: str  # subject identifier without the prefix, e.g. "556"
    view: str  # camera view, e.g. "buddy"
    position: Position  # cell on the 2 x 4 display: A B | E F over C D | G H
    crf: int | None = None  # ffmpeg quality for this camera's clips; default_crf if omitted
    fps: int | None = None
    width: int | None = None
    height: int | None = None
    exposure: float | None = None  # read but unused on macOS (GitHub #51)


class Config(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    cameras: list[CameraConfig]
    machine_name: str | None = None  # a name for this camera computer, shown by the viewer; the hostname when absent
    study_label: str
    subject_prefix: str = "subj"  # prefixed to every subjID to form the subject name, e.g. "rat"
    default_crf: int = 25  # ffmpeg quality for cameras that do not set their own crf
    default_fps: int
    default_width: int
    default_height: int
    default_cam_exposure: float  # read but unused on macOS (GitHub #51); still required in every config
    time_slice: int
    preview_interval: int
    save_path: str  # final destination folder for video files
    temp_path: str  # temporary folder for video files while streaming


class CameraCalibration(BaseModel):
    """The frame rate one camera delivers when it streams alone, measured by the calibration
    program, with what identifies the camera and the mode the measurement holds for."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    index: int  # the camera's place in the config's list, counted from 0: the index the server opens
    name: str  # the camera name the config gave at calibration time, a check against the config in use
    position: Position
    unique_id: str | None = None  # the system's id of the camera on that port, once it can be read at launch
    requested_width: int  # the mode the calibration was made in; another mode needs its own
    requested_height: int
    requested_fps: int
    width: int  # the frame size the camera delivered
    height: int
    rate: float  # frames per second the camera delivers on its own: frames that arrived, less one, over the span
    uncertainty: float  # of rate: one frame over the span of the measurement
    frames: int  # frames the measurement counted
    seconds: float  # over this long
    gaps: int  # intervals of more than 2.5 typical ones during the measurement: holes of two or more frames


class Calibration(BaseModel):
    """A calibration file: the rate of each camera of a config, measured one camera at a time."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    version: int = 1
    calibrated: str  # when the measurements were made
    code_version: str
    config_file: str  # the config file's name at calibration time
    cameras: list[CameraCalibration]

    def for_camera(self, index: int, name: str, width: int, height: int, fps: int) -> CameraCalibration | None:
        """The entry for the camera at this index, if it was calibrated under this name in this
        requested mode; otherwise None, and the camera counts as uncalibrated."""
        for entry in self.cameras:
            if (entry.index, entry.name, entry.requested_width, entry.requested_height, entry.requested_fps) == (index, name, width, height, fps):
                return entry
        return None


CALIBRATION_SUFFIX = "_fps_calibration.json"  # the calibration file sits beside the config, named after it


def calibration_path(config_path: str) -> str:
    """Where the calibration of a config file is kept unless another path is given."""
    stem, _ = os.path.splitext(config_path)
    return stem + CALIBRATION_SUFFIX


def load_calibration(json_path: str) -> Calibration | None:
    """The calibration file as a Calibration, or None after ERROR lines when it cannot be read
    or fails the checks. Whether a run may go on without one is the caller's decision."""
    try:
        with open(json_path, "r") as f:
            json = f.read()
    except OSError as e:
        print(f"ERROR: cannot read the calibration file {json_path}: {e.strerror or e}")
        return None
    try:
        return Calibration.model_validate_json(json)
    except ValidationError as e:
        print(f"ERROR: the calibration file {json_path} fails its checks:")
        for error in e.errors():
            print(f"  {'.'.join(map(str, error.get('loc', ())))} : {error['msg']}")
        return None


def ensure_dir_exists(path: str) -> bool:
    # create the folder if it does not exist; True if it exists afterwards
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except PermissionError:
        print(
            "Error: Permission denied. Unable to create directory ",
            path,
        )
    except OSError as e:
        print(f"OS Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    return False


# image shown for a camera that has no still yet or has gone offline
BLANK_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blanks", "offline_status.png")


def temp_is_clear(temp_path: str) -> bool:
    """True if the temp folder does not exist or holds nothing but .DS_Store files. A run must
    start on a clear temp folder: leftovers of an earlier run would be mixed into this one."""
    if not os.path.isdir(temp_path):
        return True
    return all(name == ".DS_Store" for name in os.listdir(temp_path))


def not_writable(folder: str) -> str | None:
    """Create and remove a small uniquely named file in folder: None if that works, else the
    reason it did not. A folder can exist and still refuse writes (a drive mounted read-only,
    wrong ownership), which no presence check sees. A test file that cannot be removed
    afterwards is reported but does not count as a failure."""
    probe = os.path.join(folder, f"ratrixcam_write_test_{os.getpid()}.tmp")
    try:
        with open(probe, "w") as f:
            _ = f.write("ratrixcam write test\n")
    except OSError as e:
        return e.strerror or str(e)
    try:
        os.remove(probe)
    except OSError as e:
        print(f"WARNING: could not remove the write-test file {probe}: {e.strerror or e}")
    return None


def opens_as_video(path: str) -> bool:
    """Whether OpenCV can open the file and read a frame from it. A clip whose writer was
    never released (a camera killed mid-clip) has no index and fails here. Used by the
    shutdown cleanup and by the clip transfer."""
    import cv2  # heavy; imported where used

    capture = cv2.VideoCapture(path)
    try:
        return capture.isOpened() and capture.read()[0]
    finally:
        capture.release()


def flush_to_device(path: str) -> None:
    """Push a file's data, or a folder's entries, out of the OS write cache onto the drive, so
    that a drive pulled right afterwards still holds it. F_FULLFSYNC (macOS) also empties the
    drive's own cache; where it is unavailable or rejected, a plain fsync is used. Used by the
    clip transfer and by the shutdown cleanup."""
    fd = os.open(path, os.O_RDONLY)
    try:
        full_sync: int | None = getattr(fcntl, "F_FULLFSYNC", None)
        if full_sync is not None:
            try:
                _ = fcntl.fcntl(fd, full_sync)
                return
            except OSError:
                pass
        os.fsync(fd)
    finally:
        os.close(fd)


# Seconds the supervisor waits between launching one camera and the next. The camera servers
# use it to work out when the whole rig has started.
CAMERA_LAUNCH_PAUSE = 5

DEVICE_COUNT_TIMEOUT = 30  # seconds for system_profiler to answer; it has been seen to hang for days
USB_MODEL_PREFIX = "UVC Camera"  # how the Model ID of every USB video class camera begins; a built-in
                                 # or iPhone camera reports something else


def count_video_devices(non_usb: set[str]) -> int | None:
    """The number of cameras the system reports, of any make (every entry carries one
    "Unique ID" line), or None when it could not be counted because the query failed or
    timed out; either is said on the terminal. A query that times out is killed, so a hung
    one cannot stall the caller. OpenCV opens camera k as index k only while the system
    lists exactly the configured cameras, so the supervisor launches, and the calibration
    program measures, only when this count equals the config's. A camera that is not a USB
    camera (a built-in or an iPhone camera, by its Model ID) counts like any other and is
    opened as one of the streams, which is rarely intended, so a WARNING names it when it
    appears; `non_usb` holds the ones named on the previous call and is kept current here,
    so that the warning is printed once per change rather than on every pass, like the
    supervisor's count messages."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=DEVICE_COUNT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"WARNING: the camera count timed out after {DEVICE_COUNT_TIMEOUT} s; no camera is launched until it answers")
        return None
    except Exception as e:
        print(f"Error detecting video devices: {e}")
        return None
    count = 0
    name = "camera"  # the entry's heading, the line that precedes its Model ID
    present: set[str] = set()
    for line in result.stdout.splitlines():
        text = line.strip()
        if "Unique ID:" in text:
            count += 1
        elif text.startswith("Model ID:"):
            model = text.removeprefix("Model ID:").strip()
            if not model.startswith(USB_MODEL_PREFIX):
                present.add(f"{name} (Model ID: {model})")
        elif text.endswith(":") and line.startswith(" "):
            name = text[:-1]
    for camera in sorted(present - non_usb):
        print(f"WARNING: {camera} is not a USB camera; it counts as one of the camera streams and is opened as one unless disabled")
    non_usb.clear()
    non_usb.update(present)
    return count


# Assumed write rate of one camera, used to estimate how long a drive will last. A ballpark
# constant based on sample 640x480 30fps recordings. It could be replaced by a rate measured
# from the run's own clips.
ASSUMED_WRITE_RATE_PER_CAMERA = 1e9 / 60 / 8  # bytes per second


def write_rate(n_cameras: int) -> float:
    """The assumed write rate of a rig with this many cameras."""
    return ASSUMED_WRITE_RATE_PER_CAMERA * n_cameras


def seconds_until_full(free_bytes: int, rate: float) -> float:
    """Expected time until a drive with this much free space is full, at this write rate."""
    return free_bytes / rate


def stills_dir(temp_path: str) -> str:
    """Folder holding the most recent still from each camera, always inside the temp folder."""
    return os.path.join(temp_path, "status")


def place_offline_stills(config: Config):
    """Put the offline placeholder image in the stills folder under each camera's still name,
    so that a camera shows as offline until its first real still replaces it. The folder is
    empty at this point: launch refuses to start on a non-empty temp folder."""
    folder = stills_dir(config.temp_path)
    for camera in config.cameras:
        name = camera_name(config.subject_prefix, camera.subjID, camera.view)
        _ = shutil.copy(BLANK_IMAGE, still_path(folder, name))


def load_settings(json_path: str) -> Config | None:
    """The config file as a Config, or None after one ERROR line: the file cannot be read, or
    its content fails the checks."""
    try:
        with open(json_path, "r") as myfile:
            json = myfile.read()
    except OSError as e:
        print(f"ERROR: cannot read the config file {json_path}: {e.strerror or e}")
        return None
    try:
        config = Config.model_validate_json(json)
    except ValidationError as e:
        print(f"Error loading settings from {json_path}:")
        for error in e.errors():
            if "loc" in error:
                print(f"  {'.'.join(map(str, error['loc']))} : {error['msg']}")
            else:
                print(f"  {error['msg']}")
        return None
    return config
