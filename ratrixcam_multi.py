import argparse
import multiprocessing
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from multiprocessing import Process
from multiprocessing.synchronize import Event
from threading import Thread
from types import FrameType

import ratrixcam_server
from ratrixcam_naming import camera_name, still_path
from ratrixcam_utils import (
    BLANK_IMAGE,
    Config,
    ensure_dir_exists,
    load_settings,
    place_offline_stills,
    stills_dir,
)

# NOT IMPLEMENTED, KEPT FOR A FUTURE VERSION: audio recording and TTL input (GitHub #13). The
# code for them is commented out, not deleted, so that it can be revived with the config options
# that would switch them on.
# RECORDING_AUDIO = False
# RECORDING_TTL = False


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


def run_without_handlers(config: Config, camera_idx: int, stop_event: Event, abort_event: Event | None = None):
    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ratrixcam_server.run(config, camera_idx, stop_event, abort_event)


# Main loop: check every second and restart any cameras or processes that are not running
def run(config: Config, stop_event: Event, abort_event: Event | None = None):
    """abort_event, when given and set by the launcher, tells each camera server at its own
    shutdown to leave its last clip and its pending transfers in temp instead of waiting
    for the drive; it is passed straight through to the servers."""
    print("Settings successfully loaded")

    stills_folder = stills_dir(config.temp_path)
    if not ensure_dir_exists(stills_folder):
        print("ERROR: Stills folder does not exist and could not be created")
        return

    if not ensure_dir_exists(config.temp_path):
        print("ERROR: Temp streaming folder does not exist and could not be created")
        return

    if not os.path.isdir(config.save_path):  # never created here; see ratrixcam_IO
        print(f"ERROR: Save drive folder not found: {config.save_path}")
        return

    place_offline_stills(config)

    print("Multicam: Starting cameras...")

    num_cameras = len(config.cameras)
    print('Expecting ',num_cameras,'cameras based on config file...')

    camera_processes: list[Process | None] = [None for _ in range(num_cameras)]
    camera_state: list[bool] = [False for _ in range(num_cameras)]
    # p_TTL: Process | None = None  # TTL input, not implemented (see the top of the file)

    devices = 0
    while not stop_event.is_set():
        prev_devices = devices
        devices = count_video_devices()
        if devices < num_cameras:#only report if not enough cameras to launch
            print('seeing ',devices,'devices; was expecting',num_cameras)
        
        have_all_cameras = devices >= num_cameras #note if too many will grab the first ones
        if not have_all_cameras:
            if prev_devices != devices:
                print(
                    f"Only detected {devices}/{num_cameras} camera(s), waiting for all to be connected"
                )

        just_started_a_cam = False
        for idx, (process, cam_up_prev, camera_config) in enumerate(
            zip(camera_processes, camera_state, config.cameras)
        ):
            name = camera_name(config.subject_prefix, camera_config.subjID, camera_config.view)
            camera_state[idx] = process is not None and process.is_alive()
            if camera_state[idx]: #still running
                continue
            elif cam_up_prev:# not running, but was previously: indicate offline in GUI and terminal
                try:
                    _ = shutil.copyfile(BLANK_IMAGE, still_path(stills_folder, name))
                except OSError as e:  # the still is cosmetic; the supervisor must outlive it
                    print(f"WARNING: could not place the offline image for camera {name}: {e.strerror or e}")
                print(
                    f"Camera {name} went offline at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
                )
            if not have_all_cameras:
                continue
            # only when all devices are detected, try to re-launch the ones that went offline
            try: 
                if just_started_a_cam: # if another camera was already launched within this loop,
                    time.sleep(5)  # wait a bit before trying to launch another one
                cam_proc = Process(
                    target=run_without_handlers,
                    args=(config, idx, stop_event, abort_event),
                )
                cam_proc.start()
                camera_processes[idx] = cam_proc
                just_started_a_cam=True
                print(f"Multicam: Started camera {name}")

            except Exception as e:
                print(f"Multicam: error starting camera {name}:", e)

        # TTL input, not implemented (see the top of the file): check its process and restart it
        # if RECORDING_TTL:
        #     if p_TTL is not None and p_TTL.is_alive():
        #         continue
        #     try:
        #         p_TTL = Process(target=ttl.run, args=(...))
        #         print("TTL logging restarted")
        #     except Exception as e:
        #         print(type(e), e)
        #         print("Cannot restart TTL logging")
        _ = stop_event.wait(1)

    print("Multicam attempting to shut down nicely")

    timeout = 600  # seconds for the cameras to finish or give up their transfers (a transfer retries for
                   # TRANSFER_RETRY_WINDOW on a failing drive) before they are killed. If on-the-fly
                   # transcoding is revived, allow about half the slice duration on top.
    print("Waiting for child processes to terminate...")
    for _ in range(int(timeout / 0.1)):
        all_cameras_terminated = all(
            process is None or not process.is_alive() for process in camera_processes
        )
        # TTL_terminated = p_TTL is None or not p_TTL.is_alive()  # TTL input, not implemented
        if all_cameras_terminated:
            break
        time.sleep(0.1)  # wait a bit before trying again
    else:
        print("Multicam: Timed out waiting for child processes to terminate, killing")
        # in case termination fails, kill
        for process, camera_config in zip(camera_processes, config.cameras):
            if process is None or not process.is_alive():
                continue
            name = camera_name(config.subject_prefix, camera_config.subjID, camera_config.view)
            print(f"Camera {name} failed to terminate, killing process")
            process.kill()
        # TTL input, not implemented (see the top of the file)
        # if p_TTL is not None:
        #     print("TTL logging failed to terminate, killing process")
        #     p_TTL.kill()

    print("ratrixcam multi: shutdown complete")


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
        description="ratrixcam multi-camera supervisor", add_help=False
    )
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    args = vars(parser.parse_args())

    print("Accessing settings file...")  # get settings from the json file
    config = load_settings(args["config"])  # pyright: ignore[reportAny]  # argparse hands back Any
    if config is None:
        return

    run(config, stop_event)


if __name__ == "__main__":
    main()
