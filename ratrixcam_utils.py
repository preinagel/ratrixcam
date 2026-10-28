import os
import shutil
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ratrixcam_naming import camera_name, still_path

Position = Literal["A", "B", "C", "D", "E", "F", "G", "H"]


class CameraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjID: str  # subject identifier without the prefix, e.g. "556"
    view: str  # camera view, e.g. "buddy"
    position: Position  # cell on the 2 x 4 display: A B | E F over C D | G H
    crf: int | None = None  # ffmpeg quality for this camera's clips; default_crf if omitted
    fps: int | None = None
    width: int | None = None
    height: int | None = None
    exposure: float | None = None  # LUT code for camera exposure setting, eg -8


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras: list[CameraConfig]
    study_label: str
    subject_prefix: str = "subj"  # prefixed to every subjID to form the subject name, e.g. "rat"
    default_crf: int = 25  # ffmpeg quality for cameras that do not set their own crf
    default_fps: int
    default_width: int
    default_height: int
    default_cam_exposure: float  # LUT code for camera exposure setting, eg -8
    time_slice: int
    preview_interval: int
    save_path: str  # final destination folder for video files
    temp_path: str  # temporary folder for video files while streaming


def ensure_dir_exists(path: str) -> bool:
    # if the temp streaming directory doesn't exist create it
    try:  #  create a new empty one
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


def ensure_config_file_exists(config_file: str):
    if not os.path.isfile(config_file):
        try:
            _ = shutil.copy(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "default_config.json"
                ),
                config_file,
            )
        except Exception as e:
            print("Fatal Error: failed to find settings file " + config_file)
            print(type(e), e)
            return


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
            f.write("ratrixcam write test\n")
    except OSError as e:
        return e.strerror or str(e)
    try:
        os.remove(probe)
    except OSError as e:
        print(f"WARNING: could not remove the write-test file {probe}: {e.strerror or e}")
    return None


def stills_dir(temp_path: str) -> str:
    """Folder holding the most recent still from each camera, always inside the temp folder."""
    return os.path.join(temp_path, "status")


def reset_stills(config: Config):
    folder = stills_dir(config.temp_path)
    for file in os.listdir(folder):
        file_path = os.path.join(folder, file)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error deleting file {file_path}: {e}")

    # copy stills blank image to stills folder
    for camera in config.cameras:
        name = camera_name(config.subject_prefix, camera.subjID, camera.view)
        _ = shutil.copy(BLANK_IMAGE, still_path(folder, name))


# -----------------------------------------------------------
# BEGIN FUNCTION DEFS
def load_settings(json_path: str) -> Config | None:
    with open(json_path, "r") as myfile:
        json = myfile.read()
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
