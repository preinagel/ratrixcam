"""Names and paths shared by ratrixcam and videoproc, plus the code-version stamp both write
into their records. Start of the shared naming module (GitHub #26); the video filename
builders/parsers still live in videoproc/util.py and move here next.

Folder layout for a recording run's metadata (GitHub #34):

    <save_path>/ratrixcam_run_metadata/<run_id>/
        <config filename>           copy of the config file given with -c, same name, taken at Start Recording
        terminal_<run_id>.log       verbatim terminal output of the whole run
        notes.txt                   template for the human's notes
        stills/                     the per-camera status stills as they were at shutdown
        <anything else>             other files found in the temp folder at shutdown
"""

import importlib.util
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

RUN_METADATA_DIRNAME = "ratrixcam_run_metadata"
NOTES_FILENAME = "notes.txt"
STILLS_DIRNAME = "stills"
NOTES_TEMPLATE = "Notes for this recording run (free text; edit by hand).\n"


def code_version(anchor: Path | None = None) -> str:
    """Short git commit of the checkout containing `anchor` (default: this file), '+dirty' if
    it has uncommitted edits, 'unknown' if git or the repository is unavailable."""
    here = (anchor or Path(__file__)).resolve().parent
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


def safe_token(text: str) -> str:
    """A string made safe for use inside a file or folder name on any OS."""
    return "".join(c if c.isalnum() or c in "-." else "_" for c in text.strip()) or "unnamed"


def run_id(started: datetime) -> str:
    """ratrixcam_<YYYYMMDD>_<HHMMSS>_<8 random hex>, the same shape as videoproc's run ids."""
    return f"ratrixcam_{started:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def run_metadata_dir(save_path: str | Path, run_id_: str) -> Path:
    return Path(save_path) / RUN_METADATA_DIRNAME / run_id_


def terminal_log_filename(run_id_: str) -> str:
    return f"terminal_{run_id_}.log"


def still_path(stills_path: str, camera_name: str) -> str:
    return os.path.join(stills_path, f"cam_{camera_name}_status.png")


# optional site-specific parser for filename shapes the standard rule does not cover; loaded
# from this exact file and nowhere else (see custom_filename_parser_example.py)
CUSTOM_PARSER_PATH = Path(__file__).resolve().parent / "custom_filename_parser.py"


def _load_custom_parser():
    if not CUSTOM_PARSER_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("custom_filename_parser", CUSTOM_PARSER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


custom_filename_parser = _load_custom_parser()


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
