import os
import shutil
import signal
import subprocess
import time
from datetime import datetime

import ratrix_utils
from ratrixcam_IO import (
    Config,  # object for configuration
    configFile,  # full path to config file
    load_settings,  # function to load config file
)


def removeCamStill(blankImage: str, stillFolder: str, cam_idx: int):
    # this function replaces the most recent still image of camera with the default offline image
    camID = str(cam_idx + 1).zfill(2)  # note the file names go from 01 to Ncameras
    shutil.copy(
        blankImage, os.path.join(stillFolder, f"cam_{camID}_status.png")
    )  # NB this will force overwrite by default
    #print("Updated still to show camera", camID, "is offline")


def moveTempVideos(config: Config) -> bool:
    # if no temp folder exists, return
    if not os.path.exists(config.tempStreamPath):
        return True
    # if temp folder is empty, return
    d = os.listdir(config.tempStreamPath)
    if len(d) == 0:
        return True

    # TO BE WRITTEN IF NOT EMPTY MOVE STRAY FILES TO SAFETY
    # this should use the copyfile function used to routinely move the files
    print("Cleaning up temporary video folder...")
    return False
    # for i in range(len(d)):
    # check if it is a folder, or look for the expected folders 01 thru 08
    # check if it contains videos .mp4 files or anything else
    # use the file dates to construct outfile path for each date
    # create directories if needed
    # move all the files to the outfile directory
    # confirm moved, then delete the folder in the temp location


def initializeTempFolder(config: Config) -> bool:
    if os.path.exists(config.tempStreamPath):
        return True
        # d = os.listdir(config.tempStreamPath)
        # if len(d) == 0:
        #     return True
        # # if not empty,try to empty it
        # try:
        #     return moveTempVideos(config)
        # except Exception as e:
        #     print(f"An unexpected error occurred: {e}")
        #     return False
    else:
        return ratrix_utils.create_directory(config.tempStreamPath)


def initializeOutFolder(config: Config):
    if os.path.exists(config.out_path):
        print(config.out_path, "already exists")
        return True
    try:
        os.mkdir(config.out_path)
    except PermissionError:
        print("Error: Permission denied. Unable to create", config.out_path)
    except OSError as e:
        print(f"OS Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    return True


def graceful_shutdown(
    config: Config | None,
    p: list[subprocess.Popen[bytes] | None],
    p_TTL: subprocess.Popen[bytes] | None,
):
    print("Multicam attempting to shut down nicely")
    for i, process in enumerate(p):
        if process is None:
            continue
        process.terminate()
    if p_TTL is not None:
        p_TTL.terminate()

    # wait up to 10 seconds for the child processes to exit
    tries = 100
    for _ in range(tries):
        all_cameras_terminated = all(
            process is None or process.returncode is not None for process in p
        )
        TTL_terminated = p_TTL is None or p_TTL.returncode is not None
        if all_cameras_terminated and TTL_terminated:
            break
        time.sleep(0.1)  # wait a bit before trying again

    # in case termination fails, kill
    for i, process in enumerate(p):
        if process is None:
            continue
        print(
            f"Camera {config.camera_names[i] if config is not None else i} failed to terminate, killing process"
        )
        process.kill()
    if p_TTL is not None:
        print("TTL logging failed to terminate, killing process")
        p_TTL.kill()

    print("Done cleaning up; Exiting ratrix_multicam.")


# Main loop: check every second and restart any cameras or processes that are not running
def main():
    ratrix_utils.check_for_config_file(configFile)

    # --------------------------------------------------------------
    print("Accessing settings file...")  # get settings from the json file
    config = load_settings(configFile)
    if config is None:
        print("Error loading settings file")
        return
    print("Settings for ", config.study_label, " successfully loaded")

    # check the paths
    # print('checking output path')
    if not os.path.exists(config.out_path):
        print("Fatal Error: No storage disk found")
        return

    # check if folder exists for still image status files, or try to create it
    # print('checking stills path')
    if not os.path.exists(config.stillFolder):
        print("Still folder does not exist, creating now.")
        try:
            os.mkdir(config.stillFolder)
        except IOError as e:
            print(f"An IOError occurred: {e}")
            print("Can neither find nor create folder for update images")
            return

    # print('checking temp path')
    ok = initializeTempFolder(
        config
    )  # STILL NOT WRITTEN: IF temp directory is not empty must clean up NOT delete!
    if not ok:
        print("issue with temp folder, try cleanup?")
        return

    print("attemping to initialize output folder")
    ok = initializeOutFolder(config)
    if not ok:
        print("something went wrong setting up output folder!")
        return
    
    # get rid of any old still images
    print('Removing any old still images')
    for camera_idx in range(config.Ncameras):
        removeCamStill(config.blankImage, config.stillFolder, camera_idx)

    print("Multicam: Starting cameras...")
    # try to start all the cameras
    command = [
        [
            "python3",
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "ratrix_cam_server.py"
            ),
            "-c",
            str(configFile),
            "-i",
            str(index + 1),  # numerical from 1-8
        ]
        for index in range(config.Ncameras)
    ]  # to save the camera starting commands for re-use

    # if applicable, try to start a TTL monitoring process
    command_TTL = [
        "python3",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ttl_server.py"),
    ]

    camera_processes: list[subprocess.Popen[bytes] | None] = [
        None for _ in range(config.Ncameras)
    ]  # to contain a list of processes
    camera_state: list[bool] = [False for _ in range(config.Ncameras)]
    p_TTL = None

    _ = signal.signal(
        signal.SIGINT,
        lambda sig, frame: graceful_shutdown(config, camera_processes, p_TTL),
    )

    while True:
        # otherwise check all cameras and restart if needed
        for camera_idx, process in enumerate(camera_processes):
            cam_up_prev = camera_state[camera_idx]
            camera_state[camera_idx] = process is not None and process.poll() is None
            if camera_state[camera_idx]:
                continue
            if cam_up_prev:
                removeCamStill(config.blankImage, config.stillFolder, camera_idx)
                print(
                    f"Camera {config.camera_names[camera_idx]} went offline at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
                )
            try:
                camera_processes[camera_idx] = subprocess.Popen(command[camera_idx])
                print(f"Started camera {config.camera_names[camera_idx]}")
            except Exception as _:
                pass
                # print(type(e), e)  # let user know it is dead

        # check the TTL process and restart if applicable
        if config.recording_ttl:
            if p_TTL is not None and p_TTL.poll() is None:
                continue
            # if not running, try to relaunch
            try:
                p_TTL = subprocess.Popen(command_TTL)
                print("TTL logging restarted")
            except Exception as e:
                print(type(e), e)
                print("Cannot restart TTL logging")

        time.sleep(1)


# If in recording mode, initialize the cameras and any other processes on entry
# is this the correct way to implement a main guard for this section of the code?
if __name__ == "__main__":
    main()
