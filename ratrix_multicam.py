import argparse
import multiprocessing
import os
import shutil
import signal
import time
from datetime import datetime
from multiprocessing import Process
from multiprocessing.synchronize import Event
from threading import Thread
from types import FrameType

import ratrix_cam_server
from ratrix_utils import (
    Config,
    ensure_dir_exists,
    load_settings,
    set_pdeathsig,
)


def removeCamStill(blankImage: str, stillFolder: str, cam_idx: int):
    # this function replaces the most recent still image of camera with the default offline image
    camID = str(cam_idx + 1).zfill(2)  # note the file names go from 01 to Ncameras
    _ = shutil.copy(blankImage, os.path.join(stillFolder, f"cam_{camID}_status.png"))


# Main loop: check every second and restart any cameras or processes that are not running
def run(config: Config, stop_event: Event):
    print(f"Settings for '{config.study_label}' successfully loaded")

    if not ensure_dir_exists(config.stillFolder):
        print("ERROR: Stills folder does not exist and could not be created")
        return
    else:
        print(f"Stills folder: {config.stillFolder}")

    # print('checking temp path')
    if not ensure_dir_exists(config.tempStreamPath):
        print("ERROR: Temp streaming folder does not exist and could not be created")
        return
    else:
        print(f"Temp streaming folder: {config.tempStreamPath}")

    if not ensure_dir_exists(config.out_path):
        print("ERROR: Recording folder does not exist and could not be created")
        return
    else:
        print(f"Recording folder: {config.out_path}")

    # get rid of any old still images
    print("Removing any old still images")
    for camera_idx in range(config.Ncameras):
        removeCamStill(config.blankImage, config.stillFolder, camera_idx)

    print("Multicam: Starting cameras...")

    camera_processes: list[Process | None] = [
        None for _ in range(config.Ncameras)
    ]  # to contain a list of processes
    camera_state: list[bool] = [False for _ in range(config.Ncameras)]
    p_TTL: Process | None = None

    while not stop_event.is_set():
        for camera_idx, process in enumerate(camera_processes):
            cam_up_prev = camera_state[camera_idx]
            camera_state[camera_idx] = process is not None and process.is_alive()
            if camera_state[camera_idx]:
                continue
            if cam_up_prev:
                removeCamStill(config.blankImage, config.stillFolder, camera_idx)
                print(
                    f"Camera {config.camera_names[camera_idx]} went offline at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
                )
            try:

                def run_without_handlers():
                    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
                    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    set_pdeathsig()
                    ratrix_cam_server.run(config, camera_idx, stop_event)

                new_proc = Process(target=run_without_handlers)
                new_proc.start()
                camera_processes[camera_idx] = new_proc
                print(f"Started camera {config.camera_names[camera_idx]}")
            except Exception as _:
                pass
                # print(type(e), e)  # let user know it is dead

        # check the TTL process and restart if applicable
        if config.recording_ttl:
            raise Exception("TTL server not implemented")
            if p_TTL is not None and p_TTL.is_alive():
                continue
            # if not running, try to relaunch
            try:
                # p_TTL = Process(target=ttl.run, args=(...))
                print("TTL logging restarted")
            except Exception as e:
                print(type(e), e)
                print("Cannot restart TTL logging")
        _ = stop_event.wait(1)

    print("Multicam attempting to shut down nicely")

    timeout = 10
    print("Waiting for child processes to terminate...")
    for _ in range(int(timeout / 0.1)):
        all_cameras_terminated = all(
            process is None or not process.is_alive() for process in camera_processes
        )
        TTL_terminated = p_TTL is None or not p_TTL.is_alive()
        if all_cameras_terminated and TTL_terminated:
            break
        time.sleep(0.1)  # wait a bit before trying again
    else:
        print("Multicam: Timed out waiting for child processes to terminate, killing")
        # in case termination fails, kill
        for i, process in enumerate(camera_processes):
            if process is None or not process.is_alive():
                continue
            print(
                f"Camera {config.camera_names[i]} failed to terminate, killing process"
            )
            process.kill()
        if p_TTL is not None:
            print("TTL logging failed to terminate, killing process")
            p_TTL.kill()

    print("Ratrix Multicam: Shutdown complete")


def main():
    stop_event = multiprocessing.Event()

    def stop():
        # Thread is needed to prevent dead lock with stop_event.wait
        Thread(target=stop_event.set).start()

    def int_handler(_sig: int, _frame: FrameType | None):
        print(
            "Received signal to terminate, shutting down gracefully.\nTo force exit, press Ctrl+C again."
        )
        stop()
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(signal.SIGTERM, lambda sig, frame: stop())

    parser = argparse.ArgumentParser(
        description="Ratrix Multi-Camera Setup", add_help=False
    )
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    args = vars(parser.parse_args())

    print("Accessing settings file...")  # get settings from the json file
    config = load_settings(args["config"])
    if config is None:
        return

    run(config, stop_event)


# If in recording mode, initialize the cameras and any other processes on entry
# is this the correct way to implement a main guard for this section of the code?
if __name__ == "__main__":
    main()
