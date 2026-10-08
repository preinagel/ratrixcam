#!/usr/bin/env python3 -u

import argparse
import concurrent.futures
import contextlib
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from pprint import pprint

import detect_motion
import numpy as np

from classify import (
    ACTION_COPY,
    MAX_LISTED_PATTERN_MISMATCHES,
    STATUS_COMPRESSED,
    STATUS_COPIED,
    STATUS_ERROR,
    STATUS_NOT_ATTEMPTED,
    STATUS_OUTPUT_INVALID,
    STATUS_PATTERN_MISMATCH,
    STATUS_UNPARSEABLE,
    Logger,
    SkipFile,
    classify_file,
    find_unmatched_files,
    log_unmatched_files,
)
from util import (
    RAW_CODEC,
    build_output_path,
    code_version,
    get_codec_nframes,
    parse_filenames,
    parse_recording_datetime,
)

# connectivity-loss handling: if the input/output root becomes unreachable (e.g. a
# network-mounted volume drops), retry fast at first, then patiently, before giving up --
# rather than spinning through the rest of the file list failing on every single file
CONNECTIVITY_FAST_RETRY_SECONDS = 10
CONNECTIVITY_FAST_RETRY_COUNT = 6  # 1 minute total
CONNECTIVITY_SLOW_RETRY_SECONDS = 600  # 10 minutes
CONNECTIVITY_TOTAL_TIMEOUT_HOURS = 10

# TODO(generalize): camera views and their compression policy belong in the shared config,
# not here. Until then, only these names select a policy; anything else is treated as a
# probable misnaming and compressed with the conservative task-view setting
TASK_VIEWS = ("lid", "face")
CAGE_VIEWS = ("buddy", "home")

# ffmpeg encoding policy. The task-view CRF is the --taskcam_crf argument; the rest is fixed
VIDEO_CODEC = "libx264"
PIX_FMT = "yuv420p"
CRF_CAGE_MOTION = 30
CRF_NO_MOTION = 40
GOP_NO_MOTION = 1800


def encoding_policy(taskcam_crf: int, compress_spd: str) -> dict:
    """The complete set of encoding decisions in force, for the run's config.json."""
    return {
        "codec": VIDEO_CODEC,
        "pix_fmt": PIX_FMT,
        "preset": compress_spd,
        "task_views": list(TASK_VIEWS),
        "cage_views": list(CAGE_VIEWS),
        "crf_task_view_motion": taskcam_crf,
        "crf_cage_view_motion": CRF_CAGE_MOTION,
        "crf_unrecognized_view_motion": taskcam_crf,
        "crf_no_motion": CRF_NO_MOTION,
        "gop_no_motion": GOP_NO_MOTION,
        "crf_when_motion_detection_off": taskcam_crf,
    }


def build_manifest(input_paths: list[Path]) -> list[dict]:
    """
    Scan input paths once and summarize, per (subjID, view) stream found, the date/time
    range covered and how many files were seen. Records what a compression run's input
    scope was, independent of whatever folder-naming convention the source data uses.
    Unparseable files are counted separately and excluded from date-range stats.
    """
    streams: dict[tuple[str, str], dict] = {}
    unparseable_count = 0

    for path in input_paths:
        parsed = parse_filenames(path)
        if parsed is None:
            unparseable_count += 1
            continue
        subj_ID, view, filming_date, filming_time = parsed
        recorded_at = parse_recording_datetime(filming_date, filming_time)

        stream = streams.setdefault(
            (subj_ID, view), {"subj_ID": subj_ID, "view": view, "start": None, "end": None, "file_count": 0}
        )
        stream["file_count"] += 1
        if recorded_at is not None:
            if stream["start"] is None or recorded_at < stream["start"]:
                stream["start"] = recorded_at
            if stream["end"] is None or recorded_at > stream["end"]:
                stream["end"] = recorded_at

    manifest = sorted(streams.values(), key=lambda s: (s["subj_ID"], s["view"]))
    for stream in manifest:
        stream["start"] = stream["start"].strftime("%Y-%m-%d %H:%M:%S") if stream["start"] else ""
        stream["end"] = stream["end"].strftime("%Y-%m-%d %H:%M:%S") if stream["end"] else ""
    if unparseable_count:
        print(f"WARNING: {unparseable_count} input file(s) could not be parsed; excluded from manifest")

    return manifest


