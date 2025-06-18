import ctypes
import ctypes.util
import os
import shutil
import signal

from pydantic import BaseModel, ValidationError

# https://github.com/torvalds/linux/blob/v5.11/include/uapi/linux/prctl.h#L9
PR_SET_PDEATHSIG = 1


def set_pdeathsig():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        raise OSError(ctypes.get_errno(), "SET_PDEATHSIG")


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


class Config(BaseModel):
    rack_name: str
    camera_names: list[str]
    camera_rows: list[int]
    camera_cols: list[int]
    Ncameras: int
    study_label: str
    fps: int
    width: int
    height: int
    time_slice: int
    preview_interval: int
    cam_exposure: float  # LUT code for camera exposure setting, eg -8
    codec: str  # cv2 video codec, eg MJPG
    video_ext: str  # extension for video files eg .mp4
    out_path: str  # final destination folder for video files
    tempStreamPath: str  # temporary folder for video files while streaming
    blankImage: str  # full path to image to display when cameras offline
    stillFolder: str  # folder containing most recent grabbed frames
    recording_audio: bool  # not currently supported
    recording_ttl: bool  # not currently supported


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
