"""Naming, path and video-probe helpers shared by the videoproc tools.

Intended to become the module shared with ratrixcam, so that both tools build and parse
file and folder names with one convention."""

import subprocess
from datetime import datetime
from pathlib import Path

import cv2


def code_version() -> str:
    """Short git commit of the videoproc checkout, '+dirty' if it has uncommitted edits,
    'unknown' if git or the repository is unavailable. Recorded in every run's config.json
    so settings hardwired in code can be recovered later."""
    here = Path(__file__).resolve().parent
    try:
        commit = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(here), "status", "--porcelain", "--", str(here)], capture_output=True, text=True, check=True
        ).stdout.strip()
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"

# FourCC ratrixcam writes when it records; any other openable codec has been through
# downstream processing and is treated as "compressed"
RAW_CODEC = "FMP4"

# optional site-specific parser for filename shapes the standard rule does not cover;
# see custom_filename_parser_example.py. No module means no special cases.
try:
    import custom_filename_parser
except ImportError:
    custom_filename_parser = None


def parse_filenames(video_fname: Path) -> tuple[str, str, str, str] | None:
    """Split a video filename into (subjectID, view, YYYYMMDD, HH-MM-SS).

    The standard shape is subjID_view_date_time, e.g. rat558_buddy_20250722_09-41-55.mp4.
    Anything else is offered to custom_filename_parser.parse() if that module exists.
    Returns None when neither recognizes the name; callers must treat that as unparseable
    rather than substitute placeholder values.
    TODO(generalize): validate tokens against the configured subject-ID and view lists and
    a real date parse, instead of a bare token-count split."""
    stem = video_fname.stem
    tokens = stem.split("_")
    if len(tokens) == 4:
        subj_ID, camera_view, filming_date, filming_time = tokens
        return subj_ID, camera_view, filming_date, filming_time
    if custom_filename_parser is not None:
        return custom_filename_parser.parse(stem)
    return None


def parse_recording_datetime(filming_date: str, filming_time: str) -> datetime | None:
    try:
        return datetime.strptime(f"{filming_date}_{filming_time}", "%Y%m%d_%H-%M-%S")
    except ValueError:
        return None


def build_output_path(
    output_root: Path, output_studyname: str, subj_ID: str, view: str, recording_date: str, recording_time: str
) -> Path:
    """Standard final-destination path for a compressed clip."""
    new_filename = f"{subj_ID}_{view}_{recording_date}_{recording_time}.mp4"
    return output_root / subj_ID / f"{output_studyname}_{subj_ID}_{view}_{recording_date}" / new_filename


def get_codec_nframes(path: Path):
    """(FourCC string, frame count) of a video, or (None, None) if it cannot be opened."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None, None
    codec = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((codec >> 8 * i) & 0xFF) for i in range(4)])
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return codec, n_frames
