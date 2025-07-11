# version 250306_1325
import argparse
import multiprocessing
import os
import shutil
import signal
import time
from datetime import datetime
from multiprocessing import Process
from multiprocessing.synchronize import Event
from types import FrameType
from typing import NamedTuple

import cv2
from cv2.typing import MatLike
from pydantic import BaseModel

from ratrix_utils import Config, ensure_dir_exists, load_settings, still_path

video_codec = cv2.VideoWriter.fourcc(*"mp4v")


# ----------------------------------------------------------------------
# BEGIN FUNCTION DEFINITIONS
# function to move video file from temporary to permanent location
def move_file(temp_file: str, out_file: str):
    """
    Utility function for moving video file from temporary to permanent location copy
    the file from the temporary location to the permanent one

    Args:
        temp_file:
        out_file:
    """
    if not os.path.exists(temp_file):
        print(f"WARNING: the temp file '{temp_file}' was not found!")
        return

    while True:
        try:
            _ = shutil.copy2(temp_file, out_file)
            if os.path.exists(out_file) and os.path.isfile(out_file):
                break
        except Exception as e:
            print(
                f"WARNING: Failed to copy {temp_file} to {out_file}, retrying in 1 second",
                e,
            )
        time.sleep(1)

    os.remove(temp_file)
    if os.path.exists(temp_file):
        print("WARNING: failed to clean up ", temp_file)


class CameraParams(BaseModel):
    name: str
    row: int
    width: int
    height: int
    fps: int
    cam_exposure: float


def save_frame_to_writer(
    capture: cv2.VideoCapture,
    writer: cv2.VideoWriter,
    params: CameraParams,
    current_time: datetime, #should eliminate? less accurate than capturing here
    label: str,
) -> MatLike | None:

    # 250708PR: debugging why camera images are cropped compared to view in other apps
    # Adjust the webcam resolution using cap.set() before reading frames:
    # okw=capture.set(cv2.CAP_PROP_FRAME_WIDTH, params.width)
    # okh=capture.set(cv2.CAP_PROP_FRAME_HEIGHT, params.height) 
    # if not (okw and okh):
    #     print('Failed to set frame dimensions!')
    
    frame_grab_time = datetime.now() # check time immediately before frame grab attempted
    ret, frame = capture.read()
    if not ret:  # same as if frame is None:  ?
        print(
            f"WARNING! failed to capture a frame from camera {params.name} at {datetime.now().strftime('%H:%M:%S.%f')}"
        )
        return
    # else: #for debugging purposes verify the dimensions of the image
    #     tmph, tmpw, jnk = frame.shape
    #     print(f"Frame dimensions: Width={tmpw}, Height={tmph}")
    
    # time stamp to overlay on video frame
    video_date = frame_grab_time.strftime("%Y%m%d")  
    video_time_long = frame_grab_time.strftime("%H:%M:%S.%f")[:-3] # Truncate to milliseconds?

    # NOTE text position is hardwired for 640x480 videos, needs generalization
    font = cv2.FONT_HERSHEY_PLAIN
    font_scale = params.width / 640
    _ = cv2.putText(
        frame,
        label,
        (10, params.height - 10),
        font,
        font_scale,
        (255, 255, 255),
        thickness=1,
        lineType=cv2.LINE_AA,
    )
    _ = cv2.putText(
        frame,
        video_date,
        (params.width - 115, params.height - 25),
        font,
        font_scale,
        (255, 255, 255),
        thickness=1,
        lineType=cv2.LINE_AA,
    )
    _ = cv2.putText(
        frame,
        video_time_long,
        (params.width - 115, params.height - 10),
        font,
        font_scale,
        (255, 255, 255),
        thickness=1,
        lineType=cv2.LINE_AA,
    )

    writer.write(frame)

    return frame

class WriterState(NamedTuple):
    writer: cv2.VideoWriter
    save_dir: str
    temp_dir: str
    file_name: str


def close_writer(writer_state: WriterState, file_transfer_processes: list[Process]):
    writer_state.writer.release()

    temp_video_path = os.path.join(writer_state.temp_dir, writer_state.file_name)
    out_path = os.path.join(writer_state.save_dir, writer_state.file_name)

    # spawn a separate process to move the closed tmp file to permanent location
    print(f"Starting transfer of file:{writer_state.file_name}") # {temp_video_path} to {out_path}")
    p = Process(target=move_file, args=(temp_video_path, out_path))
    p.start()
    # keep track of process to clean up later
    file_transfer_processes.append(p)


