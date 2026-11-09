#!/usr/bin/env python3 -u

import argparse
import concurrent.futures
import csv
import io
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pprint import pprint

import cv2
import detect_motion
import numpy as np
import numpy.typing as npt
from cv2.typing import MatLike

from classify import (
    ACTION_COPY,
    STATUS_COMPRESSED,
    STATUS_COPIED,
    STATUS_ERROR,
    STATUS_MISSING_SOURCE,
    STATUS_NOT_ATTEMPTED,
    STATUS_OUTPUT_INVALID,
    Logger,
    SkipFile,
    classify_file,
)
from manifest import (
    DEFAULT_CRF,
    SUMMARY_FILENAME,
    ManifestError,
    ManifestRow,
    SummaryRow,
    read_manifests,
    summarize_by_subject_and_view,
    write_summary,
)
from util import build_output_path, code_version, file_lock, get_codec_nframes, shorten

# NON-NEGOTIABLE HARDWIRED CONSTANTS DEFINED HERE
# connectivity-loss handling: if the input/output root becomes unreachable (e.g. a
# network-mounted volume drops), retry fast at first, then patiently, before giving up --
# rather than spinning through the rest of the file list failing on every single file
CONNECTIVITY_FAST_RETRY_SECONDS = 10
CONNECTIVITY_FAST_RETRY_COUNT = 6  # 1 minute total
CONNECTIVITY_SLOW_RETRY_SECONDS = 600  # 10 minutes
CONNECTIVITY_TOTAL_TIMEOUT_HOURS = 10

VIDEO_CODEC = "libx264"
PIX_FMT = "yuv420p"
CRF_NO_MOTION = 40  # aggressive compression setting for videos with no motion, if motion detection used
GOP_NO_MOTION = 1800  # group of pictures: keyframe interval for aggressive compression of no-motion videos

# motion review: material kept so a no-motion classification can be checked before the raw
# clip is deleted (see --motion_review)
NO_MOTION_STILLS_DIRNAME = "no_motion_stills"
MOTION_REVIEW_FILENAME = "motion_review.csv"
MOTION_REVIEW_FIELDS = [
    "clip_filename",
    "output_path",
    "motion_max",
    "n_frames_over_threshold",
    "motion_perc",
    "peak_frame_number",
    "still_path",
]

INDEX_FIELDS = [
    "run_id", "subjects", "range_start", "range_end", "n_streams", "n_files", "motion_detection", "motion_threshold",
]


def index_row(
    run_id: str, summary: list[SummaryRow], motion_detection: bool | None = None, motion_threshold: float | None = None
) -> dict[str, object]:
    """The one-line summary of a run that goes into index.csv. The scope columns derive from
    the run folder's summary_by_subj_and_view.csv (see rebuild_index.py); the motion columns
    are a compression run's settings, blank for a confirm run."""
    starts = [str(s["start"]) for s in summary if s["start"]]
    ends = [str(s["end"]) for s in summary if s["end"]]
    return {
        "run_id": run_id,
        "subjects": "_".join(sorted({str(s["subjID"]) for s in summary})),
        "range_start": min(starts) if starts else "",
        "range_end": max(ends) if ends else "",
        "n_streams": len(summary),
        "n_files": sum(int(s["file_count"]) for s in summary),
        "motion_detection": "" if motion_detection is None else motion_detection,
        "motion_threshold": "" if motion_threshold is None else motion_threshold,
    }


