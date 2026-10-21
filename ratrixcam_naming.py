"""Names and paths shared by ratrixcam and videoproc, plus the code-version stamp both write
into their records (GitHub #26). Every string built from the subject prefix, subject ID and
view is built here and nowhere else; nothing here parses names.

Folder layout for a recording run's metadata (GitHub #34):

    <save_path>/ratrixcam_run_metadata/<run_id>/
        <config filename>           copy of the config file given with -c, same name, taken at Start Recording
        terminal_<run_id>.log       verbatim terminal output of the whole run
        notes.txt                   template for the human's notes
        stills/                     the per-camera status stills as they were at shutdown
        <anything else>             other files found in the temp folder at shutdown

    <save_path>/manifests_<run_id>/
        <camera name>_manifest.csv  one row per clip the camera closed during the run
"""

import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

RUN_METADATA_DIRNAME = "ratrixcam_run_metadata"
NOTES_FILENAME = "notes.txt"
STILLS_DIRNAME = "stills"
NOTES_TEMPLATE = "Notes for this recording run (free text; edit by hand).\n"
MANIFESTS_DIRNAME_PREFIX = "manifests_"
MANIFEST_SUFFIX = "_manifest.csv"


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


def manifests_dir(save_path: str | Path, run_id_: str) -> Path:
    """Where a run's per-camera manifests are collected at shutdown."""
    return Path(save_path) / f"{MANIFESTS_DIRNAME_PREFIX}{run_id_}"


def manifest_filename(camera_name_: str) -> str:
    return f"{camera_name_}{MANIFEST_SUFFIX}"


def still_path(stills_path: str, camera_name: str) -> str:
    return os.path.join(stills_path, f"cam_{camera_name}_status.png")


VIDEO_EXT = ".mp4"


def camera_name(subject_prefix: str, subjID: str, view: str) -> str:
    """The camera's name as it appears in folder names, file names and frame overlays:
    <subject_prefix><subjID>_<view>, e.g. rat556_buddy."""
    return f"{subject_prefix}{subjID}_{view}"


def clip_filename(camera_name_: str, started: datetime) -> str:
    """<camera name>_<YYYYMMDD>_<HH-MM-SS>.mp4 for a clip that started at `started`."""
    return f"{camera_name_}_{started:%Y%m%d_%H-%M-%S}{VIDEO_EXT}"


def build_output_path(
    output_root: Path, output_studyname: str, subj_ID: str, view: str, recording_date: str, recording_time: str
) -> Path:
    """Standard final-destination path for a compressed clip."""
    new_filename = f"{subj_ID}_{view}_{recording_date}_{recording_time}.mp4"
    return output_root / subj_ID / f"{output_studyname}_{subj_ID}_{view}_{recording_date}" / new_filename
