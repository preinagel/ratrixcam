"""Names and paths shared by ratrixcam and videoproc, plus the code-version stamp both write
into their records (GitHub #26). Every string built from the subject prefix, subject ID and
view is built here and nowhere else; nothing here parses names.

Folder layout for a recording run's metadata (GitHub #34). During the run it lives in the temp
folder, without the run id; at shutdown it moves whole to the save drive under the run id:

    <temp_path>/ratrixcam_run_metadata/     ->   <save_path>/ratrixcam_run_metadata/<run_id>/
        <config filename>           copy of the config file given with -c, same name, taken at Start Recording
        notes.txt                   template for the human's notes
        manifests/                  <camera name>_manifest.csv, one row per clip the camera closed
        stills/                     the per-camera status stills as they were at shutdown (from <temp_path>/status/)
        <anything else>             other files found in the temp folder at shutdown
        terminal_<run_id>.log       verbatim terminal output of the whole run; lives in <temp_path> until the end
"""

import csv
import io
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

RUN_METADATA_DIRNAME = "ratrixcam_run_metadata"
NOTES_FILENAME = "notes.txt"
STILLS_DIRNAME = "stills"
NOTES_TEMPLATE = "Notes for this recording run (free text; edit by hand).\n"
MANIFESTS_DIRNAME = "manifests"
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
    """Where a run's per-camera manifests end up: inside the run's metadata folder on the save drive."""
    return run_metadata_dir(save_path, run_id_) / MANIFESTS_DIRNAME


def temp_run_metadata_dir(temp_path: str | Path) -> Path:
    """The run's metadata folder while the run lasts; moved to run_metadata_dir at shutdown."""
    return Path(temp_path) / RUN_METADATA_DIRNAME


def temp_manifests_dir(temp_path: str | Path) -> Path:
    """Where the cameras write their manifests during the run."""
    return temp_run_metadata_dir(temp_path) / MANIFESTS_DIRNAME


def manifest_filename(camera_name_: str) -> str:
    return f"{camera_name_}{MANIFEST_SUFFIX}"


MANIFEST_WARNING_TAG = "MANIFEST_ROW_NOT_WRITTEN"
MANIFEST_WARNING_FIELDS = ["manifest", "path", "subjID", "view", "clip_start", "crf"]


def manifest_warning(manifest_filename_: str, path: str, subjID: str, view: str, clip_start: str, crf: int) -> str:
    """The terminal-log line a camera prints when a clip's manifest row could not be written:
    'WARNING <tag> ' followed by the row's fields in CSV form, so the shutdown cleanup can
    add the row from the log (parse_manifest_warning)."""
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([manifest_filename_, path, subjID, view, clip_start, crf])
    return f"WARNING {MANIFEST_WARNING_TAG} {out.getvalue()}"


def parse_manifest_warning(line: str) -> dict[str, str] | None:
    """The fields of a line written by manifest_warning, keyed by MANIFEST_WARNING_FIELDS, or
    None for any other line."""
    marker = f"WARNING {MANIFEST_WARNING_TAG} "
    start = line.find(marker)
    if start < 0:
        return None
    values = next(csv.reader([line[start + len(marker):].rstrip("\r\n")]))
    if len(values) != len(MANIFEST_WARNING_FIELDS):
        return None
    return dict(zip(MANIFEST_WARNING_FIELDS, values))


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

