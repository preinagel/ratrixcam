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
    CAMERA_LAUNCH_PAUSE,
    Calibration,
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


DEVICE_COUNT_TIMEOUT = 30  # seconds for system_profiler to answer; it has been seen to hang for days
USB_MODEL_PREFIX = "UVC Camera"  # how the Model ID of every USB video class camera begins; a built-in
                                 # or iPhone camera reports something else


def count_video_devices(non_usb: set[str]) -> int | None:
    """The number of cameras the system reports, of any make (every entry carries one
    "Unique ID" line), or None when it could not be counted because the query failed or
    timed out; either is said on the terminal. A query that times out is killed, so a hung
    one cannot stall the supervisor. A camera that is not a USB camera (a built-in or an
    iPhone camera, by its Model ID) counts like any other and is opened as one of the
    streams, which is rarely intended, so a WARNING names it when it appears; `non_usb`
    holds the ones named on the previous pass and is kept current here, so that the warning
    is printed once per change rather than on every pass, like the count messages."""
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


def run_without_handlers(
    config: Config, camera_idx: int, stop_event: Event, abort_event: Event | None = None, calibration: Calibration | None = None
):
    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ratrixcam_server.run(config, camera_idx, stop_event, abort_event, calibration)


# Main loop: check every second and restart any cameras or processes that are not running
def run(config: Config, stop_event: Event, abort_event: Event | None = None, calibration: Calibration | None = None):
    """abort_event, when given and set by the launcher, tells each camera server at its own
    shutdown to leave its last clip and its pending transfers in temp instead of waiting
    for the drive; it is passed straight through to the servers, as is calibration, the
    cameras' measured frame rates."""
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

    devices = 0  # the last count that succeeded
    non_usb: set[str] = set()  # the cameras that are not USB cameras, as warned about on the last pass
    while not stop_event.is_set():
        counted = count_video_devices(non_usb)
        if counted is None:
            have_all_cameras = False  # unknown this pass: nothing is launched; the cameras are still watched below
        else:
            # exactly the configured number, for launch and relaunch alike: process i opens
            # device i, which is right only while the device list holds the configured cameras
            # and nothing else; with one more counted, a dropped camera's relaunch would open
            # the camera above it, already recorded by its own process (GitHub #66)
            have_all_cameras = counted == num_cameras
            if counted < num_cameras and counted != devices:
                print(
                    f"Only detected {counted}/{num_cameras} camera(s), waiting for all to be connected"
                )
            elif counted > num_cameras and counted != devices:
                print(
                    f"Detected {counted} camera(s), {num_cameras} configured: nothing is launched until the count matches "
                    + "(configure the extra camera, or disconnect or disable it)"
                )
            devices = counted

        launched_this_pass = False
        for idx, (process, was_alive, camera_config) in enumerate(
            zip(camera_processes, camera_state, config.cameras)
        ):
            name = camera_name(config.subject_prefix, camera_config.subjID, camera_config.view)
            camera_state[idx] = process is not None and process.is_alive()
            if camera_state[idx]: #still running
                continue
            elif was_alive:  # not running, but was on the previous pass: indicate offline in GUI and terminal
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
                if launched_this_pass:  # another camera was launched within this pass of the loop:
                    time.sleep(CAMERA_LAUNCH_PAUSE)  # wait a bit before launching another one
                cam_proc = Process(
                    target=run_without_handlers,
                    args=(config, idx, stop_event, abort_event, calibration),
                )
                cam_proc.start()
                camera_processes[idx] = cam_proc
                launched_this_pass = True
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
    running = sum(1 for process in camera_processes if process is not None and process.is_alive())
    print(f"Waiting for {running} camera process(es) to terminate...")
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