def run(config: Config, device_id: int, stop_event: Event):
    params = CameraParams(
        name=config.cameras[device_id].name,
        row=config.cameras[device_id].row,
        width=(config.cameras[device_id].width or config.default_width),
        height=(config.cameras[device_id].height or config.default_height),
        fps=(config.cameras[device_id].fps or config.default_fps),
        cam_exposure=(
            config.cameras[device_id].exposure or config.default_cam_exposure
        ),
    )
    label = f"{config.study_label}_{params.name}"

    temp_dir = os.path.join(config.temp_path, label)
    if not ensure_dir_exists(temp_dir):
        print("ERROR: Temporary streaming folder does not exist and cannot be created")
        return

    if not ensure_dir_exists(config.stills_path):
        print("ERROR: Still image folder does not existand cannot be created")
        return

    # create full path filename for updating still images (used for GUI display)
    camera_still_path = still_path(config.stills_path, params.name)

    # try to connect to the camera
    capture = cv2.VideoCapture(int(device_id))  # hardware address
    _ = capture.set(cv2.CAP_PROP_FRAME_WIDTH, params.width)
    _ = capture.set(cv2.CAP_PROP_FRAME_HEIGHT, params.height)
    _ = capture.set(cv2.CAP_PROP_FPS, params.fps)
    _ = capture.set(cv2.CAP_PROP_EXPOSURE, params.cam_exposure)

    if not capture.isOpened():
        print(f"Camera {params.name} Failed to open recording device {device_id}")
        return

    count = 0  # tracks frames since last still image update
    filecount: int = 0
    file_transfer_processes: list[Process] = []
    writer_state: WriterState | None = None

    # this loop is executed once per video frame until camera is stcaopped
    start = time.time()
    while capture.isOpened() and not stop_event.is_set():
        current_time = time.time()
        current_datetime = datetime.fromtimestamp(current_time)
        # if video slice duration has been exceeded, close video file and initialize new one
        if writer_state is None or current_time - start > config.time_slice:
            #timer = time.time_ns()
            file_transfer_processes = [
                p for p in file_transfer_processes if p.is_alive()
            ]
            if len(file_transfer_processes) > 0:
                print(
                    f"WARNING: {len(file_transfer_processes)} previous file transfer processes are still running"
                )

            if writer_state is not None:
                close_writer(writer_state, file_transfer_processes)

            current_save_dir = os.path.join(
                config.save_path, f"{label}_{current_datetime.strftime('%Y%m%d')}"
            )
            if not ensure_dir_exists(current_save_dir):
                print(f"WARNING! Unable to create output path '{current_save_dir}'")
                continue

            current_file_name = f"{params.name}_{str(current_datetime.strftime('%Y%m%d_%H-%M-%S'))}{config.video_ext}"
            # Create video writer
            writer_state = WriterState(
                cv2.VideoWriter(
                    os.path.join(temp_dir, current_file_name),
                    video_codec,
                    params.fps,
                    (params.width, params.height),
                ),
                current_save_dir,
                temp_dir,
                current_file_name,
            )
            start = current_time #update start time for new video
            filecount += 1
            print(f"Camera {params.name} will now stream to {current_file_name}")
            #elapsed = time.time_ns() - timer
            #print("Took", elapsed / 1e6, "ms to open new video writer")

        # get one frame from the camera and write it to the video writer
        frame = save_frame_to_writer(
            capture, writer_state.writer, params, current_datetime, label
        )
        if frame is None:
            break
        # once per N sec, try to update the still image
        if count % (config.preview_interval * params.fps) == 0:
            # print('attempting to overwrite',camera_still_path)
            try:
                result = cv2.imwrite(camera_still_path, frame)
                if result is False:
                    print(
                        f"WARNING: Failed to write still image to {camera_still_path}"
                    )
                # print('image saved as',camera_still_path)
            except Exception as e:
                print(type(e), e)
        count += 1  # increment frame count whether that succeeds or fails

    print(f"Camera server {device_id} attempting to shut down nicely")
    capture.release()
    if writer_state is not None:
        # flush remaining frames
        while (
            save_frame_to_writer(
                capture, writer_state.writer, params, datetime.now(), label
            )
            is not None
        ):
            pass
        close_writer(writer_state, file_transfer_processes)

    file_transfer_processes = [p for p in file_transfer_processes if p.is_alive()]
    print(
        f"Waiting for {len(file_transfer_processes)} file transfer processes to finish"
    )
    for process in file_transfer_processes:
        process.join()  # this blocks until process is complete

    # print(
    #     f"==== STOPPING CAMERA {str(device_id + 1).zfill(2)} at: {datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}",
    # )
    print("Saved ", filecount, " files during this run")
    print(f"Ratrix Cam Server {device_id}: Shutdown complete")


def main():
    stop_event = multiprocessing.Event()

    def int_handler(_sig: int, _frame: FrameType | None):
        print(
            "Received signal to terminate, shutting down gracefully.\nTo force exit, press Ctrl+C again."
        )
        stop_event.set()
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(signal.SIGTERM, lambda sig, frame: stop_event.set())

    parser = argparse.ArgumentParser(description="Ratrix Camera Setup", add_help=False)
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    _ = parser.add_argument("-i", "--index", type=int, required=True)
    args = vars(parser.parse_args())

    config = load_settings(args["config"])
    if config is None:
        print("ERROR: Cannot load settings")
        return

    device_id = int(args["index"]) - 1
    if device_id < 0:
        print("ERROR: Invalid device index. Must be a positive integer.")
        return

    run(config, device_id, stop_event)


if __name__ == "__main__":
    main()
