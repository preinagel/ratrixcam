"""Per-clip manifest written by each camera server as it records: one CSV per camera in the
manifests/ subfolder of the run's metadata folder in temp, one row per closed clip, appended
before the clip's transfer to the save drive starts. videoproc takes its list of clips from
these files. A row that could not be written is announced in the terminal log
(ratrixcam_naming.manifest_warning) and added by the shutdown cleanup, which then moves the
whole metadata folder to the save drive (ratrixcam_run_metadata).
"""

import csv
import os
import time
from datetime import datetime

from ratrixcam_naming import manifest_filename, manifest_warning, temp_manifests_dir

MANIFEST_COLUMNS = ["path", "subjID", "view", "clip_start", "row_written", "crf"]
TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"  # trimmed to milliseconds by format_time
ATTEMPTS = 3
RETRY_DELAY = 0.5  # seconds between attempts


def format_time(t: datetime) -> str:
    return t.strftime(TIME_FORMAT)[:-3]


def write_row(manifest_path: str, path: str, subjID: str, view: str, clip_start: str, crf: int | str) -> None:
    """One attempt to append a row, with row_written = now; the folder and the header are
    created with the file. Raises OSError on failure."""
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    new = not os.path.exists(manifest_path) or os.path.getsize(manifest_path) == 0
    with open(manifest_path, "a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(MANIFEST_COLUMNS)
        writer.writerow([path, subjID, view, clip_start, format_time(datetime.now()), crf])
        f.flush()
        os.fsync(f.fileno())


class ManifestMetadata:
    """What one camera writes into every row of its manifest, and where that manifest is."""

    def __init__(self, temp_path: str, camera_name_: str, subjID: str, view: str, crf: int):
        self.path = str(temp_manifests_dir(temp_path) / manifest_filename(camera_name_))
        self.subjID = subjID
        self.view = view
        self.crf = crf

    def append(self, day_folder: str, file_name: str, started: datetime) -> bool:
        """Append the row for one closed clip. Up to ATTEMPTS tries RETRY_DELAY apart; after
        the last failure prints a WARNING with the error, then the tagged line the shutdown
        cleanup reads, and returns False. Never raises: recording must not stop over a
        manifest write."""
        path, clip_start = f"{day_folder}/{file_name}", format_time(started)
        error: OSError | None = None
        for attempt in range(ATTEMPTS):
            try:
                write_row(self.path, path, self.subjID, self.view, clip_start, self.crf)
                return True
            except OSError as e:
                error = e
                if attempt < ATTEMPTS - 1:
                    time.sleep(RETRY_DELAY)
        print(f"WARNING: could not add {file_name} to the manifest {self.path} after {ATTEMPTS} attempts: {error}")
        print(manifest_warning(os.path.basename(self.path), path, self.subjID, self.view, clip_start, self.crf))
        return False
