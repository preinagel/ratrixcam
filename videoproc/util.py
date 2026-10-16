"""Naming, path and video-probe helpers shared by the videoproc tools.

Intended to become the module shared with ratrixcam, so that both tools build and parse
file and folder names with one convention."""

import re
import sys
from datetime import date, datetime
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root; the shared module moves next (#26)
from ratrix_naming import code_version as _code_version  # noqa: E402


def code_version() -> str:
    """Git commit of the videoproc checkout ('+dirty' if edited), recorded in every run's
    config.json so settings hardwired in code can be recovered later."""
    return _code_version(Path(__file__))

# FourCC ratrixcam writes when it records; any other openable codec has been through
# downstream processing and is treated as "compressed"
RAW_CODEC = "FMP4"

# optional site-specific parser for filename shapes the standard rule does not cover;
# see custom_filename_parser_example.py. No module means no special cases.
try:
    import custom_filename_parser
except ImportError:
    custom_filename_parser = None


# the legacy experiment's standard clip name, every field closed-set or strictly parsed
SUBJECT_IDS = frozenset(f"rat{n}" for n in range(556, 564))  # rat556 .. rat563
VIEWS = frozenset(("face", "lid", "buddy", "home"))
RECORDING_DATE_FIRST = date(2025, 6, 27)
RECORDING_DATE_LAST = date(2025, 8, 29)
VIDEO_EXT = ".mp4"
STANDARD_STEM = re.compile(r"(rat\d{3})_([a-z]+)_(\d{8})_(\d{2}-\d{2}-\d{2})")


def parse_filenames(video_fname: Path) -> tuple[str, str, str, str] | None:
    """Split a clip filename into (subjectID, view, YYYYMMDD, HH-MM-SS).

    The standard name is rat<ID>_<view>_<YYYYMMDD>_<HH-MM-SS>.mp4 with the subject in
    SUBJECT_IDS, the view in VIEWS, a real date within the experiment's recording dates
    and a real time. A name that fails any of these is offered, extension included, to
    custom_filename_parser.parse() if that module exists; otherwise, or if that also declines,
    the result is None and the caller must treat the file as unparseable."""
    stem = video_fname.stem
    if video_fname.suffix == VIDEO_EXT:
        m = STANDARD_STEM.fullmatch(stem)
        if m:
            subj_ID, camera_view, filming_date, filming_time = m.groups()
            recorded = parse_recording_datetime(filming_date, filming_time)
            if (
                subj_ID in SUBJECT_IDS
                and camera_view in VIEWS
                and recorded is not None
                and RECORDING_DATE_FIRST <= recorded.date() <= RECORDING_DATE_LAST
            ):
                return subj_ID, camera_view, filming_date, filming_time
    if custom_filename_parser is not None:
        return custom_filename_parser.parse(video_fname.name)
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
