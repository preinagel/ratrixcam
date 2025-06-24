import os
import shutil

from pydantic import BaseModel, ValidationError


class CameraConfig(BaseModel):
    name: str
    row: int
    col: int
    fps: int | None = None
    width: int | None = None
    height: int | None = None
    exposure: float | None = None  # LUT code for camera exposure setting, eg -8


class Config(BaseModel):
    rack_name: str
    cameras: list[CameraConfig]
    study_label: str
    default_fps: int
    default_width: int
    default_height: int
    default_cam_exposure: float  # LUT code for camera exposure setting, eg -8
    time_slice: int
    preview_interval: int
    codec: str  # cv2 video codec, eg MJPG
    video_ext: str  # extension for video files eg .mp4
    save_path: str  # final destination folder for video files
    temp_path: str  # temporary folder for video files while streaming
    blank_image: str  # full path to image to display when cameras offline
    stills_path: str  # folder containing most recent grabbed frames
    recording_audio: bool  # not currently supported
    recording_ttl: bool  # not currently supported


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


def still_path(stills_path: str, camera_name: str) -> str:
    return os.path.join(stills_path, f"cam_{camera_name}_status.png")


def reset_stills(config: Config):
    for file in os.listdir(config.stills_path):
        file_path = os.path.join(config.stills_path, file)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error deleting file {file_path}: {e}")

    # copy stills blank image to stills folder
    for camera in config.cameras:
        _ = shutil.copy(config.blank_image, still_path(config.stills_path, camera.name))


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
