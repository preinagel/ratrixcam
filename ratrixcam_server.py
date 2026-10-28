# version 250306_1325
import argparse
import fcntl
import multiprocessing
import os
import shutil
import signal
import time
import math
from datetime import datetime
from multiprocessing import Process
from multiprocessing.synchronize import Event
from types import FrameType
from typing import NamedTuple

import cv2
from cv2.typing import MatLike
from pydantic import BaseModel

from ratrixcam_manifest import ManifestMetadata
from ratrixcam_naming import camera_name, clip_filename, still_path
from ratrixcam_utils import Config, ensure_dir_exists, load_settings, stills_dir

video_codec = cv2.VideoWriter.fourcc(*"mp4v")


# ----------------------------------------------------------------------
# BEGIN FUNCTION DEFINITIONS
# function to move video file from temporary to permanent location
# NOTE TRY MODIFYING THIS FUNCTION TO TEST TRANSCODING INSTEAD OF COPYING?
TRANSFER_PAUSE: float = 3  # seconds before the first attempt; hand tuned to prevent race condition failures (fragile)
TRANSFER_RETRY_DELAY: float = 5  # seconds between attempts
TRANSFER_RETRY_WINDOW: float = 180  # seconds of silent retrying before a clip is left in temp: long enough for a
                                    # planned SSD swap (eject, unplug, plug in, mount) without a frame lost; the
                                    # stop click waits this long on a missing or full drive, so it must stay well
                                    # under the 10-minute shutdown kill timeout
TRANSFER_SPACE_FACTOR = 10  # free space required at the destination, as a multiple of the clip's size;
                            # generous so that the cameras' near-simultaneous transfers all fit or all wait


def move_file(temp_file: str, out_file: str, save_root: str) -> bool:
    """
    Copy a closed clip from its temporary location to its permanent one, verify the copy by
    size, flush it onto the drive, then remove the temporary file. Runs in its own process. Whatever stops a transfer
    (the save folder, created at launch, is gone; the destination cannot be created or is
    short of space; the copy fails) is retried silently every TRANSFER_RETRY_DELAY seconds
    for TRANSFER_RETRY_WINDOW seconds; after that the clip is left in temp with one WARNING
    naming it, and the shutdown cleanup finds such clips by listing temp. Nothing is created
    unless save_root exists, so a vanished mount point never gets a folder of the same name
    on the system disk.
    Returns:
        True if the clip was moved
    """
    xfer_mode="copy" # hardwire until compression fully supported

    time.sleep(TRANSFER_PAUSE)  # pause before first attempt reduces failures
    name = os.path.basename(temp_file)
    if not os.path.exists(temp_file):
        print(f"WARNING: the temp file '{temp_file}' was not found!")
        return False
    try:
        size = os.path.getsize(temp_file)
    except OSError as e:
        print(f"WARNING: {name} left in temp; cannot read it ({e.strerror or e})")
        return False

    deadline = time.monotonic() + TRANSFER_RETRY_WINDOW
    tries = 0
    while True:
        tries += 1
        problem = _transfer_attempt(temp_file, out_file, save_root, size, xfer_mode)
        if problem is None:
            break
        if time.monotonic() >= deadline:
            try:
                if os.path.exists(out_file + PARTIAL_SUFFIX):
                    os.remove(out_file + PARTIAL_SUFFIX)  # no partial copies at the destination
            except OSError:
                pass
            print(f"WARNING: {name} left in temp; gave up after {tries} {'try' if tries == 1 else 'tries'} ({problem})")
            return False
        time.sleep(TRANSFER_RETRY_DELAY)

    try:
        os.remove(temp_file)
    except OSError as e:
        print(f"WARNING: {name} transferred, but its temp copy could not be removed ({e.strerror or e})")
    return True


PARTIAL_SUFFIX = ".part"  # a clip is copied under this suffix and renamed once its size is verified,
                          # so a copy cut short by a vanishing drive never carries a clip's name


def _flush_to_device(path: str) -> None:
    """Push a file's data, or a folder's entries, out of the OS write cache onto the drive, so
    that a drive pulled right afterwards still holds it. F_FULLFSYNC (macOS) also empties the
    drive's own cache; where it is unavailable or rejected, a plain fsync is used."""
    fd = os.open(path, os.O_RDONLY)
    try:
        full_sync = getattr(fcntl, "F_FULLFSYNC", None)
        if full_sync is not None:
            try:
                _ = fcntl.fcntl(fd, full_sync)
                return
            except OSError:
                pass
        os.fsync(fd)
    finally:
        os.close(fd)


