"""Per-clip manifest written by each camera server as it records: one CSV per camera in the
temp folder, one row per closed clip, appended before the clip's transfer to the save drive
starts. videoproc takes its list of clips from these files. At shutdown the run's manifests
are moved to <save_path>/manifests_<run_id>/ by ratrixcam_run_metadata.
"""

import csv
import os
import time
from datetime import datetime

from ratrixcam_naming import manifest_filename

MANIFEST_COLUMNS = ["path", "subjID", "view", "clip_start", "row_written", "crf"]
TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"  # trimmed to milliseconds by format_time
ATTEMPTS = 3
RETRY_DELAY = 0.5  # seconds between attempts


def format_time(t: datetime) -> str:
    return t.strftime(TIME_FORMAT)[:-3]


class ManifestMetadata:
    """What one camera writes into every row of its manifest, and where that manifest is."""

    def __init__(self, temp_path: str, camera_name_: str, subjID: str, view: str, crf: int):
        self.path = os.path.join(temp_path, manifest_filename(camera_name_))
        self.subjID = subjID
        self.view = view
        self.crf = crf

    def append(self, day_folder: str, file_name: str, started: datetime) -> bool:
        """Append the row for one closed clip; the header is written when the file is created.
        Up to ATTEMPTS tries RETRY_DELAY apart; after the last failure prints a WARNING and
        returns False. Never raises: recording must not stop over a manifest write."""
        error: OSError | None = None
        for attempt in range(ATTEMPTS):
            try:
                new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
                with open(self.path, "a", newline="") as f:
                    writer = csv.writer(f)
                    if new:
                        writer.writerow(MANIFEST_COLUMNS)
                    writer.writerow(
                        [
                            f"{day_folder}/{file_name}",
                            self.subjID,
                            self.view,
                            format_time(started),
                            format_time(datetime.now()),
                            self.crf,
                        ]
                    )
                    f.flush()
                    os.fsync(f.fileno())
                return True
            except OSError as e:
                error = e
                if attempt < ATTEMPTS - 1:
                    time.sleep(RETRY_DELAY)
        print(f"WARNING: could not add {file_name} to the manifest {self.path} after {ATTEMPTS} attempts: {error}")
        return False
