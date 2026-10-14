"""Names and paths shared by ratrixcam and videoproc, plus the code-version stamp both write
into their records. Start of the shared naming module (GitHub #26); the video filename
builders/parsers still live in videoproc/util.py and move here next.

Folder layout for a recording run's metadata (GitHub #34):

    <save_path>/ratrixcam_run_metadata/<run_id>/
        camera_config.json          copy of the config in use, taken at Start Recording
        terminal_<run_id>.log       verbatim terminal output of the whole run
        notes.txt                   template for the human's notes
        stills/                     the per-camera status stills as they were at shutdown
        <anything else>             other files found in the temp folder at shutdown
"""

import subprocess
import uuid
from datetime import datetime
from pathlib import Path

RUN_METADATA_DIRNAME = "ratrixcam_run_metadata"
CAMERA_CONFIG_FILENAME = "camera_config.json"
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