def write_manifest(manifest_path: Path, manifest: list[dict]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["subj_ID", "view", "start", "end", "file_count"])
        writer.writeheader()
        writer.writerows(manifest)


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout: float = 30.0, poll_interval: float = 0.05):
    """Cross-platform mutual-exclusion lock using atomic exclusive file creation
    (os.O_CREAT | os.O_EXCL is atomic on both NTFS and POSIX filesystems, unlike
    fcntl.flock which is POSIX-only and wouldn't work on Windows)."""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"could not acquire lock {lock_path} within {timeout}s")
            time.sleep(poll_interval)
    try:
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def append_index_row(index_path: Path, run_id: str, manifest: list[dict]) -> None:
    """Append one summary line for this run to the top-level run index (creating it with a
    header on first use), so later lookups don't require opening every run's own folder.
    Locked so two concurrent compress_drive.py runs sharing the same output root (a real
    usage pattern here -- one machine running two batches at once) can't interleave writes
    or duplicate the header row."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    starts = [s["start"] for s in manifest if s["start"]]
    ends = [s["end"] for s in manifest if s["end"]]
    row = {
        "run_id": run_id,
        "subjects": "_".join(sorted({s["subj_ID"] for s in manifest})),
        "range_start": min(starts) if starts else "",
        "range_end": max(ends) if ends else "",
        "n_streams": len(manifest),
        "n_files": sum(s["file_count"] for s in manifest),
    }
    lock_path = index_path.with_suffix(index_path.suffix + ".lock")
    with _file_lock(lock_path):
        is_new = not index_path.exists()
        with open(index_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)


def append_motion_timeseries(path: Path, clip_filename: str, motion_by_frame) -> None:
    """Append one clip's per-sampled-frame motion values as a single row (clip filename,
    then the values space-separated, 4 significant digits), so per-frame activity (e.g. for
    an occupancy timeline or picking high-activity sub-clips) survives past the single
    found_motion/motion_perc summary in the per-file log. detect_motion.py skips the first
    N_FRAMES_TO_SKIP frames then keeps every SAMPLE_EVERY-th, so value i corresponds to true
    video frame ~(N_FRAMES_TO_SKIP + SAMPLE_EVERY) + SAMPLE_EVERY*i. Read back with
    `[float(x) for x in row['motion_values'].split()]`."""
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["clip_filename", "motion_values"])
        writer.writerow([clip_filename, " ".join(f"{value:.4g}" for value in motion_by_frame)])


class TeeStream:
    """File-like object that forwards each write to multiple underlying streams, so
    output reaches all of them (e.g. the live terminal and a log file) at once."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def start_terminal_log(log_path: Path):
    """Duplicate everything printed via print()/sys.stdout/sys.stderr to both the live
    terminal and a log file, so a run's terminal output is preserved after the fact. Only
    captures Python-level output -- messages written directly by native libraries (e.g.
    OpenCV) bypass Python's stream objects and won't reach the log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # force line buffering so live output and the log file don't lag behind what's
    # actually been printed
    sys.__stdout__.reconfigure(line_buffering=True)
    sys.__stderr__.reconfigure(line_buffering=True)
    log_file = open(log_path, "a", buffering=1)
    sys.stdout = TeeStream(sys.__stdout__, log_file)
    sys.stderr = TeeStream(sys.__stderr__, log_file)
    return log_file


# a dropped network mount commonly makes exists() hang on a stale handle rather than
# raise -- bound the check with a timeout via a worker thread so a hang can't block the
# fast/slow retry schedule below indefinitely. A persistent small pool (rather than one
# throwaway executor per call) avoids spinning up a new OS thread on every reachability
# check; a hung check's thread is simply abandoned (leaked) rather than joined.
_REACHABILITY_CHECK_TIMEOUT_SECONDS = 5
_reachability_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="reachability-check"
)


def _is_reachable(path: Path) -> bool:
    future = _reachability_executor.submit(path.exists)
    try:
        return future.result(timeout=_REACHABILITY_CHECK_TIMEOUT_SECONDS)
    except (OSError, concurrent.futures.TimeoutError):
        return False


def wait_for_connectivity(input_root: Path, output_root: Path) -> bool:
    """Block until both roots are reachable again (e.g. a network-mounted volume that
    dropped has been restored), retrying fast at first then patiently. Returns True once
    reachable (immediately, if it already was); returns False if still unreachable after
    the full timeout, in which case the caller should abort rather than keep trying files
    that can't possibly succeed."""
    if _is_reachable(input_root) and _is_reachable(output_root):
        return True

    print(f"WARNING: {input_root} or {output_root} is unreachable -- pausing and retrying")
    deadline = time.time() + CONNECTIVITY_TOTAL_TIMEOUT_HOURS * 3600

    for _ in range(CONNECTIVITY_FAST_RETRY_COUNT):
        time.sleep(CONNECTIVITY_FAST_RETRY_SECONDS)
        if _is_reachable(input_root) and _is_reachable(output_root):
            print("Connectivity restored, resuming")
            return True

    while time.time() < deadline:
        time.sleep(CONNECTIVITY_SLOW_RETRY_SECONDS)
        if _is_reachable(input_root) and _is_reachable(output_root):
            print("Connectivity restored, resuming")
            return True

    return False


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