def append_index_row(
    index_path: Path, run_id: str, summary: list[SummaryRow], motion_detection: bool | None = None, motion_threshold: float | None = None
) -> bool:
    """Append one summary line for this run to the top-level run index (creating it with a
    header on first use), so later lookups don't require opening every run's own folder.
    Locked so concurrent runs sharing the same output root, on one or several machines,
    can't interleave writes or duplicate the header row. Returns False (after a warning)
    if the index stayed locked for the whole timeout; the run itself continues, since its
    own folder is the complete record and the index can be rebuilt from it."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    row = index_row(run_id, summary, motion_detection, motion_threshold)
    lock_path = index_path.with_suffix(index_path.suffix + ".lock")
    try:
        with file_lock(lock_path):
            is_new = not index_path.exists()
            with open(index_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
                if is_new:
                    writer.writeheader()
                writer.writerow(row)
        return True
    except TimeoutError as e:
        print(
            f"WARNING: {e}; this run is recorded only in its own folder "
            "(run rebuild_index.py to regenerate index.csv later)"
        )
        return False


def append_motion_timeseries(path: Path, clip_filename: str, motion_by_frame) -> None:
    """Append one clip's per-sampled-frame motion values as a single row (clip filename,
    then the values space-separated, 4 significant digits), so per-frame activity (e.g. for
    an occupancy timeline or picking high-activity sub-clips) survives past the single
    found_motion/motion_perc summary in the per-file log. detect_motion.py scores every
    SAMPLE_EVERY-th frame after the first N_FRAMES_TO_SKIP, so value i is the score of 0-based
    frame N_FRAMES_TO_SKIP + SAMPLE_EVERY*(i+1) - 1, the numbering peak_frame_number uses.
    Read back with `[float(x) for x in row['motion_values'].split()]`."""
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["clip_filename", "motion_values"])
        writer.writerow([clip_filename, " ".join(f"{value:.4g}" for value in motion_by_frame)])


def save_peak_still(stills_dir: Path, clip_filename: str, frame, frame_number: int) -> Path:
    """JPEG of the sampled frame that scored highest in a clip classified no-motion, so the
    classification can be checked by eye before the raw clip is deleted."""
    stills_dir.mkdir(parents=True, exist_ok=True)
    path = stills_dir / f"{Path(clip_filename).stem}_frame{frame_number}.jpg"
    cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return path


def append_motion_review_row(path: Path, row: dict[str, object]) -> None:
    """One row per no-motion clip with at least one sampled frame at or above the threshold."""
    is_new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MOTION_REVIEW_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


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
    for stream in (sys.__stdout__, sys.__stderr__):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(line_buffering=True)
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
    retry_pause: float = 0.1  # seconds between attempts
    out_file.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            shutil.copy2(in_file, out_file)
            if out_file.exists() and out_file.is_file():
                return
        except Exception as e:
            print(
                f"WARNING: Failed to copy {in_file} to {out_file} (attempt {attempt}/{max_retries}), retrying in {retry_pause} seconds: {e}"
            )
            time.sleep(retry_pause)
    raise RuntimeError(f"Failed to copy {in_file} to {out_file} after {max_retries} attempts")


@dataclass
class MotionResult:
    motion_perc: float
    found_motion: bool
    detection_time: float
    fract_frames_exceeding: float
    motion_by_frame: npt.NDArray[np.float64]
    motion_max: float
    n_frames_over_threshold: int
    peak_frame: MatLike | None
    peak_frame_number: int | None


def run_motion_detection(input_path: Path, motion_percentile: float, motion_threshold: float) -> MotionResult | None:
    """Motion detection on one clip. None when the clip has too few frames to score."""
    try:
        start = time.time()
        motion_by_frame, peak_frame, peak_frame_number = detect_motion.motion_scores(
            input_path, play_video=False, return_peak=True
        )
        motion_perc = float(np.percentile(motion_by_frame, motion_percentile))
        over_threshold = motion_by_frame >= motion_threshold
        print("     ", float(np.mean(over_threshold)), "of frames reached the motion threshold")
        return MotionResult(
            motion_perc,
            bool(motion_perc >= motion_threshold),  # a Python bool: the caller tests `is False`
            time.time() - start,
            float(np.mean(over_threshold)),
            motion_by_frame,
            float(motion_by_frame.max()),
            int(over_threshold.sum()),
            peak_frame,
            peak_frame_number,
        )

    except IndexError:
        print(f"     WARNING: {input_path.resolve()} has not enough frames for motion detection")
        return None


def compress_video(
    input_path: Path,
    output_path: Path,
    threads: int,
    crf: int,
    gop: int | None,
    compress_spd: str,
):
    """Compress video using ffmpeg at the given CRF, with a keyframe interval only when gop is
    given. Returns (success, error message, compression time)."""
    start = time.time()

    command = [
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
        "-crf",
        str(crf),
    ]
    if gop is not None:
        command += ["-g", str(gop)]
    command.append(str(output_path))

    # run ffmpeg compression
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        compression_time = time.time() - start
        return True, None, compression_time
    except subprocess.CalledProcessError as e:
        compression_time = time.time() - start
        return False, e.stderr.decode("utf-8"), compression_time


def log_not_attempted(logger: Logger, remaining: list[ManifestRow], input_root: Path) -> None:
    for row in remaining:
        logger.reset()
        logger.input_path = input_root / row.path
        logger.subjID, logger.view = row.subjID, row.view
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
    n_listed: int,
    motion_detection: bool,
    motion_review: bool,
    n_no_motion: int,
    flagged_by_view: Counter[str],
) -> None:
    n_rows = sum(logger.status_counts.values())
    print("\n==== run summary ====")
    print(f"clips listed in the manifest(s): {n_listed}")
    print(f"rows logged for them: {n_rows}")
    for status, count in sorted(logger.status_counts.items()):
        print(f"    {status}: {count}")
    if motion_detection:
        print("motion detection: on")
        if motion_review:
            print(f"no-motion clips: {n_no_motion}; peak still saved for each in {NO_MOTION_STILLS_DIRNAME}/")
            n_flagged = sum(flagged_by_view.values())
            by_view = ", ".join(f"{view} {n}" for view, n in sorted(flagged_by_view.items())) if n_flagged else ""
            print(
                f"    flagged for review (>= 1 sampled frame at the threshold): {n_flagged}"
                + (f" ({by_view}) -> {MOTION_REVIEW_FILENAME}; time series kept for these" if n_flagged else "")
            )
    else:
        print("motion detection: off")
    print(f"rows flagged for human review: {logger.n_action_items} -> {logger.action_items_path}")


def main(
    input: Path,
    output: Path,
    manifest: list[Path],
    studyname: str | None,
    subject_prefix: str,
    motion_percentile: float,
    motion_threshold: float,
    n_threads: int,
    compress_spd: str,
    recompress: bool,
    overwrite_raw: bool,
    overwrite_compressed: bool,
    motion_detection: bool,
    motion_review: bool,
    call_note: str | None = None,
) -> Path | None:
    """(optional motion detection) -> compression, for every clip the manifest(s) list.
    `call_note`, given by a caller such as the ratrixcam wrapper, is printed in place of the
    command line. Returns the run's metadata folder, or None if the run stopped before
    processing."""

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
    def short(path: Path | str) -> str:
        return shorten(path, input, output)

    run_id = f"compression_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    run_metadata_dir = output / "videoproc_run_metadata" / run_id
    log_file = start_terminal_log(run_metadata_dir / "terminal.log")
    print(f"Starting compression run {run_id} -- code version {code_version()}")
    print(call_note if call_note else f"Call: python3 {' '.join(sys.argv)}")
    print(f"source root      {input.resolve()}")
    print(f"destination root {output.resolve()}")

    try:
        # make sure ffmpeg exists as a shell command
        ffmpeg_cmd = shutil.which("ffmpeg")
        if ffmpeg_cmd is None:
            print("Cannot find ffmpeg!")
            return
        else:
            print(f"ffmpeg found at {ffmpeg_cmd}")

        try:
            rows = read_manifests(manifest)
        except ManifestError as e:
            print(f"ERROR: {e}. Exiting.")
            return
        if not rows:
            print("the manifest(s) list no clips. Exiting.")
            return
        print(f"{len(rows)} clip(s) listed in {len(manifest)} manifest(s)")

        summary = summarize_by_subject_and_view(rows)
        write_summary(run_metadata_dir / SUMMARY_FILENAME, summary)
        index_path = output / "videoproc_run_metadata" / "index.csv"
        indexed = append_index_row(index_path, run_id, summary, motion_detection, motion_threshold)

        if motion_detection:
            print("motion detection ON: clips with no detected motion will be aggressively compressed")
            print(f"    threshold {motion_threshold} at the {motion_percentile}th percentile")
            if motion_review:
                print(
                    f"motion review ON: peak still for every no-motion clip in {NO_MOTION_STILLS_DIRNAME}/; "
                    "time series kept for motion clips and for no-motion clips with any sampled frame at the threshold"
                )
            else:
                print("motion review OFF: no stills; time series kept for every clip")
        else:
            print("motion detection OFF: all clips will be compressed at their manifest crf regardless of motion")

        log_path = run_metadata_dir / "compression_log.csv"
        motion_timeseries_path = run_metadata_dir / "motion_timeseries.csv"

        logger = Logger(log_path, run_metadata_dir / "action_items.csv")

        n_no_motion = 0
        flagged_by_view: Counter[str] = Counter()
        for i, row in enumerate(rows):
            if not wait_for_connectivity(input, output):
                print(
                    f"ABORTING: {input} or {output} still unreachable after "
                    f"{CONNECTIVITY_TOTAL_TIMEOUT_HOURS} hours -- stopping run rather than "
                    "continuing to fail on every remaining file"
                )
                log_not_attempted(logger, rows[i:], input)
                break

            input_path = input / row.path
            view = row.view
            logger.reset()
            logger.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.input_path = input_path
            logger.subjID, logger.view = row.subjID, row.view
            print(f"processing {row.path} at {logger.start_time}.")

            try:
                # (1) the file must be where the manifest says
                if not input_path.is_file():
                    raise SkipFile(STATUS_MISSING_SOURCE, "listed in the manifest but not found at the source", action_item=True)

                # (2) create output path
                output_path = build_output_path(
                    output, subject_prefix, studyname, row.subjID, row.view, row.clip_start, input_path.name
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

                # (5) if classify_file returned a decision to copy the file, do so 
                #     (e.g.,an already-compressed source is copied as-is unless --recompress)
                if decision.action == ACTION_COPY:
                    copy_file(input_path, output_path)
                    logger.status = STATUS_COPIED
                    print("    source already compressed; copied without re-encoding")
                    continue

                # (6) motion detection -- only when requested
                found_motion = None
                if motion_detection:
                    motion = run_motion_detection(input_path, motion_percentile, motion_threshold)
                    # too few frames to score: treated as motion, i.e. the conservative encoding
                    found_motion = True if motion is None else motion.found_motion
                    if motion is not None:
                        logger.motion_detection_time = motion.detection_time
                        logger.motion_perc = motion.motion_perc
                        logger.found_motion = motion.found_motion
                        logger.fract_frames_exceeding = motion.fract_frames_exceeding
                        logger.motion_max = motion.motion_max
                        logger.n_frames_over_threshold = motion.n_frames_over_threshold
                        flagged = False
                        if motion_review and not motion.found_motion:
                            n_no_motion += 1
                            assert motion.peak_frame is not None and motion.peak_frame_number is not None  # return_peak=True
                            still = save_peak_still(
                                run_metadata_dir / NO_MOTION_STILLS_DIRNAME,
                                input_path.name,
                                motion.peak_frame,
                                motion.peak_frame_number,
                            )
                            flagged = motion.n_frames_over_threshold > 0
                            logger.motion_review_flag = flagged
                            if flagged:
                                flagged_by_view[view] += 1
                                print(
                                    f"    REVIEW: no motion by the {motion_percentile}th-percentile rule, but "
                                    f"{motion.n_frames_over_threshold} sampled frame(s) reach the threshold "
                                    f"(max {motion.motion_max:.4g}); still and time series kept"
                                )
                                append_motion_review_row(
                                    run_metadata_dir / MOTION_REVIEW_FILENAME,
                                    {
                                        "clip_filename": input_path.name,
                                        "output_path": output_path,
                                        "motion_max": f"{motion.motion_max:.4g}",
                                        "n_frames_over_threshold": motion.n_frames_over_threshold,
                                        "motion_perc": f"{motion.motion_perc:.4g}",
                                        "peak_frame_number": motion.peak_frame_number,
                                        "still_path": still,
                                    },
                                )
                        if motion.found_motion or flagged or not motion_review:
                            append_motion_timeseries(motion_timeseries_path, input_path.name, motion.motion_by_frame)
                # (7) choose the encoding: if motion detection is in use, a motionless clip gets the
                # fixed aggressive setting; otherwise, or when motion was detected, the CRF comes
                # from the manifest (DEFAULT_CRF if the row has none)
                if found_motion is False:
                    crf, gop = CRF_NO_MOTION, GOP_NO_MOTION
                    print("    no motion, highly lossy compression will be used")
                else:
                    crf, gop = (row.crf if row.crf is not None else DEFAULT_CRF), None
                logger.crf = crf

                # (8) video compression
                success, err_msg, compression_time = compress_video(
                    input_path, output_path, n_threads, crf, gop, compress_spd
                )
                logger.compression_time = compression_time
                logger.compression_success = success

                if err_msg:
                    logger.error = err_msg

                # (9) check output exists, has non-zero size, and frame count matches
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
                print(f"    {label} {short(input_path)}: {s}")
            except Exception as e:
                logger.status = STATUS_ERROR
                logger.action_item = True
                logger.error = str(e)
                print(f"    ERROR {short(input_path)}: {e}")

            # one log row per file, whatever the outcome
            finally:
                try:
                    logger.append()
                except Exception as e:
                    print(f"CRITICAL: Failed to write log row for {short(input_path)}: {e}")

        print_summary(
            logger,
            len(rows),
            motion_detection,
            motion_review,
            n_no_motion,
            flagged_by_view,
        )
        if not indexed:
            append_index_row(index_path, run_id, summary, motion_detection, motion_threshold)
        return run_metadata_dir

    finally:
        # flush and restore stdout/stderr and close the log file, even on an early return
        # or an uncaught exception, so terminal.log ends up complete
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_file.close()


def add_processing_arguments(parser: argparse.ArgumentParser) -> None:
    """The options that control how each clip is processed, shared with the ratrixcam wrapper
    (process_ratrixcam_output.py); the source, destination and manifest arguments are the
    caller's."""
    parser.add_argument(
        "--motion_detection",
        action="store_true",
        help="analyse each raw clip for motion and compress clips without motion more strongly; also saves "
        "motion_timeseries.csv (per-frame motion values), which can be used to develop a custom threshold. "
        "Off by default",
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
        "The default 0 classifies every clip as motion (encoded according to camera policy) while still "
        "recording motion_timeseries.csv; a working value is calibrated on the rig's own raw video, "
        "e.g. from the motion_timeseries.csv of a run at the default",
    )
    parser.add_argument(
        "--motion_review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="with --motion_detection: save the highest-scoring frame of every no-motion clip as a still "
        "in the run's no_motion_stills/ folder, and keep the motion time series only for motion clips and "
        "for no-motion clips in which at least one sampled frame reaches --motion_threshold (listed in "
        "motion_review.csv). --no-motion_review: no stills, time series for every clip",
    )
    parser.add_argument(
        "--n_threads",
        default=4,
        type=int,
        help="number of threads ffmpeg may use; about the machine's core count minus one",
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


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="compress and transfer the videos listed in a manifest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # positional arguments: required
    parser.add_argument("input", type=Path, help="source root; manifest paths are relative to it")
    parser.add_argument("output", type=Path, help="destination root")
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        required=True,
        help="manifest CSV listing the clips to process (path, subjID, view, clip_start[, crf]); "
        "repeat the option to process several manifests as one run",
    )

    # keyword arguments: optional
    parser.add_argument(
        "--studyname",
        type=str,
        default=None,
        help="study-name prefix for the destination day folders; omitted, the folders have no prefix",
    )
    parser.add_argument(
        "--subject_prefix",
        type=str,
        default="subj",
        help="prefix joined to the manifest's subjID to form the subject name in destination paths; '' for none",
    )
    add_processing_arguments(parser)
    kwargs = vars(parser.parse_args())

    # argument validation
    if not kwargs["input"].is_dir():
        raise NotADirectoryError(f"{kwargs['input']} is not a valid directory")

    if not kwargs["output"].is_dir():
        raise NotADirectoryError(f"{kwargs['output']} is not a valid directory")

    if kwargs["input"].resolve() == kwargs["output"].resolve():
        raise ValueError("input and output paths cannot be the same")

    for m in kwargs["manifest"]:
        if not m.is_file():
            raise FileNotFoundError(f"manifest {m} not found")

    # confirm configuration with user
    print("\nconfiguration:")
    pprint(kwargs, sort_dicts=False)
    input("\n[enter] to continue: ")

    main(**kwargs)
