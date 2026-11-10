"""The status file the viewer reads: <temp_path>/ratrixcam_status.json, one per run, the
contract between this program and gui/viewer.js. Every statement the viewer makes about the run
("save drive full", "clips waiting", "stopping", "ended") comes from a field written here;
the viewer applies only soft thresholds to the numbers. Written through a temporary name
and a rename, so a reader never sees a half-written file.
"""

import csv
import json
import math
import os
import platform
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from ratrixcam_manifest import format_time
from ratrixcam_naming import STATUS_FILENAME, VIDEO_EXT, camera_name, manifest_filename, temp_manifests_dir, temp_run_metadata_dir
from ratrixcam_utils import Config, not_writable, seconds_until_full, stills_dir, write_rate

SCHEMA = 1
PROBE_INTERVAL = 60  # seconds between write probes of the save drive: a probe creates and removes a file there
CONSERVATIVE_BELOW_S = 24 * 3600  # the projection switches from the average to the conservative rate under this
CONSERVATIVE_PERCENTILE = 90  # the clip-size percentile behind the conservative rate
CLIP_SETTLE_S = 13  # seconds after a clip's start plus its length before it is looked for: the close and the transfer pause


class WriteRateEstimate:
    """The rig's write rate in bytes per second, for the time-until-full figures, from the
    clips the cameras' manifests in temp list (ratrixcam_manifest). Once per clip length the
    manifests are read and every listed clip not yet in the inventory, and old enough to have
    settled, is looked for on the save drive under its manifest path, then in the camera's temp
    folder, where it sits complete before its transfer; a clip found nowhere is not in the
    inventory and counts as not recorded. Until every configured camera has a found clip, the
    rig writes at the assumed rate. From then on two rates are kept per camera: the AVERAGE,
    all its bytes so far over the run time so far, which carries every cause of variation
    (bursty, circadian occupancy, dropouts) and projects best over more than a day; and the
    CONSERVATIVE, its CONSERVATIVE_PERCENTILE clip size over the clip length. Every projection
    uses the rig's average rate while the save drive has more than CONSERVATIVE_BELOW_S left at
    it, and the conservative rate below that or when the save drive cannot be queried.
    Everything here runs in the launcher: the cameras are not involved."""

    def __init__(self, config: Config, started: datetime, now: Callable[[], datetime] = datetime.now):
        self.config: Config = config
        self.started: datetime = started
        self.now: Callable[[], datetime] = now
        self.names: list[str] = [camera_name(config.subject_prefix, c.subjID, c.view) for c in config.cameras]
        self.found: dict[str, dict[str, int]] = {name: {} for name in self.names}  # camera name -> clip path -> size
        self.recorded_until: datetime | None = None  # the end of the newest found clip: the time the inventory covers
        self._next_check: datetime | None = None  # the manifests are checked once per clip length
        self._announced: bool = False
        self._lock: threading.Lock = threading.Lock()

    def measured(self) -> bool:
        """Whether every configured camera has at least one found clip."""
        return all(self.found[name] for name in self.names)

    def average_rate(self) -> float:
        """The rig's bytes per second so far: all found clips over the time from the run's start
        to the end of the newest found clip, so that gaps count against the rate and the clips
        still being written or settling do not."""
        recorded = (self.recorded_until - self.started).total_seconds() if self.recorded_until is not None else 0.0
        return sum(sum(found.values()) for found in self.found.values()) / max(recorded, self.config.time_slice)

    def conservative_rate(self) -> float:
        """The rig's rate if every camera wrote its CONSERVATIVE_PERCENTILE clip size every clip."""
        return sum(percentile(list(found.values()), CONSERVATIVE_PERCENTILE) for found in self.found.values()) / self.config.time_slice

    def rate(self, save_free_bytes: int | None) -> float:
        """The rig's rate for every projection, given the save drive's free space (None when
        it cannot be queried): the assumed rate until the rig is measured; then the average
        rate while the save drive has more than CONSERVATIVE_BELOW_S left at it, else the
        conservative rate."""
        with self._lock:
            self._check_manifests()
            if not self.measured():
                return write_rate(len(self.names))
            average, conservative = self.average_rate(), self.conservative_rate()
            if not self._announced:
                self._announced = True
                print(f"Write rate measured from the run's clips: {average / 1e6:.2f} MB/s for the rig")
            if save_free_bytes is not None and seconds_until_full(save_free_bytes, average) > CONSERVATIVE_BELOW_S:
                return average
            return conservative

    def _check_manifests(self) -> None:
        """Once per clip length: look for every listed clip that is not in the inventory yet
        and started more than a clip length plus CLIP_SETTLE_S ago."""
        now = self.now()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + timedelta(seconds=self.config.time_slice)
        settled_before = now - timedelta(seconds=self.config.time_slice + CLIP_SETTLE_S)
        for name in self.names:
            manifest = temp_manifests_dir(self.config.temp_path) / manifest_filename(name)
            for path, started in self._listed_clips(manifest):
                if path in self.found[name] or started > settled_before:
                    continue
                size = self._size(path, name)
                if size is not None:
                    self.found[name][path] = size
                    ended = started + timedelta(seconds=self.config.time_slice)
                    if self.recorded_until is None or ended > self.recorded_until:
                        self.recorded_until = ended

    @staticmethod
    def _listed_clips(manifest: Path) -> list[tuple[str, datetime]]:
        """(path, clip start) of every row of the manifest, in order; [] if it cannot be read.
        A row whose start is not a date and time is left out."""
        clips: list[tuple[str, datetime]] = []
        try:
            with open(manifest, newline="") as f:
                for row in csv.DictReader(f):
                    path, start = row.get("path"), row.get("clip_start")
                    if not path or not start:
                        continue
                    try:
                        clips.append((path, datetime.fromisoformat(start)))
                    except ValueError:
                        continue
        except (OSError, csv.Error):
            return []
        return clips

    def _size(self, path: str, name: str) -> int | None:
        """The clip's size: on the save drive under its manifest path, else in the camera's
        temp folder; None if it is at neither place."""
        candidates = (
            Path(self.config.save_path) / path,
            Path(self.config.temp_path) / f"{self.config.study_label}_{name}" / Path(path).name,
        )
        for candidate in candidates:
            try:
                return candidate.stat().st_size
            except OSError:
                continue
        return None


def percentile(values: list[int], p: int) -> int:
    """The p-th percentile of values by nearest rank: the smallest value at or above which p
    percent of them lie; 0 for no values."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


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
        self.write_rate: WriteRateEstimate = WriteRateEstimate(config, started)
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

    @property
    def rate(self) -> float:
        """The rig's write rate now, for every projection: measured from the run's clips once
        every camera has one, conservative once the save drive has less than a day left
        (WriteRateEstimate)."""
        try:
            save_free = shutil.disk_usage(self.config.save_path).free
        except OSError:
            save_free = None
        return self.write_rate.rate(save_free)

    def snapshot(self, cameras_alive: bool) -> dict[str, object]:
        """The file's content now, every field present."""
        rate = self.rate
        save = self._drive(self.config.save_path, rate)
        temp = self._drive(self.config.temp_path, rate)
        clips, largest = self._clips_in_temp()
        if save["found"]:
            reason = self._probe()
            free = save["free_bytes"]
            clip = largest if largest > 0 else rate * self.config.time_slice
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

    def _drive(self, path: str, rate: float) -> dict[str, object]:
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
            "seconds_until_full": round(seconds_until_full(usage.free, rate)),
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
