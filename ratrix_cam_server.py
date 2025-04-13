# version 250306_1325
import argparse
import os
import shutil
import signal
import sys
import time
from datetime import datetime
from multiprocessing import Process

import cv2
from cv2.typing import MatLike

from ratrix_utils import create_directory
from ratrixcam_IO import Config, load_settings

video_codec = cv2.VideoWriter.fourcc(*"mp4v")


# ----------------------------------------------------------------------
# BEGIN FUNCTION DEFINITIONS
# function to move video file from temporary to permanent location
def move_file(temp_file: str, out_file: str):
    # utility function for moving video file from temporary to permanent location
    # copy the file from the temporary location to the permanent one
    # print('attempting to copy ',file_completed)
    # print('to ',out_file)
    if not os.path.exists(temp_file):
        print("WARNING! ", temp_file, "not found!")
        return

    source_file = temp_file
    destination_file = out_file

    try:
        shutil.copy2(source_file, destination_file)
        # print(f"File '{source_file}' copied successfully to '{destination_file}'.")
    # except shutil.SameFileError:
    #     print("Error: Source and destination files are the same.")
    # except PermissionError:
    #     print("Error: Permission denied. Unable to copy the file.")
    # except FileNotFoundError:
    #     print(f"Error: Source file '{source_file}' not found.")
    # except OSError as e:
    #     print(f"OS Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

    # check if this succeeded
    if os.path.exists(out_file) and os.path.isfile(out_file):
        # print('Saved ', out_file, ' to storage drive')
        os.remove(temp_file)
        if os.path.exists(temp_file):
            print("WARNING: failed to clean up ", temp_file)
        # else: print('Removed ', file_completed)
    else:
        print("WARNING!!! Failed to save ", temp_file, " to permanent storage")