def run_motion_detection(input_path: Path, motion_percentile: float, motion_threshold: float):
    """Perform motion detection and return results"""
    try:
        start = time.time()
        motion_by_frame = detect_motion.main(input_path, play_video=False)
        motion_perc = np.percentile(motion_by_frame, motion_percentile)
        found_motion = motion_perc >= motion_threshold
        detection_time = time.time() - start
        fract_frames_exceeding = np.mean(motion_by_frame > motion_threshold)

        print("     ", fract_frames_exceeding, "of frames exceeded motion threshold")
        return motion_perc, found_motion, detection_time, fract_frames_exceeding, motion_by_frame

    except IndexError:
        print(f"     WARNING: {input_path.resolve()} has not enough frames for motion detection")
        return None, True, None, None, None


def compress_video(
    input_path: Path,
    output_path: Path,
    motion_detected: bool,
    view: str,
    threads: int,
    taskcam_crf: int,
    compress_spd: str,
):
    """Compress video using ffmpeg. Returns (success, error message, compression time, and
    whether the view name was unrecognized and the conservative setting was used)."""
    start = time.time()

    base_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),  # converts into platform-dependent path format
        "-c:v",
        VIDEO_CODEC,
        "-preset",
        compress_spd,
        "-pix_fmt",
        PIX_FMT,
        "-threads",
        str(threads),
    ]

    unrecognized_view = False
    if motion_detected is None:
        print(f"    no motion classification: compressing at CRF {taskcam_crf}")
        command = base_command + ["-crf", str(taskcam_crf), str(output_path)]
    elif not motion_detected:
        print("    no motion, highly lossy compression will be used")
        command = base_command + ["-crf", str(CRF_NO_MOTION), "-g", str(GOP_NO_MOTION), str(output_path)]
    elif view in TASK_VIEWS:
        print("    motion, task view: minimally lossy compression will be used")
        command = base_command + ["-crf", str(taskcam_crf), str(output_path)]
    elif view in CAGE_VIEWS:
        print("    motion, cage view: more lossy compression will be used")
        command = base_command + ["-crf", str(CRF_CAGE_MOTION), str(output_path)]
    else:
        # a view name outside the known lists is most likely a misnamed file; never let that
        # silently select the strongest compression
        unrecognized_view = True
        print(
            f"    WARNING: unrecognized camera view '{view}' (known: {', '.join(TASK_VIEWS + CAGE_VIEWS)}); "
            "motion present, using minimally lossy compression"
        )
        command = base_command + ["-crf", str(taskcam_crf), str(output_path)]

    # run ffmpeg compression
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        compression_time = time.time() - start
        return True, None, compression_time, unrecognized_view
    except subprocess.CalledProcessError as e:
        compression_time = time.time() - start
        return False, e.stderr.decode("utf-8"), compression_time, unrecognized_view


