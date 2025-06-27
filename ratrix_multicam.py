import argparse
import multiprocessing
import shutil
import signal
import subprocess
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
    reset_stills,
    still_path,
)


def count_video_devices():
    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = result.stdout
        video_device_count = 0
        for line in output.splitlines():
            if "USB Camera:" in line:
                video_device_count += 1

        return video_device_count

    except Exception as e:
        print(f"Error detecting video devices: {e}")
        return 0


def run_without_handlers(config: Config, camera_idx: int, stop_event: Event):
    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ratrix_cam_server.run(config, camera_idx, stop_event)


# Main loop: check every second and restart any cameras or processes that are not running
def run(config: Config, stop_event: Event):
    print(f"Settings for '{config.study_label}' successfully loaded")

    if not ensure_dir_exists(config.stills_path):
        print("ERROR: Stills folder does not exist and could not be created")
        return
    else:
        print(f"Stills folder: {config.stills_path}")

    # print('checking temp path')
    if not ensure_dir_exists(config.temp_path):
        print("ERROR: Temp streaming folder does not exist and could not be created")
        return
    else:
        print(f"Temp streaming folder: {config.temp_path}")

    if not ensure_dir_exists(config.save_path):
        print("ERROR: Recording folder does not exist and could not be created")
        return
    else:
        print(f"Recording folder: {config.save_path}")

    print("Removing any old still images")
    reset_stills(config)

    print("Multicam: Starting cameras...")

    num_cameras = len(config.cameras)

    camera_processes: list[Process | None] = [None for _ in range(num_cameras)]
    camera_state: list[bool] = [False for _ in range(num_cameras)]
    p_TTL: Process | None = None

    devices = 0
    while not stop_event.is_set():
        prev_devices = devices
        devices = count_video_devices()
        have_all_cameras = devices >= num_cameras
        if not have_all_cameras:
            if prev_devices != devices:
                print(
                    f"Only detected {devices}/{num_cameras} camera(s), waiting for all to be connected"
                )

        just_started_a_cam = False
        for idx, (process, cam_up_prev, camera_config) in enumerate(
            zip(camera_processes, camera_state, config.cameras)
        ):
            camera_state[idx] = process is not None and process.is_alive()
            if camera_state[idx]: #still running
                continue
            elif cam_up_prev:# not running, but was previously: indicate offline in GUI and terminal
                _ = shutil.copyfile(
                    config.blank_image,
                    still_path(config.stills_path, camera_config.name),
                )
                print(
                    f"Camera {camera_config.name} went offline at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
                )
            if not have_all_cameras:
                continue
            # only when all devices are detected, try to re-launch the ones that went offline
            try: 
                if just_started_a_cam: # if another camera was already launched within this loop
                    time.sleep(1)  # wait a bit before trying to launch another one 
                cam_proc = Process(
                    target=run_without_handlers,
                    args=(config, idx, stop_event),
                )
                cam_proc.start()
                camera_processes[idx] = cam_proc
                just_started_a_cam=True
                print(f"Started camera {camera_config.name}")
                
            except Exception as e:
                print(f"Error starting camera {camera_config.name}:", e)

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
        for process, camera_config in zip(camera_processes, config.cameras):
            if process is None or not process.is_alive():
                continue
            print(f"Camera {camera_config.name} failed to terminate, killing process")
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