def _transfer_attempt(temp_file: str, out_file: str, save_root: str, size: int, xfer_mode: str) -> str | None:
    """One attempt to put the clip at out_file: None on success, otherwise the reason it
    failed (the message alone, no paths)."""
    if not os.path.isdir(save_root):
        return "save folder not found"
    out_dir = os.path.dirname(out_file)
    try:
        if not os.path.isdir(out_dir):
            # one level only, never makedirs: if the save folder vanished since the check
            # above, this fails instead of rebuilding it on the system disk
            os.mkdir(out_dir)
        free = shutil.disk_usage(out_dir).free
    except OSError as e:
        return f"destination unavailable: {e.strerror or e}"
    if free < size * TRANSFER_SPACE_FACTOR:
        return "destination short of space"
    try:
        if xfer_mode == "copy":
            partial = out_file + PARTIAL_SUFFIX
            _ = shutil.copy2(temp_file, partial)
            copied = os.path.getsize(partial)
            if copied != size:
                return f"copied {copied} of {size} bytes"
            _flush_to_device(partial)  # on the drive before it takes the clip's name
            os.replace(partial, out_file)
            try:
                _flush_to_device(out_dir)  # the rename itself; best effort
            except OSError:
                pass
        else:
            # The following works with timeout=3 but only for 640x480 on most cameras;can handle 2 780p cams
            # HEADS-UP before enabling: os.system hands this string to the shell with the two paths
            # unquoted, so a folder name with a space or a shell character breaks or alters the
            # command; use subprocess.run with an argument list instead.
            conversion_failed: int = os.system(command=f"ffmpeg -i {temp_file} -c:v hevc_videotoolbox -q:v 65 -tag:v hvc1 -loglevel error {out_file}")
            if conversion_failed:  # returns 0 for success, other outcomes
                print(f"fmpeg conversion of {temp_file} failed with exit code {conversion_failed}")
            # verify existence of the transcoded output file (its size differs from the original)
            if not (os.path.exists(out_file) and os.path.isfile(out_file)):
                return "no output file"
    except OSError as e:
        return f"copy failed: {e.strerror or e}"
    except Exception as e:
        return f"copy failed: {e}"
    return None


class CameraParams(BaseModel):
    name: str  # constructed camera name: <subject_prefix><subjID>_<view>
    width: int
    height: int
    fps: int
    cam_exposure: float
    crf: int  # ffmpeg quality for this camera's clips, recorded for videoproc


def save_frame_to_writer(
    capture: cv2.VideoCapture,
    writer: cv2.VideoWriter,
    params: CameraParams,
    current_time: datetime,  
    label: str
) -> MatLike | None:

    ret=False
    for _ in range(3):
        ret, frame = capture.read()
        if ret:
            break 

    if not ret:
        print(
            f"Cam_server: Failed to capture a frame from camera {params.name} at {datetime.now().strftime('%H:%M:%S.%f')}"
        )
        return  
    

    # time stamp to overlay on video frame
    video_date = current_time.strftime("%Y%m%d")  
    video_time_long = current_time.strftime("%H:%M:%S.%f")[:-4] # Truncate to .01s to reflect actual accuracy of timestamps

    # NOTE text position is hardwired for 640x480 videos, needs generalization
    font = cv2.FONT_HERSHEY_PLAIN
    font_scale = params.height/480 #params.width / 640
    # type checker: "frame is possibly unbound" here and at the four uses below, because it is
    # first assigned inside the read loop. `if not ret or frame is None: return` after the loop
    # would clear it and is behaviourally inert; left as written.
    _ = cv2.putText(
        frame,  # checker: possibly unbound, see note above
        label,
        (math.ceil(0.015*params.width), math.floor(0.97*params.height )),  #(10, params.height - 10),
        font,
        font_scale,
        (255, 255, 255),
        thickness=1,
        lineType=cv2.LINE_AA,
    )
    _ = cv2.putText(
        frame,  # checker: possibly unbound, see note above
        video_date,
        (math.floor(0.8*params.width), math.floor(0.94*params.height) ), #(params.width - 115, params.height - 25),
        font,
        font_scale,
        (255, 255, 255),
        thickness=1,
        lineType=cv2.LINE_AA,
    )
    _ = cv2.putText(
        frame,  # checker: possibly unbound, see note above
        video_time_long,
        (math.floor(0.8*params.width), math.floor(0.97*params.height) ),#(params.width - 115, params.height - 10),
        font,
        font_scale,
        (255, 255, 255),
        thickness=1,
        lineType=cv2.LINE_AA,
    )

    writer.write(frame)  # checker: possibly unbound, see note above

    return frame  # checker: possibly unbound, see note above

class WriterState(NamedTuple):
    writer: cv2.VideoWriter
    save_dir: str
    temp_dir: str
    file_name: str
    started: datetime  # time of the first frame written to this clip


def close_writer(writer_state: WriterState, file_transfer_processes: list[Process], manifest: ManifestMetadata, save_root: str):
    writer_state.writer.release()
    manifest.append(os.path.basename(writer_state.save_dir), writer_state.file_name, writer_state.started)

    temp_video_path = os.path.join(writer_state.temp_dir, writer_state.file_name)
    out_path = os.path.join(writer_state.save_dir, writer_state.file_name)

    # spawn a separate process to move the closed tmp file to permanent location
    print(f"Cam_server: Starting transfer of file:{writer_state.file_name}") # {temp_video_path} to {out_path}")
    p = Process(target=move_file, args=(temp_video_path, out_path, save_root))
    p.start()
    # keep track of process to clean up later
    file_transfer_processes.append(p)


