#!/usr/bin/env python3 -u

import argparse
import csv
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from pprint import pprint

import cv2
import detect_motion
import numpy as np


class Logger:
    """Logging to CSV file"""

    LOG_FIELDS = [
        "input_path",
        "output_path",
        "start_time",
        "motion_perc",
        "found_motion",
        "motion_detection_time",
        "fract_frames_exceeding",
        "compression_ratio",
        "compression_success",
        "compression_time",
        "valid_output",
        "skipped_reason",
        "error",
    ]

    def __init__(self, log_path: Path):
        """Initialize logger"""
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.reset()

        with open(self.log_path, mode="w", newline="") as log_file:  # write mode
            csv.writer(log_file).writerow(self.LOG_FIELDS)

    def reset(self) -> None:
        """Reset all log fields to None"""
        for field in self.LOG_FIELDS:
            setattr(self, field, None)

    def append(self) -> None:
        """
        Write current log fields to CSV
        """
        values = [getattr(self, field) for field in self.LOG_FIELDS]
        row = ["" if v is None else str(v) for v in values]

        with open(self.log_path, mode="a", newline="") as log_file:  # append mode
            csv.writer(log_file).writerow(row)


class SkipFile(Exception):
    pass


def default_resident(station_ID: str) -> str:
    # in our study rats are stably assigned to stations, so we can map from one to the other
    match station_ID:
        case "stn09":
            rat_ID = "rat556"
        case "stn10":
            rat_ID = "rat557"
        case "stn11":
            rat_ID = "rat558"
        case "stn12":
            rat_ID = "rat559"
        case "stn13":
            rat_ID = "rat560"
        case "stn14":
            rat_ID = "rat561"
        case "stn15":
            rat_ID = "rat562"
        case "stn16":
            rat_ID = "rat563"
        case _:
            rat_ID = "unknown_subject"

    return rat_ID


def parse_filenames(video_fname: Path) -> tuple[str, str, str, str]:
    parse_fname: list[str] = video_fname.stem.split(sep="_")

    if len(parse_fname) == 4:
        # the video filename architecture we plan to use going forward is
        # formatted like: rat558_buddy_20250722_09-41-55.mp4
        rat_ID, camera_view, filming_date, filming_time = parse_fname
    elif len(parse_fname) == 5:
        # the legacy filenames were like: 04_stn09_buddy_20250627_15-34-12.mp4
        _, station_ID, camera_view, filming_date, filming_time = parse_fname
        rat_ID: str = default_resident(station_ID)
    else:
        rat_ID = "unknown_subj"
        camera_view = "unknown_view"
        filming_date = "unknown_date"
        filming_time = "unknown_time"

    return rat_ID, camera_view, filming_date, filming_time


def parse_volume(input_paths: list[Path]) -> tuple[str, str]:
    """
    Parse input paths to extract unique rat IDs and session dates.

    Returns strings of (1) all sorted ratIDs separated by '_' and (2) first and last sessions
    separated by '-'. If only one session is found, it will store the first and last as the same date.
    """

    rat_IDs, sess_IDs = set(), set()  # stores unique IDs
    for path in input_paths:
        rat_ID, _, filming_date, _ = parse_filenames(path)
        rat_IDs.add(rat_ID)
        sess_IDs.add(filming_date)
    rat_IDs, sess_IDs = sorted(rat_IDs), sorted(sess_IDs)

    return "_".join(rat_IDs), f"{sess_IDs[0]}-{sess_IDs[-1]}"


