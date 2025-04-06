import os
import shutil


def create_directory(path: str) -> bool:
    # if the temp streaming directory doesn't exist create it
    try:  #  create a new empty one
        os.mkdir(path)
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


def check_for_config_file(config_file: str):
    if not os.path.isfile(config_file):
        try:
            shutil.copy(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "default_config.json"
                ),
                config_file,
            )
        except Exception as e:
            print("Fatal Error: failed to find settings file " + config_file)
            print(type(e), e)
            return