def run(config: Config, device_id: int, stop_event: Event):
    camera = config.cameras[device_id]
    params = CameraParams(
        name=camera_name(config.subject_prefix, camera.subjID, camera.view),
        width=(camera.width or config.default_width),
        height=(camera.height or config.default_height),
        fps=(camera.fps or config.default_fps),
        cam_exposure=(camera.exposure or config.default_cam_exposure),
        crf=(camera.crf if camera.crf is not None else config.default_crf),
    )
    label: str = f"{config.study_label}_{params.name}"
    manifest = ManifestMetadata(config.temp_path, params.name, camera.subjID, camera.view, params.crf)
    #ifi: float=1/params.fps #nominal interframe interval

    temp_dir = os.path.join(config.temp_path, label)
    if not ensure_dir_exists(temp_dir):
        print("ERROR: Temporary streaming folder does not exist and cannot be created")
        return

    stills_folder = stills_dir(config.temp_path)
    if not ensure_dir_exists(stills_folder):
        print("ERROR: Still image folder does not existand cannot be created")
        return

    # create full path filename for updating still images (used for GUI display)
    camera_still_path = still_path(stills_folder, params.name)

    # try to connect to the camera
    capture = cv2.VideoCapture(int(device_id))  # hardware address
    start = time.time() # indicates time this videocapture was opened

    _ = capture.set(cv2.CAP_PROP_FRAME_WIDTH, params.width)
    _ = capture.set(cv2.CAP_PROP_FRAME_HEIGHT, params.height)
    _ = capture.set(cv2.CAP_PROP_FPS, params.fps)
    _ = capture.set(cv2.CAP_PROP_EXPOSURE, params.cam_exposure)

    if not capture.isOpened():
        print(f"Cam_server: Camera {params.name} Failed to open recording device {device_id}")
        return

    count:int = 0  # tracks frames since last still image update
    filecount: int = 0
    file_transfer_processes: list[Process] = []
    writer_state: WriterState | None = None

    # this loop is executed once per video frame until camera is stcaopped
    while capture.isOpened() and not stop_event.is_set():
        current_time: float = time.time()
        current_datetime: datetime = datetime.fromtimestamp(timestamp=current_time)

        # if not started yet, open first video file
        # or if video slice duration has been exceeded, close video file and initialize new one
        if writer_state is None or current_time - start > config.time_slice:
            #timer = time.time_ns() 
            # check for unfinished file transfers
            # file_transfer_processes = [
            #     p for p in file_transfer_processes if p.is_alive()
            # ]
            # if len(file_transfer_processes) > 0:
            #     print(
            #         f"WARNING: {len(file_transfer_processes)} previous file transfer processes are still running"
            #     )
            start = current_time # update start time for first frame of new video (time.time() format)
                                        # note that the next frame captured will be written to the new writer

            # close the old writer
            if writer_state is not None:
                close_writer(writer_state, file_transfer_processes, manifest, config.save_path)
            filecount += 1

            # open a new writer; the clip's destination day folder is created by move_file
            # when the clip is transferred, so this loop never touches the save drive
            current_save_dir = os.path.join(
                config.save_path, f"{label}_{current_datetime.strftime('%Y%m%d')}"
            )
            current_file_name = clip_filename(params.name, current_datetime)
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
                current_datetime,
            )
            print(f"Cam_server: Camera {params.name} will now stream to {current_file_name}")

            #elapsed = time.time_ns() - timer
            #print("Took", elapsed / 1e6, "ms to open new video writer")

        # Testing whether we could compute time from frame count? Result: no, fps is not exactly the nominal fps
        # frametime: float=start+count*ifi  #nominal time since video started
        # frame_datetime: datetime=datetime.fromtimestamp(timestamp=frametime)
        # frame_timestr: str=frame_datetime.strftime("%H:%M:%S.%f")[:-3] 
        # print(label,'frame',count,'capture time',current_datetime.strftime("%H:%M:%S.%f")[:-3],'nominal time',frame_timestr)

        # proceed to capture the next frame in the buffer
        # NOTE maybe should try 2-3x before giving up?  
        full_label: str = label + ' frame ' + str(count) #include frame# in overlay text
        frame = save_frame_to_writer(
            capture, writer_state.writer, params, current_datetime, label=full_label
        )
        if frame is None:
            break #if you fail to capture, interpret that camera is down, fall out of loop
        
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

    # reach this line whenever camera fails to capture a frame (camera presumed offline)
    print(f"Camera server {device_id+1} attempting to shut down nicely")
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
        close_writer(writer_state, file_transfer_processes, manifest, config.save_path)

    file_transfer_processes = [p for p in file_transfer_processes if p.is_alive()]
    print(
        f"Cam_server Waiting for {len(file_transfer_processes)} file transfer processes to finish"
    )
    for process in file_transfer_processes:
        process.join()  # this blocks until process is complete

    # print(
    #     f"==== STOPPING CAMERA {str(device_id + 1).zfill(2)} at: {datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}",
    # )
    
    print(f"ratrixcam server {device_id+1}: shutdown complete; saved {filecount} files during this run")


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

    parser = argparse.ArgumentParser(description="ratrixcam camera server", add_help=False)
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
