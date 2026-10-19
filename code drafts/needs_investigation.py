"""
Shared helper for appending to NEEDS_INVESTIGATION.txt, a plain human-readable master list
kept at the top of the videos root (not inside any per-project folder) so any machine/tool
working on this backlog can append to it and any human can scan it. Not machine-parsed by
anything else; the format is for people, not code. Entries are de-duplicated per folder --
re-running an automated check won't spam the file with repeat mentions of the same folder.
"""

from datetime import datetime
from pathlib import Path

NEEDS_INVESTIGATION_FILENAME = "NEEDS_INVESTIGATION.txt"


def append_entry(videos_root: Path, folder: Path, flagged_by: str, reason: str) -> bool:
    """Append an entry unless this folder is already mentioned somewhere in the file.
    Returns True if an entry was written, False if skipped as a duplicate."""
    path = videos_root / NEEDS_INVESTIGATION_FILENAME
    existing = path.read_text() if path.exists() else ""
    if str(folder) in existing:
        return False

    entry = f"[{datetime.now():%Y-%m-%d %H:%M}] {folder}\n" f"    flagged by: {flagged_by}\n" f"    reason: {reason}\n\n"
    with open(path, "a") as f:
        f.write(entry)
    return True