def log_not_attempted(logger: Logger, remaining: list[Path]) -> None:
    for path in remaining:
        logger.reset()
        logger.input_path = path
        logger.status = STATUS_NOT_ATTEMPTED
        logger.action_item = True
        logger.skipped_reason = "run aborted before this file was attempted"
        try:
            logger.append()
        except Exception as e:
            print(f"CRITICAL: could not log the {len(remaining)} never-attempted file(s): {e}")
            return


def print_summary(
    logger: Logger,
    n_matched: int,
    n_unmatched_mp4: int,
    n_other: int,
    n_unrecognized_view: int,
    motion_detection: bool,
    n_motion_skipped_compressed_source: int,
    taskcam_crf: int,
) -> None:
    n_rows = sum(logger.status_counts.values())
    print("\n==== run summary ====")
    print(f"files matching --pattern: {n_matched}")
    print(f"rows logged for them: {n_rows - logger.status_counts[STATUS_PATTERN_MISMATCH]}")
    for status, count in sorted(logger.status_counts.items()):
        print(f"    {status}: {count}")
    if motion_detection:
        print("motion detection: on" + (
            f"; skipped for {n_motion_skipped_compressed_source} clip(s) from already-compressed sources "
            f"(encoded at CRF {taskcam_crf})" if n_motion_skipped_compressed_source else ""
        ))
    else:
        print(f"motion detection: off -- every clip encoded at CRF {taskcam_crf}")
    if n_unrecognized_view:
        print(
            f"WARNING: {n_unrecognized_view} file(s) had an unrecognized camera view name and were compressed "
            "with the minimally lossy setting -- check for misnamed files (see WARNING lines above)"
        )
    unlisted = " (not listed individually; review by hand)" if n_unmatched_mp4 > MAX_LISTED_PATTERN_MISMATCHES else ""
    print(f".mp4 files not matching --pattern (not checked or copied): {n_unmatched_mp4}{unlisted}")
    print(f"non-.mp4 files in source (not checked or copied): {n_other}")
    print(f"rows flagged for human review: {logger.n_action_items} -> {logger.action_items_path}")


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
    overwrite_raw: bool,
    overwrite_compressed: bool,
    output_studyname: str,
    motion_detection: bool,
):
    """(optional motion detection) -> compression."""

    kwargs = locals()

    # confirm both roots are reachable before creating the run's metadata folder or
    # writing anything else under `output`
    if not wait_for_connectivity(input, output):
        print(
            f"ABORTING: {input} or {output} unreachable after "
            f"{CONNECTIVITY_TOTAL_TIMEOUT_HOURS} hours -- exiting before run setup"
        )
        return

    # each run gets its own metadata folder, named only for uniqueness (start time + a short
    # random suffix) -- what subjects/views/dates it covers is recorded inside, not encoded
    # into the folder name itself. Computed first, and the terminal log started immediately,
    # so the log captures the run from its very first message.
    run_id = f"videoproc_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    run_metadata_dir = output / "videoproc_run_metadata" / run_id
    log_file = start_terminal_log(run_metadata_dir / "terminal.log")
    print(f"Starting videoproc run {run_id}")
    print(f"Call: python3 {' '.join(sys.argv)}")

    try:
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

        manifest = build_manifest(input_paths)
        write_manifest(run_metadata_dir / "manifest.csv", manifest)
        append_index_row(output / "videoproc_run_metadata" / "index.csv", run_id, manifest)

        config = dict(kwargs)
        config["code_version"] = code_version()
        config["encoding_policy"] = encoding_policy(taskcam_crf, compress_spd)
        config["motion_detection"] = {
            "enabled": motion_detection,
            "percentile": motion_percentile,
            "threshold": motion_threshold,
            **detect_motion.PARAMETERS,
        }
        if motion_detection:
            print(f"motion detection ON: threshold {motion_threshold} at the {motion_percentile}th percentile")
        else:
            print(f"motion detection OFF: every clip encoded at CRF {taskcam_crf}")
        config_path = run_metadata_dir / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=4, default=str))

        log_path = run_metadata_dir / "log.csv"
        motion_timeseries_path = run_metadata_dir / "motion_timeseries.csv"

        logger = Logger(log_path, run_metadata_dir / "action_items.csv")

        unmatched_mp4, n_other_files = find_unmatched_files(input, input_paths)
        log_unmatched_files(logger, unmatched_mp4, n_other_files, pattern)

        n_unrecognized_view = 0
        n_motion_skipped_compressed_source = 0
        for i, input_path in enumerate(input_paths):
            if not wait_for_connectivity(input, output):
                print(
                    f"ABORTING: {input} or {output} still unreachable after "
                    f"{CONNECTIVITY_TOTAL_TIMEOUT_HOURS} hours -- stopping run rather than "
                    "continuing to fail on every remaining file"
                )
                log_not_attempted(logger, input_paths[i:])
                break

            logger.reset()
            logger.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.input_path = input_path
            print(f"processing {input_path.name} at {logger.start_time}.")

            try:
                # (1) parse input path
                parsed = parse_filenames(input_path)
                if parsed is None:
                    raise SkipFile(
                        STATUS_UNPARSEABLE,
                        "could not parse filename (unrecognized modern or legacy shape)",
                        action_item=True,
                    )
                subj_ID, view, recording_date, recording_time = parsed

                # (2) create output path
                output_path = build_output_path(
                    output, output_studyname, subj_ID, view, recording_date, recording_time
                )
                logger.output_path = output_path

                # (3) decide: skip, copy, or compress, from the state of source and destination
                decision = classify_file(
                    input_path,
                    output_path,
                    recompress=recompress,
                    overwrite_raw=overwrite_raw,
                    overwrite_compressed=overwrite_compressed,
                )
                if decision.skip:
                    raise decision.skip
                if decision.note:
                    print(f"    {decision.note}")
                input_n_frames = decision.input_n_frames

                # (4) make output directory
                print(f"    will attempt to save as {output_path.name}.")
                output_path.parent.mkdir(parents=True, exist_ok=True)

                # (5) an already-compressed source is copied as-is unless --recompress
                if decision.action == ACTION_COPY:
                    copy_file(input_path, output_path)
                    logger.status = STATUS_COPIED
                    print("    source already compressed; copied without re-encoding")
                    continue

                # (6) motion detection -- only when requested, and never on an already-compressed
                # source, whose compression artifacts register as motion
                found_motion = None
                if motion_detection and decision.input_codec != RAW_CODEC:
                    n_motion_skipped_compressed_source += 1
                    print(f"    source is already compressed ({decision.input_codec}); motion detection skipped")
                elif motion_detection:
                    motion_perc, found_motion, detection_time, fract_frames_exceeding, motion_by_frame = (
                        run_motion_detection(input_path, motion_percentile, motion_threshold)
                    )
                    logger.motion_detection_time = detection_time
                    logger.motion_perc = motion_perc
                    logger.found_motion = found_motion
                    logger.fract_frames_exceeding = fract_frames_exceeding
                    if motion_by_frame is not None:
                        append_motion_timeseries(motion_timeseries_path, input_path.name, motion_by_frame)

                # (7) video compression; found_motion None means "no motion classification: best quality"
                success, err_msg, compression_time, unrecognized_view = compress_video(
                    input_path, output_path, found_motion, view, n_threads, taskcam_crf, compress_spd
                )
                n_unrecognized_view += unrecognized_view
                logger.compression_time = compression_time
                logger.compression_success = success

                if err_msg:
                    logger.error = err_msg

                # (8) check output exists, has non-zero size, and frame count matches
                _, output_n_frames = get_codec_nframes(output_path)
                if (
                    (not output_path.exists())
                    or (output_path.stat().st_size == 0)
                    or (input_n_frames != output_n_frames)
                ):
                    raise SkipFile(
                        STATUS_OUTPUT_INVALID,
                        "compressed output missing, empty, or frame count differs from source",
                        action_item=True,
                    )

                logger.compression_ratio = input_path.stat().st_size / output_path.stat().st_size
                logger.valid_output = True
                logger.status = STATUS_COMPRESSED

            except SkipFile as s:
                logger.status = s.status
                logger.action_item = s.action_item
                logger.skipped_reason = str(s)
                label = "FLAGGED" if s.action_item else "SKIPPED"
                print(f"    {label} {input_path.resolve()}: {s}")
            except Exception as e:
                logger.status = STATUS_ERROR
                logger.action_item = True
                logger.error = str(e)
                print(f"    ERROR {input_path.resolve()}: {e}")

            # one log row per file, whatever the outcome
            finally:
                try:
                    logger.append()
                except Exception as e:
                    print(f"CRITICAL: Failed to write log row for {input_path.resolve()}: {e}")

        print_summary(
            logger,
            len(input_paths),
            len(unmatched_mp4),
            n_other_files,
            n_unrecognized_view,
            motion_detection,
            n_motion_skipped_compressed_source,
            taskcam_crf,
        )

    finally:
        # flush and restore stdout/stderr and close the log file, even on an early return
        # or an uncaught exception, so terminal.log ends up complete
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_file.close()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="compress and transfer videos", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # positional arguments: required
    parser.add_argument("input", type=Path, help="source path")
    parser.add_argument("output", type=Path, help="destination path")
    parser.add_argument(
        "--output_studyname",
        type=str,
        required=True,
        help="study-name prefix for compressed output folders/filenames "
        "(independent of whatever prefix the source videos use; no default -- must be set explicitly)",
    )

    # keyword arguments: optional
    parser.add_argument("--pattern", default="**/LS*/*.mp4", type=str, help="pattern to match")
    parser.add_argument(
        "--motion_detection",
        action="store_true",
        help="analyse each raw clip for motion and compress clips without motion more strongly; also saves "
        "motion_timeseries.csv (per-frame motion values), which can be used to develop a custom threshold. "
        "Off by default: every clip is encoded at --taskcam_crf. Skipped automatically for already-compressed "
        "sources, whose compression artifacts register as motion",
    )
    parser.add_argument(
        "--motion_percentile",
        default=99.9,
        type=float,
        help="with --motion_detection: percentile of the per-frame motion values used as the clip's motion score",
    )
    parser.add_argument(
        "--motion_threshold",
        default=0.0,
        type=float,
        help="with --motion_detection: a clip whose motion score is at or above this counts as having motion. "
        "The default 0 classifies every clip as motion (encoded by view at --taskcam_crf / CRF 30) while still "
        "recording motion_timeseries.csv; this lab uses 0.001, calibrated on its own raw video",
    )
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
    parser.add_argument(
        "--recompress",
        action="store_true",
        help="re-encode source files that are already compressed instead of copying them as-is "
        "(emergency use only: compresses lossy video a second time)",
    )
    parser.add_argument(
        "--overwrite_raw",
        action="store_true",
        help="replace a raw (uncompressed) video already at the destination with the processed source",
    )
    parser.add_argument(
        "--overwrite_compressed",
        action="store_true",
        help="replace a compressed video already at the destination with a fresh processing of the source",
    )
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
