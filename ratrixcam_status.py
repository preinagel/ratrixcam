"""The status file the viewer reads: <temp_path>/ratrixcam_status.json, one per run, the
contract between this program and gui/viewer.js. Every statement the viewer makes about the run
("save drive full", "clips waiting", "stopping", "ended") comes from a field written here;
the viewer applies only soft thresholds to the numbers. Written through a temporary name
and a rename, so a reader never sees a half-written file.
"""

import json
import os
import platform
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from ratrixcam_manifest import format_time
from ratrixcam_naming import STATUS_FILENAME, VIDEO_EXT, temp_run_metadata_dir
from ratrixcam_utils import Config, not_writable, seconds_until_full, stills_dir, write_rate

SCHEMA = 1
PROBE_INTERVAL = 60  # seconds between write probes of the save drive: a probe creates and removes a file there


class StatusWriter:
    """Writes the status file for one run. write() is called from the launcher's poll loop
    while recording, from the shutdown wait loops, and at the milestones (state changes);
    it writes at most once per preview_interval unless forced, since every write counts
    the clips in temp. The save drive's transitions (gone, no longer accepting clips, back)
    are printed once each as they happen. close() ends all writing, so a poll still
    ticking cannot recreate the file after the cleanup has removed it."""

    def __init__(self, config: Config, run_id: str, started: datetime):
        self.config: Config = config
        self.run_id: str = run_id
        self.started: datetime = started
        self.path: Path = Path(config.temp_path) / STATUS_FILENAME
        self.rate: float = write_rate(len(config.cameras))
        self.state: str = "running"
        self.message: str = ""
        self._last_write: float = 0.0
        self._closed: bool = False
        self._lock: threading.Lock = threading.Lock()
        self._save_found: bool = True  # launch checked the drive, so a run starts healthy
        self._save_accepting: bool = True
        self._write_failed: bool = False
        self._probe_time: float = float("-inf")
        self._probe_reason: str | None = None

    def write(self, cameras_alive: bool, state: str | None = None, message: str | None = None, force: bool = False) -> None:
        """Write the file if preview_interval has passed since the last write, or at once
        when forced (a milestone). A state or message given here is kept for later writes."""
        with self._lock:
            if self._closed:
                return
            if state is not None:
                self.state = state
            if message is not None:
                self.message = message
            now = time.monotonic()
            if not force and now - self._last_write < self.config.preview_interval:
                return
            self._last_write = now
            status = self.snapshot(cameras_alive)
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                with open(tmp, "w") as f:
                    json.dump(status, f, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                if not self._write_failed:
                    print(f"WARNING: could not write the status file {self.path}: {e.strerror or e}")
                self._write_failed = True
                return
            if self._write_failed:
                print(f"Status file written again: {self.path}")
            self._write_failed = False

    def snapshot(self, cameras_alive: bool) -> dict[str, object]:
        """The file's content now, every field present."""
        save = self._drive(self.config.save_path)
        temp = self._drive(self.config.temp_path)
        clips, largest = self._clips_in_temp()
        if save["found"]:
            reason = self._probe()
            free = save["free_bytes"]
            clip = largest if largest > 0 else self.rate * self.config.time_slice
            accepting = reason is None and isinstance(free, int) and free >= clip * self._space_factor()
        else:
            accepting = False
        save["accepting_clips"] = accepting
        # beyond the one clip each camera has open while it runs
        temp["clips_waiting"] = max(0, clips - len(self.config.cameras)) if cameras_alive else clips
        self._report_transitions(bool(save["found"]), accepting)
        return {
            "schema": SCHEMA,
            "written": format_time(datetime.now()),
            "recording": True,  # false only with the hard stop for a nearly full temp drive (#41)
            "run": {
                "run_id": self.run_id,
                "started": format_time(self.started),
                "state": self.state,
                "message": self.message,
                "machine_name": self.config.machine_name or platform.node(),
            },
            "save_drive": save,
            "temp_drive": temp,
        }

    def _probe(self) -> str | None:
        """The write probe's last result, refreshed at most every PROBE_INTERVAL seconds, or at
        once after the drive was found missing: a probe is a file created and removed on the
        save drive, not something to do every second for a week."""
        now = time.monotonic()
        if now - self._probe_time >= PROBE_INTERVAL or not self._save_found:
            self._probe_reason = not_writable(self.config.save_path)
            self._probe_time = now
        return self._probe_reason

    def _drive(self, path: str) -> dict[str, object]:
        try:
            if not os.path.isdir(path):
                raise FileNotFoundError(path)
            usage = shutil.disk_usage(path)
        except OSError:
            return {"path": path, "found": False, "total_bytes": None, "used_bytes": None, "free_bytes": None, "seconds_until_full": None}
        return {
            "path": path,
            "found": True,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "seconds_until_full": round(seconds_until_full(usage.free, self.rate)),
        }

    def _clips_in_temp(self) -> tuple[int, int]:
        """(number of clips in the per-camera folders, size of the largest)."""
        count = largest = 0
        temp = Path(self.config.temp_path)
        skip = (temp_run_metadata_dir(self.config.temp_path), Path(stills_dir(self.config.temp_path)))
        try:
            for folder in temp.iterdir():
                if not folder.is_dir() or folder in skip:
                    continue
                for clip in folder.glob(f"*{VIDEO_EXT}"):
                    try:
                        size = clip.stat().st_size
                    except OSError:
                        continue
                    count += 1
                    largest = max(largest, size)
        except OSError:
            pass
        return count, largest

    @staticmethod
    def _space_factor() -> int:
        from ratrixcam_server import TRANSFER_SPACE_FACTOR  # cv2-heavy module; imported where used

        return TRANSFER_SPACE_FACTOR

    def _report_transitions(self, found: bool, accepting: bool) -> None:
        """One terminal line when the save drive first goes missing, first stops accepting
        clips, and when it is back; nothing while a state persists."""
        if found != self._save_found:
            print(f"WARNING: save drive not found: {self.config.save_path}" if not found else f"Save drive back: {self.config.save_path}")
            if found:
                self._save_accepting = True  # a drive that is back starts as accepting: a read-only one then reports itself
        if found and accepting != self._save_accepting:  # a missing drive's "not accepting" goes without saying
            print("WARNING: save drive no longer accepting clips (not writable or short of space)" if not accepting else "Save drive accepting clips again")
        self._save_found, self._save_accepting = found, accepting

    def close(self) -> None:
        """No further writes, whoever calls."""
        with self._lock:
            self._closed = True

    def remove(self) -> None:
        """Delete the file, after close(), and a temporary name a failed write may have left."""
        for path in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"WARNING: could not remove the status file {path}: {e.strerror or e}")