def get_codec_nframes(path: Path):
    """
    Get video codec and total number of frames in video.
    Returns (None, None) if file cannot be opened.
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None, None
    codec = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((codec >> 8 * i) & 0xFF) for i in range(4)])
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return codec, n_frames


def copy_file(in_file: Path, out_file: Path, max_retries: int = 5) -> None:
    """
    Copy a file, retrying up to max_retries times if it fails.
    Raises an exception if all attempts fail.
    """
    timeout: float = 0.1  # seconds
    out_file.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            shutil.copy2(in_file, out_file)
            if out_file.exists() and out_file.is_file():
                return
        except Exception as e:
            print(
                f"WARNING: Failed to copy {in_file} to {out_file} (attempt {attempt}/{max_retries}), retrying in {timeout} seconds: {e}"
            )
            time.sleep(timeout)
    raise RuntimeError(f"Failed to copy {in_file} to {out_file} after {max_retries} attempts")


def detect_motion(input_path: Path, motion_percentile: float, motion_threshold: float):
    """Perform motion detection and return results"""
    try:
        start = time.time()
        motion_by_frame = detect_motion.main(input_path, play_video=False)
        motion_perc = np.percentile(motion_by_frame, motion_percentile)
        found_motion = motion_perc >= motion_threshold
        detection_time = time.time() - start
        fract_frames_exceeding = np.mean(motion_by_frame > motion_threshold)

        print("     ", fract_frames_exceeding, "of frames exceeded motion threshold")
        return motion_perc, found_motion, detection_time, fract_frames_exceeding

    except IndexError:
        print(f"     WARNING: {input_path.resolve()} has not enough frames for motion detection")
        return None, True, None, None


def compress_video(
    input_path: Path,
    output_path: Path,
    motion_detected: bool,
    view: str,
    threads: int,
    taskcam_crf: int,
    compress_spd: str,
):
    """Compress video using ffmpeg and return success status, error message, and compression time"""
    start = time.time()

    base_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),  # converts into platform-dependent path format
        "-c:v",
        "libx264",
        "-preset",
        compress_spd,
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(threads),
    ]

    if motion_detected and view in ["lid", "face"]:  # lossless compression (motion)
        print("    motion, task view: minimally lossy compression will be used")
        command = base_command + ["-crf", str(taskcam_crf), str(output_path)]
    elif motion_detected and view in ["buddy", "home"]:  # lossy compression (motion)
        print("    motion, cage view: more lossy compression will be used")
        command = base_command + ["-crf", "30", str(output_path)]
    else:  # high compression (no motion)
        print("    no motion, highly lossy compression will be used")
        command = base_command + ["-crf", "40", "-g", "1800", str(output_path)]

    # run ffmpeg compression
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        compression_time = time.time() - start
        return True, None, compression_time
    except subprocess.CalledProcessError as e:
        compression_time = time.time() - start
        return False, e.stderr.decode("utf-8"), compression_time


def main(
    input: Path,
    output: Path,
    pattern: str,
    motion_percentile: float,
    motion_threshold: float,
    n_threads: int,
    taskcam_crf: int,
    compress_spd: str,
    recompress: bool,
):
    """motion detection -> compression."""

    kwargs = locals()

    # make sure ffmpeg exists as a shell command
    ffmpeg_cmd = shutil.which("ffmpeg")
    if ffmpeg_cmd is None:
        print("Cannot find ffmpeg!")
        return
    else:
        print(f"ffmpeg found at {ffmpeg_cmd}")

    input_paths: list[Path] = sorted(input.glob(pattern))
    if not input_paths:  # check if input_paths is empty
        print(f"no video files found in {input.resolve()} matching pattern '{pattern}'. Exiting.")
        return
    print("found", len(input_paths), "video files in", input.resolve())

    # initialize configuration and logging files
    rat_IDs, sess_IDs = parse_volume(input_paths)

    config_path = (
        output
        / "auxiliary-data"
        / f"{rat_IDs}_sess-{sess_IDs}"
        / "compression-config"
        / f"{datetime.now():%Y%m%d_%H-%M-%S}.csv"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(kwargs, indent=4, default=str))

    log_path = (
        output
        / "auxiliary-data"
        / f"{rat_IDs}_sess-{sess_IDs}"
        / "compression-logs"
        / f"{datetime.now():%Y%m%d_%H-%M-%S}.csv"
    )

    logger = Logger(log_path)

    for raw_input_path in input_paths:
        input_path = Path(raw_input_path)
        logger.reset()
        logger.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.input_path = input_path
        print(f"processing {input_path.name} at {logger.start_time}.")

        try:

            # (1) parse input path
            rat_ID, view, recording_date, recording_time = parse_filenames(input_path)

            # (2) create output path
            # output_path = output / input_path.name[:6] / f"LS_{rat}_{view}_{date}" / input_path.name
            new_filename: str = f"{rat_ID}_{view}_{recording_date}_{recording_time}.mp4"
            output_path = output / rat_ID / f"LS_{rat_ID}_{view}_{recording_date}" / new_filename
            logger.output_path = output_path

            # (3) get input + output conditions
            input_codec, input_n_frames = get_codec_nframes(path=input_path)
            input_is_valid = input_codec is not None
            input_is_cam_codec = input_codec == "FMP4"
            input_recompress = recompress

            output_exists = output_path.is_file()
            output_codec, output_n_frames = get_codec_nframes(output_path) if output_exists else (None, None)
            output_is_valid = output_codec is not None and output_n_frames == input_n_frames

            # (4) decision tree: skip/copy vs compress

            # 4A what if output file exists?
            if output_exists:
                if output_is_valid:  # if output is valid, refuse to overwrite it
                    raise SkipFile("valid output exists, not overwriting")
                else:  # even if it's invalid, if input also invalid, don't overwrite it
                    if not input_is_valid:
                        raise SkipFile("invalid output exists, input also invalid, not overwriting")
                    # otherwise, if input is valid, treat the invalid output as if it does not exist

            # if we reach here we are no longer concerned about existing outputs (if they exist, overwrite)

            # 4B what if input file is invalid? just copy it over
            if not input_is_valid:
                copy_file(input_path, output_path)
                raise SkipFile("invalid input copied to output")

            # if we reach here the input is valid

            # 4C what if the input file was already previously compressed?
            if not input_is_cam_codec:
                if not input_recompress:  # if we aren't in recompress mode, just copy it
                    copy_file(input_path, output_path)
                    raise SkipFile("compressed input copied to output")
                # otherwise, treat exactly as if it were not previously compressed

            # If we reach here, the video file should be compressed and transferred

            # (5) make output directory
            print(f"    will attempt to save as {output_path.name}.")
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # (6) motion detection (whether the input was previously compressed or not)
            motion_perc, found_motion, detection_time, fract_frames_exceeding = detect_motion(
                input_path, motion_percentile, motion_threshold
            )
            logger.motion_detection_time = detection_time
            logger.motion_perc = motion_perc
            logger.found_motion = found_motion
            logger.fract_frames_exceeding = fract_frames_exceeding

            # (7) video compression using parameters determined by motion detection and view
            success, err_msg, compression_time = compress_video(
                input_path, output_path, found_motion, view, n_threads, taskcam_crf, compress_spd
            )
            logger.compression_time = compression_time
            logger.compression_success = success

            if err_msg:
                logger.error = err_msg

            # (8) check output exists, has non-zero size, and frame count matches
            output_codec, output_n_frames = get_codec_nframes(output_path)
            if (not output_path.exists()) or (output_path.stat().st_size == 0) or (input_n_frames != output_n_frames):
                raise SkipFile("compressed output invalid")

            logger.compression_ratio = input_path.stat().st_size / output_path.stat().st_size
            logger.valid_output = input_n_frames == output_n_frames

            if input_n_frames != output_n_frames:
                raise SkipFile("compressed output invalid")

        # log skips and exceptions
        except SkipFile as s:
            logger.skipped_reason = str(s)
            print(f"    SKIPPED {input_path.resolve()}: {s}")
        except Exception as e:
            logger.error = str(e)
            print(f"    ERROR {input_path.resolve()}: {e}")

        # append a log row for this file (success, skip, or error)
        finally:
            try:
                logger.append()
            except Exception as e:
                print(f"CRITICAL: Failed to write log row for {input_path.resolve()}: {e}")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="compress and transfer videos", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # positional arguments: required
    parser.add_argument("input", type=Path, help="source path")
    parser.add_argument("output", type=Path, help="destination path")

    # keyword arguments: optional
    parser.add_argument("--pattern", default="**/LS*/*.mp4", type=str, help="pattern to match")
    parser.add_argument("--motion_percentile", default=99.9, type=float, help="percentile of frame-to-frame motion")
    parser.add_argument("--motion_threshold", default=0.001, type=float, help="motion detection threshold")
    parser.add_argument(
        "--n_threads",
        default=4,
        type=int,
        help="number of threads used by ffmpeg {4 for mac05, 5 for mac06, 5 for mac07}",
    )
    parser.add_argument(
        "--taskcam_crf", default=25, type=int, help="compression quality {24 for visually lossless, ..., 30 for lossy}"
    )
    parser.add_argument(
        "--compress_spd",
        default="veryfast",
        type=str,
        help="compression speed {ultrafast, superfast, veryfast, ..., veryslow}",
    )
    parser.add_argument("--recompress", action="store_true", help="force compression if input is already compressed")
    kwargs = vars(parser.parse_args())

    # argument validation
    if not kwargs["input"].is_dir():
        raise NotADirectoryError(f"{kwargs['input']} is not a valid directory")

    if not kwargs["output"].is_dir():
        raise NotADirectoryError(f"{kwargs['output']} is not a valid directory")

    if kwargs["input"].resolve() == kwargs["output"].resolve():
        raise ValueError("input and output paths cannot be the same")

    # confirm configuration with user
    print("\nconfiguration:")
    pprint(kwargs, sort_dicts=False)
    input("\n[enter] to continue: ")

    main(**kwargs)