class CamServer:
    def __init__(self, config: Config, device_id: int):
        self.config: Config = config
        self.device_id: int = device_id
        self.filecount: int = 0
        self.file_transfer_processes: list[Process] = []
        self.video_writer: cv2.VideoWriter | None = None
        self.capture: cv2.VideoCapture | None = None
        self.current_file_name: str = ""
        self.current_save_dir: str = ""
        self.temp_dir: str = ""
        # TODO: a string used as part of the video filename
        self.label: str = "_"
        self.cam_num_str: str = str(self.device_id + 1).zfill(2)

    def graceful_shutdown(self):
        print("Multicam attempting to shut down nicely")
        if self.capture is not None:
            self.capture.release()
        # the above line clears the capture buffer, so the the below loop will not do anything
        while self.save_frame_to_writer(datetime.now()) is not None:
            continue
        if self.video_writer is not None:
            self.video_writer.release()
        temp_video_path = os.path.join(self.temp_dir, self.current_file_name)
        out_path = os.path.join(self.current_save_dir, self.current_file_name)

        # spawn a separate process to move the closed tmp file to permanent location
        p = Process(target=move_file, args=(temp_video_path, out_path))
        p.start()
        # keep track of process to clean up later
        self.file_transfer_processes.append(p)
        for process in self.file_transfer_processes:
            process.join()  # this blocks until process is complete

        print(
            f"==== STOPPING CAMERA {str(self.device_id + 1).zfill(2)} at: {datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}",
        )
        print("Saved ", self.filecount, " files during this run")
        print("Shutdown complete.")

    def save_frame_to_writer(self, current_time: datetime) -> MatLike | None:
        if self.capture is None:
            print("hmmm... something is wrong here")
            return

        ret, frame = self.capture.read()
        if not ret:  # same as if frame is None:  ?
            print(
                f"WARNING! failed to capture a frame from camera {self.cam_num_str} at {datetime.now().strftime('%H:%M:%S.%f')}"
            )
            return

        # annotate the video frame
        video_date = current_time.strftime("%y%m%d")  # written on video frame
        video_time_long = current_time.strftime("%H:%M:%S.%f")  # written on video frame

        font = cv2.FONT_HERSHEY_PLAIN
        _ = cv2.putText(
            frame,
            self.label,
            (10, self.config.height - 10),
            font,
            1,
            (255, 255, 255),
            thickness=1,
            lineType=cv2.LINE_AA,
        )
        _ = cv2.putText(
            frame,
            video_date,
            (self.config.width - 115, self.config.height - 25),
            font,
            1,
            (255, 255, 255),
            thickness=1,
            lineType=cv2.LINE_AA,
        )
        _ = cv2.putText(
            frame,
            video_time_long,
            (self.config.width - 115, self.config.height - 10),
            font,
            1,
            (255, 255, 255),
            thickness=1,
            lineType=cv2.LINE_AA,
        )

        # write the just-captured frame to the currently open video writer
        if self.video_writer is not None:
            self.video_writer.write(
                frame
            )  # filename determined by when video file was opened

        return frame

    # Function to stream video to drive in slices
    def device_stream(self):
        # check for, or create, temporary folder for streaming to (internal drive)
        # this does not change with recording date or video time
        # camera numbers 01-08
        temp_dir = os.path.join(self.config.tempStreamPath, self.cam_num_str)
        if not os.path.exists(temp_dir) and not create_directory(temp_dir):
            print("ERROR: Cannot create temporary streaming folder")
            return

        # create full path filename for updating still images (used for GUI display)
        camera_still_path = os.path.join(
            self.config.stillFolder, f"cam_{self.cam_num_str}_status.png"
        )

        # set the path for permanent storage of the current video according to the date
        # note this path will be updated within the loop to reflect date changes

        # try to connect to the camera
        self.capture = cv2.VideoCapture(int(self.device_id))  # hardware address
        _ = self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        _ = self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        _ = self.capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        _ = self.capture.set(
            cv2.CAP_PROP_EXPOSURE, self.config.cam_exposure
        )  # limits exposure duration

        if not self.capture.isOpened():
            print(
                f"Failed to initialize device {self.device_id} camera {self.cam_num_str}: Camera not found",
            )
            return

        # Create video writer for the first video file
        start = time.time()
        count = 0  # tracks frames since last still image update

        # this loop is executed once per video frame until camera is stcaopped
        while self.capture.isOpened():
            current_time = datetime.now()
            if self.video_writer is None:
                # create a new video filename for the temporary streaming location
                # print('creating new video file')
                self.current_file_name = f"cam{self.cam_num_str}_{str(current_time.strftime('%y%m%d_%H-%M-%S'))}{self.config.video_ext}"
                self.current_save_dir = os.path.join(
                    self.config.out_path,
                    f"{self.label}_{current_time.strftime('%y%m%d')}",
                )
                # open the video writer
                # print('opening video writer')
                self.video_writer = cv2.VideoWriter(
                    os.path.join(temp_dir, self.current_file_name),
                    video_codec,
                    self.config.fps,
                    (self.config.width, self.config.height),
                )
                self.filecount += 1
                print(
                    f"Camera {self.cam_num_str} will now stream to {self.current_file_name}"
                )

            # get one frame from the camera and write it to the video writer
            frame = self.save_frame_to_writer(current_time)
            # if video slice duration has been exceeded, close video file and initialize new one
            if time.time() - start > self.config.time_slice:
                start = time.time()  # update the current video start time to now

                # clean up after any previously spawned file transfer processes
                remaining_transfer_processes: list[Process] = []
                for process in self.file_transfer_processes:
                    if not process.is_alive():  # if previous write job is done
                        process.join()  # kill previous write job
                    else:
                        remaining_transfer_processes.append(process)
                        print("Not done moving last video??")
                self.file_transfer_processes = remaining_transfer_processes

                # release current video writer
                self.video_writer.release()
                # reset video_writer to intial state
                self.video_writer = None
                # enforce a delay after the release() command before attempting
                time.sleep(0.1)

                temp_video_path = os.path.join(temp_dir, self.current_file_name)
                out_path = os.path.join(self.current_save_dir, self.current_file_name)

                # spawn a separate process to move the closed tmp file to permanent location
                p = Process(target=move_file, args=(temp_video_path, out_path))
                p.start()
                # keep track of process to clean up later
                self.file_transfer_processes.append(p)

            # once per sec, try to update the still image
            if count % self.config.fps == 0 and frame is not None:
                # print('attempting to overwrite',CameraStillFilename)
                try:
                    _ = cv2.imwrite(camera_still_path, frame)
                    # print('success!')
                except Exception as e:
                    print(type(e), e)
                    # print('unable to write still frame ',CameraStillFilename)

            count += 1  # increment frame count whether that succeeds or fails


def graceful_shutdown(state: CamServer):
    state.graceful_shutdown()
    sys.exit(0)


# ------------------------------------------------------------------------
# Parser description
def main():
    parser = argparse.ArgumentParser(description="Ratrix Camera Setup", add_help=False)
    _ = parser.add_argument("-c", "--config", type=str)
    _ = parser.add_argument("-i", "--index", type=int)
    args = vars(parser.parse_args())

    config = load_settings(args["config"])
    if config is None:
        print("ERROR: Cannot load settings")
        return

    device_id = int(args["index"]) - 1

    cam_server = CamServer(config, device_id)

    _ = signal.signal(signal.SIGINT, lambda sig, frame: graceful_shutdown(cam_server))

    cam_server.device_stream()

    os.kill(os.getpid(), signal.SIGINT)


# END FUNCTION DEFINITIONS
# ----------------------------------------------------------------------

# Starting stream
# print('starting device ',device_id,' camera ',cameraNumber, ' named ',label)
if __name__ == "__main__":
    main()
