"""Measure the frame rate each camera of a config delivers on its own, one camera at a time,
and write the calibration file the camera program reads at launch.

    python3 ratrixcam_calibrate.py -c <config.json> [-o <calibration.json>] [--discard 30] [--measure 300]

Each camera is opened in the config's order with its requested size and rate, streamed for
`discard` seconds while it settles (its first frames are not what it delivers later), then
measured for `measure` seconds: the rate is the frames that arrived, less one, over the span
from the first to the last, with one frame over that span as its uncertainty, and a gap is
an interval of more than two and a half typical ones (a hole of two or more frames). No
other camera streams meanwhile, so the rate is the camera's own and not what a shared USB
link leaves it. The camera program must not be running, and a minute should have passed
since it was stopped: a camera opened within seconds of its release can deliver almost
nothing for a while. The system must list exactly the config's cameras, as for a launch:
camera k is OpenCV's index k only then, so another count stops the calibration before any
camera is opened. The file goes beside the config unless -o says otherwise, and holds an entry for
every camera that delivered frames; a camera that did not is reported and left out, and the
camera program measures it at launch. A camera's entry holds only for the mode it was made
in; recalibrate after changing a camera, its mode, or its port."""

import argparse
import os
import sys
import time
from datetime import datetime

import cv2

from ratrixcam_naming import camera_name, code_version
from ratrixcam_server import FIRST_FRAME_WAIT, measure_camera
from ratrixcam_utils import Calibration, CameraCalibration, Config, calibration_path, count_video_devices, load_settings

DISCARD_SECONDS = 30.0  # the camera streams this long before the measurement starts
MEASURE_SECONDS = 300.0  # the measurement: 9000 frames at 30 fps, a rate good to about a hundredth


def calibrate_camera(config: Config, index: int, discard: float, measure: float) -> CameraCalibration | None:
    """Open the camera at index, let it run for `discard` seconds, measure it for `measure`
    seconds and close it. None, after a line, when it does not open or delivers no frames."""
    camera = config.cameras[index]
    name = camera_name(config.subject_prefix, camera.subjID, camera.view)
    width, height, fps = camera.width or config.default_width, camera.height or config.default_height, camera.fps or config.default_fps
    print(f"Camera {index + 1} of {len(config.cameras)}, {name} at position {camera.position}: requesting {width}x{height} at {fps} fps")
    capture = cv2.VideoCapture(index)
    _ = capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    _ = capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    _ = capture.set(cv2.CAP_PROP_FPS, fps)
    if not capture.isOpened():
        print(f"WARNING: camera {name} did not open at index {index}; left out of the calibration")
        return None
    try:
        print(f"  settling for {discard:.0f} s...", flush=True)
        settled = measure_camera(capture, seconds=discard, first_frame_wait=FIRST_FRAME_WAIT)
        if settled is None:
            print(f"WARNING: camera {name} delivered no frame in {FIRST_FRAME_WAIT:.0f} s; left out of the calibration")
            return None
        print(f"  measuring for {measure:.0f} s...", flush=True)
        started = time.monotonic()
        measured = measure_camera(capture, seconds=measure, first_frame_wait=FIRST_FRAME_WAIT)
        seconds = time.monotonic() - started
    finally:
        capture.release()
    if measured is None or measured.frames < 2:
        print(f"WARNING: camera {name} delivered {measured.frames if measured else 0} frame(s) in the measurement; left out of the calibration")
        return None
    print(
        f"  {name}: {measured.rate:.3f} fps (uncertainty {measured.uncertainty:.3f}) at {measured.width}x{measured.height}, "
        + f"{measured.frames} frames in {seconds:.1f} s, {measured.gaps} gap(s)"
    )
    if measured.gaps:
        print(f"WARNING: {measured.gaps} hole(s) of two or more frames during camera {name}'s measurement; the rate counts the frames that arrived")
    if (measured.width, measured.height) != (width, height):
        print(f"WARNING: camera {name} delivers {measured.width}x{measured.height}, not the {width}x{height} requested")
    return CameraCalibration(
        index=index,
        name=name,
        position=camera.position,
        requested_width=width,
        requested_height=height,
        requested_fps=fps,
        width=measured.width,
        height=measured.height,
        rate=measured.rate,
        uncertainty=measured.uncertainty,
        frames=measured.frames,
        seconds=seconds,
        gaps=measured.gaps,
    )


def calibrate(config: Config, config_file: str, discard: float, measure: float) -> Calibration:
    """Every camera of the config in turn; the Calibration to write."""
    entries = [calibrate_camera(config, index, discard, measure) for index in range(len(config.cameras))]
    return Calibration(
        calibrated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        code_version=code_version(),
        config_file=os.path.basename(config_file),
        cameras=[entry for entry in entries if entry is not None],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="ratrixcam frame-rate calibration, one camera at a time")
    _ = parser.add_argument("-c", "--config", type=str, required=True, help="the camera config file")
    _ = parser.add_argument("-o", "--output", type=str, default=None, help="where to write the calibration; by default beside the config")
    _ = parser.add_argument("--discard", type=float, default=DISCARD_SECONDS, help="seconds each camera streams before its measurement")
    _ = parser.add_argument("--measure", type=float, default=MEASURE_SECONDS, help="seconds each camera is measured for")
    args = vars(parser.parse_args())
    config_file: str = args["config"]  # pyright: ignore[reportAny]  # argparse hands back Any
    output: str = args["output"] or calibration_path(config_file)
    discard: float = args["discard"]  # pyright: ignore[reportAny]  # argparse hands back Any
    measure: float = args["measure"]  # pyright: ignore[reportAny]  # argparse hands back Any

    config = load_settings(config_file)
    if config is None:
        return 1
    n = len(config.cameras)
    counted = count_video_devices(set())
    if counted is None:
        print("ERROR: the cameras could not be counted; nothing is calibrated")
        return 1
    if counted != n:
        print(
            f"ERROR: {counted} camera(s) counted, {n} configured: camera k is index k only when the counts match; "
            + "connect, disconnect or configure cameras until they do, then run again"
        )
        return 1
    print(f"Calibrating {n} camera(s), about {n * (discard + measure + FIRST_FRAME_WAIT) / 60:.0f} min in all; the camera program must not be running")
    print("A minute should have passed since the camera program was stopped: a camera reopened sooner can deliver almost nothing for a while")
    calibration = calibrate(config, config_file, discard, measure)
    try:
        with open(output, "w") as f:
            _ = f.write(calibration.model_dump_json(indent=2) + "\n")
    except OSError as e:
        print(f"ERROR: cannot write the calibration file {output}: {e.strerror or e}")
        return 1
    missing = n - len(calibration.cameras)
    print(f"Calibration of {len(calibration.cameras)} camera(s) written to {output}" + (f"; {missing} camera(s) left out, see the warnings above" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
