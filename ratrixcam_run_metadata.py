"""Automatic record-keeping for a ratrixcam recording run (GitHub #34): a verbatim terminal
log, a copy of the camera config, and a shutdown step that gathers the run's loose artifacts
into one folder on the output drive. Nothing here touches camera capture or process control;
ratrixcam_IO.py calls into it at launch, at Start Recording, and after the cameras have stopped.
"""

import csv
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import IO, cast

from ratrixcam_manifest import write_row
from ratrixcam_status import StatusWriter
from ratrixcam_naming import (
    MANIFEST_SUFFIX,
    NOTES_FILENAME,
    NOTES_TEMPLATE,
    RUN_METADATA_DIRNAME,
    STILLS_DIRNAME,
    VIDEO_EXT,
    code_version,
    parse_manifest_warning,
    run_id,
    run_metadata_dir,
    shorten_path,
    temp_manifests_dir,
    temp_run_metadata_dir,
    terminal_log_filename,
)
from ratrixcam_utils import Config, ensure_dir_exists, flush_to_device, not_writable, stills_dir

LOG_START_ATTEMPTS = 3
LOG_START_RETRY_DELAY = 0.5  # seconds
END_PAUSE_INTERVALS = 2.5  # preview intervals the shutdown waits after the final status file, so the viewer can load it
RE_REQUEST_RETRY_WINDOW = 15  # seconds a re-requested transfer keeps retrying at cleanup: long enough for a
                              # glitch, not for a drive swap; save_drive_problem has just passed when the
                              # cleanup starts, so what fails here is a glitch or permanent
STOP_SPACE_FACTOR = 11  # free space a stop requires beyond everything in temp: the largest clip there, this
                        # many times over, so that the clips still open at the stop click fit as well


def save_drive_problem(config: Config) -> str | None:
    """Why the save drive cannot take the shutdown cleanup right now, or None if it can: the
    save folder is missing, refuses a write (the same probe as at launch), or has less free
    space than everything in temp plus STOP_SPACE_FACTOR times the largest clip there. Run
    at the stop click and on Ctrl-C, before the cameras are stopped; the message is shown to
    the operator and says what the drive needs.

    KNOWN: this overlaps the launch check in ratrixcam_IO and _no_room_for below. Kept as
    three probes on purpose: each runs at its own moment and reports in its own words."""
    if not os.path.isdir(config.save_path):
        return "SSD drive not reachable"
    reason = not_writable(config.save_path)
    if reason is not None:
        return f"SSD drive not writable ({reason})"
    try:
        free = shutil.disk_usage(config.save_path).free
    except OSError as e:
        return f"SSD drive not reachable ({e.strerror or e})"
    in_temp, largest_clip = _temp_usage(config.temp_path)
    needed = in_temp + STOP_SPACE_FACTOR * largest_clip
    if free < needed:
        return f"SSD drive full: {_size_text(needed)} needed for the backlog now in temp, {_size_text(free)} free"
    return None


def _size_text(nbytes: float) -> str:
    """'12.3 GB' from a gigabyte up, '456 MB' below."""
    return f"{nbytes / 1e9:.1f} GB" if nbytes >= 1e9 else f"{nbytes / 1e6:.0f} MB"


def _temp_usage(temp_path: str) -> tuple[int, int]:
    """(bytes of everything under temp, size of the largest clip there)."""
    total = largest = 0
    for folder, _dirs, files in os.walk(temp_path):
        for name in files:
            try:
                size = os.path.getsize(os.path.join(folder, name))
            except OSError:
                continue
            total += size
            if name.endswith(VIDEO_EXT):
                largest = max(largest, size)
    return total, largest


def computer_identity() -> str:
    """'<host name> <MAC address>' of this computer, for the run records. The MAC is that of
    the built-in Ethernet interface en0 as reported by ifconfig; unknown if it cannot be read."""
    mac = "MAC unknown"
    try:
        for line in subprocess.run(["ifconfig", "en0"], capture_output=True, text=True, check=True).stdout.splitlines():
            if line.strip().startswith("ether "):
                mac = line.split()[1]
                break
    except (OSError, subprocess.CalledProcessError):
        pass
    return f"{platform.node()} {mac}"


class TerminalLog:
    """Tee of file descriptors 1 and 2 into a log file, so output from every process of the
    run -- this one, the multicam supervisor and each camera server (spawned, so they do not
    share Python's sys.stdout) and native libraries -- reaches both the terminal and the file."""

    def __init__(self, path: Path):
        self.path: Path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        open(path, "a").close()  # fail here, before touching the file descriptors, if the log cannot be written
        _ = sys.stdout.flush()
        _ = sys.stderr.flush()
        self._saved: tuple[int, int] = (os.dup(1), os.dup(2))
        # tee runs in its own session: a Ctrl-C in the terminal goes to the whole foreground
        # process group, and a tee that died with it would take every later line, the whole
        # shutdown report, with it. Detached, it ends when the pipe closes in stop()
        self._tee: subprocess.Popen[bytes] = subprocess.Popen(["tee", "-a", str(path)], stdin=subprocess.PIPE, start_new_session=True)
        assert self._tee.stdin is not None  # stdin=PIPE above
        self._pipe: IO[bytes] = self._tee.stdin
        _ = os.dup2(self._pipe.fileno(), 1)
        _ = os.dup2(self._pipe.fileno(), 2)
        # fd 1 is now a pipe: keep this process line-buffered, and make spawned children
        # unbuffered, so messages reach the terminal when printed rather than at exit
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(line_buffering=True)
        os.environ["PYTHONUNBUFFERED"] = "1"
        self._active: bool = True

    def stop(self) -> None:
        """Restore the terminal and let tee finish writing. Call after the camera processes
        have exited; any that are still alive keep the pipe open and tee waits for them."""
        if not self._active:
            return
        self._active = False
        _ = sys.stdout.flush()
        _ = sys.stderr.flush()
        _ = os.dup2(self._saved[0], 1)
        _ = os.dup2(self._saved[1], 2)
        for fd in self._saved:
            os.close(fd)
        self._pipe.close()
        try:
            _ = self._tee.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"WARNING: terminal log writer still busy; {self.path} may be incomplete")


class RunMetadata:
    """One ratrixcam launch: its id, terminal log, and metadata folder, born in the temp folder
    and moved whole to the save drive at shutdown."""

    def __init__(self, config: Config, config_path: str | Path, started: datetime | None = None):
        self.config: Config = config
        self.config_path: str | Path = config_path
        self.started: datetime = started or datetime.now()
        self.run_id: str = run_id(self.started)
        self.log_path: Path = Path(config.temp_path) / terminal_log_filename(self.run_id)
        self.temp_run_dir: Path = temp_run_metadata_dir(config.temp_path)  # during the run
        self.run_dir: Path = run_metadata_dir(config.save_path, self.run_id)  # after shutdown
        self.terminal_log: TerminalLog | None = None
        self.status: StatusWriter = StatusWriter(config, self.run_id, self.started)  # the viewer's status file in temp
        self._collected: bool = False

    def start_terminal_log(self) -> bool:
        """Start the tee into the log file; False if the temp folder or the log file cannot be
        created after LOG_START_ATTEMPTS tries. The caller treats False as a hard stop."""
        if not ensure_dir_exists(self.config.temp_path):
            print("ERROR: temp folder unavailable")
            return False
        for attempt in range(LOG_START_ATTEMPTS):
            try:
                self.terminal_log = TerminalLog(self.log_path)
                break
            except OSError as e:
                if attempt == LOG_START_ATTEMPTS - 1:
                    print(f"ERROR: cannot create terminal log {self.log_path}: {e}")
                    return False
                time.sleep(LOG_START_RETRY_DELAY)
        print(f"ratrixcam run {self.run_id} -- code version {code_version()} -- computer {computer_identity()}")
        print(f"Launched as: {sys.executable} {' '.join(sys.argv)}  (working folder {os.getcwd()})")
        # the two roots in full; every path printed after this is shown relative to them
        print(f"Temp folder: {self.config.temp_path}")
        print(f"Save drive: {self.config.save_path}")
        print(f"Terminal output is being copied to {self.short(self.log_path)}")
        print(f"Stills folder: {self.short(stills_dir(self.config.temp_path))}")
        print(f"Run metadata folder: {self.short(self.temp_run_dir)}")
        print(f"\t(moves to {self.short(self.run_dir)} at shutdown)")
        return True

    def short(self, path: str | Path) -> str:
        """A path for the terminal, with the temp folder or save drive replaced by <temp> or
        <save>, as printed in full at the top of the log."""
        return shorten_path(path, [(self.config.temp_path, "<temp>"), (self.config.save_path, "<save>")])

    def record_start(self) -> None:
        """Called when recording starts: lay down the run's metadata folder in temp with the
        config copy and the notes template, then put the config copy alone in the run's
        folder on the save drive at once, so the run has a record there even if this computer
        dies mid-run. The notes file exists only in temp, so there is one place to type in;
        the shutdown collection moves the whole folder over the provisional one."""
        try:
            self.temp_run_dir.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(self.config_path, self.temp_run_dir / os.path.basename(self.config_path))
            notes = self.temp_run_dir / NOTES_FILENAME
            if not notes.exists():
                _ = notes.write_text(NOTES_TEMPLATE)
        except OSError as e:
            print(f"WARNING: could not create the run metadata folder {self.short(self.temp_run_dir)}: {e.strerror or e}")
            return
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(self.config_path, self.run_dir / os.path.basename(self.config_path))
        except OSError as e:
            print(f"WARNING: could not write the provisional config copy to {self.short(self.run_dir)}: {e.strerror or e}")

    def collect_at_shutdown(self, abort: bool = False) -> None:
        """After the cameras have stopped: add to the per-camera manifests every row the
        cameras reported they could not write (read back from this run's terminal log);
        re-request the transfer of every clip still in temp that has a manifest row, and
        delete those without one that are not video (_settle_stranded_clips); gather the
        status stills, every loose file in temp (screenshots, anything the user dropped
        there) and every folder that still holds files into the run's metadata folder;
        remove the folders that are empty; move the metadata folder whole to the save drive
        under the run id, replacing the provisional copy made at the start; and report. The
        terminal log is closed and moved into it last, so the report itself is in it. Once
        everything has moved the temp folder is removed entirely.

        The metadata folder is moved only if the gathering completed and the save drive is
        present with room for the whole folder; otherwise it stays in temp with one WARNING
        saying why. With abort=True (the user chose Abort) only the manifest rows are added:
        nothing is transferred, gathered or moved, temp stays exactly as the cameras left it
        plus the closed terminal log, and the next launch refuses to start until temp is
        renamed. A later cleanup tool takes it from there and must not add the rows again."""
        if self._collected:
            return
        self._collected = True
        temp = Path(self.config.temp_path)
        moved = False
        left = 0
        if abort:
            print(f"Run aborted: the cleanup moves nothing; {self.short(temp)} is left as the cameras left it")
            print("Partial copies (.part) of clips may remain on the save drive; a later cleanup replaces them")
            self._add_unwritten_manifest_rows()
        else:
            print(f"Collecting the run metadata as {RUN_METADATA_DIRNAME}/{self.run_id}")
            self._add_unwritten_manifest_rows()
            _, left = self._settle_stranded_clips()
            if self._gather_into_run_folder():
                moved = self._move_run_folder_to_save()
            else:
                print(f"WARNING: run metadata folder not moved because the gathering failed; left in {self.short(self.temp_run_dir)}")
        if self.terminal_log is not None:
            if moved:
                print(f"Closing the terminal log and saving it as {RUN_METADATA_DIRNAME}/{self.run_id}/{self.log_path.name}")
            else:
                print(f"Closing the terminal log; it stays in {self.short(self.log_path)}")
            self.terminal_log.stop()
        if moved and self.log_path.exists():
            try:
                _ = shutil.move(str(self.log_path), str(self.run_dir / self.log_path.name))
            except OSError as e:
                print(f"WARNING: could not move the terminal log into {self.short(self.run_dir)}: {e.strerror or e}")
        # the final status file, for the viewer's run-ended screen: written after everything has
        # moved, left for a few poll intervals so the viewer can load it, then its final state
        # goes into the run folder and it is removed with temp. On an abort (or a failed move)
        # it stays in temp with the terminal log
        self.status.write(cameras_alive=False, state="ended", message=self._end_message(abort, moved, left), force=True)
        time.sleep(END_PAUSE_INTERVALS * self.config.preview_interval)
        self.status.close()
        if moved:
            try:
                _ = shutil.copy2(self.status.path, self.run_dir / self.status.path.name)
            except OSError as e:
                print(f"WARNING: could not copy the status file into {self.short(self.run_dir)}: {e.strerror or e}")
            self.status.remove()
        if moved and temp.is_dir() and _remove_if_empty(temp):
            print("Temp folder removed")

    def _end_message(self, abort: bool, moved: bool, left: int) -> str:
        """The one line the viewer shows when the run has ended: the same facts as the report."""
        if abort:
            return f"Run aborted; everything left in {self.short(self.config.temp_path)}"
        if not moved:
            return f"Run metadata left in {self.short(self.temp_run_dir)}; see the terminal log"
        text = f"Run metadata saved to {self.short(self.run_dir)}; temp folder removed"
        if left:
            text += f"; {left} clip(s) not transferred travel inside it"
        return text

    def _settle_stranded_clips(self) -> tuple[int, int]:
        """Every .mp4 one level below temp, in the per-camera folders, is a clip the cameras did
        not transfer. Three passes, each described on its own method: the clips with a
        manifest row are re-requested, those without one are deleted or kept, and complete
        partial copies on the drive are renamed. A failure to list temp is reported and the
        shutdown goes on to the terminal log. Returns (transferred, not transferred).

        KNOWN, PENDING (design document item 6): on the 600-second kill path a camera's own
        transfer process may still be alive and copying the same clip while this runs. On a
        normal stop there is no overlap: a camera waits for its transfers before exiting."""
        try:
            rows, all_read = self._manifest_rows()
            temp = Path(self.config.temp_path)
            skip = (self.temp_run_dir, Path(stills_dir(self.config.temp_path)))
            clips = sorted(
                clip for folder in temp.iterdir() if folder.is_dir() and folder not in skip for clip in folder.glob(f"*{VIDEO_EXT}")
            )
            listed = [clip for clip in clips if clip.name in rows]
            transferred = self._re_request(listed, rows)
            deleted, kept = self._triage_unlisted([clip for clip in clips if clip.name not in rows], all_read)
            print(
                f"Clips found in temp: {len(clips)}; {transferred} transferred, {len(listed) - transferred} not transferred "
                + f"(they travel with their folder into the run metadata folder); without a manifest row: {deleted} deleted, {kept} kept"
            )
            self._rename_complete_partial_copies(rows, {clip.name for clip in clips})
            return transferred, len(listed) - transferred
        except OSError as e:
            print(f"WARNING: could not go through the clips left in temp: {_error_text(e)}")
            return 0, 0

    def _re_request(self, listed: list[Path], rows: dict[str, str]) -> int:
        """Re-request the transfer of each listed clip, one at a time, through the same function
        the cameras use, to the destination its manifest row names; a stray partial copy there
        is overwritten. Before each one, the two checks that function makes first (save folder
        present, free space at least TRANSFER_SPACE_FACTOR times the clip) are made here: when
        one fails the rest are not re-requested, with one line saying why, so a drive that
        vanishes or fills during the pass does not cost a retry window per clip. Returns how
        many arrived; the ones that did not were reported by the transfer function."""
        if not listed:
            return 0
        from ratrixcam_server import TRANSFER_SPACE_FACTOR, move_file  # cv2-heavy module; imported where used

        transferred = 0
        for i, clip in enumerate(listed):
            reason = None
            if not os.path.isdir(self.config.save_path):
                reason = "save folder not found"
            else:
                try:
                    if shutil.disk_usage(self.config.save_path).free < clip.stat().st_size * TRANSFER_SPACE_FACTOR:
                        reason = "destination short of space"
                except OSError as e:
                    reason = f"destination unavailable: {e.strerror or e}"
            if reason is not None:
                print(f"WARNING: {len(listed) - i} clip(s) not re-requested: {reason}")
                break
            print(f"Re-requesting the transfer of {clip.name}")
            if move_file(
                str(clip), os.path.join(self.config.save_path, rows[clip.name]), self.config.save_path, pause=0, retry_window=RE_REQUEST_RETRY_WINDOW
            ):
                transferred += 1
                print(f"{clip.name} transferred")
        return transferred

    def _triage_unlisted(self, unlisted: list[Path], all_read: bool) -> tuple[int, int]:
        """A clip with no manifest row was never closed by its camera (a kill) or is a stranger:
        if it cannot be opened as video it is reported and deleted; if it can, it is kept, so
        that it reaches the metadata folder with its folder and is seen. Nothing is deleted
        when a manifest could not be read (all_read False): that camera's clips would all look
        unlisted. Returns (deleted, kept)."""
        deleted = kept = 0
        for clip in unlisted:
            if _opens_as_video(clip):
                kept += 1
                print(f"WARNING: {clip.name} has no manifest row but opens as video; kept")
            elif not all_read:
                kept += 1
                print(f"WARNING: {clip.name} has no manifest row and cannot be opened as video; kept because a manifest could not be read")
            else:
                try:
                    clip.unlink()
                    deleted += 1
                    print(f"WARNING: {clip.name} has no manifest row and cannot be opened as video; deleted")
                except OSError as e:
                    kept += 1
                    print(f"WARNING: {clip.name} has no manifest row and cannot be opened as video; could not be deleted ({e.strerror or e})")
        return deleted, kept

    def _rename_complete_partial_copies(self, rows: dict[str, str], in_temp: set[str]) -> None:
        """A listed clip that is neither on the drive nor in temp but has a partial copy on the
        drive may be that copy and nothing else: the drive was pulled between the data flush
        and the rename. It takes the clip's name if it opens as video (the manifest carries no
        size, so that is the completeness test) and the rename is flushed to the drive;
        otherwise it is reported and left alone. Nothing on the drive is deleted."""
        if not os.path.isdir(self.config.save_path):
            return
        from ratrixcam_server import PARTIAL_SUFFIX  # cv2-heavy module; imported where used

        for name, rel in rows.items():
            out = os.path.join(self.config.save_path, rel)
            partial = out + PARTIAL_SUFFIX
            if name in in_temp or os.path.exists(out) or not os.path.exists(partial):
                continue
            if not _opens_as_video(Path(partial)):
                print(f"WARNING: {self.short(partial)} is a partial copy that cannot be opened as video; left in place")
                continue
            try:
                os.replace(partial, out)
            except OSError as e:
                print(f"WARNING: {self.short(partial)} is a complete partial copy that could not be renamed ({e.strerror or e}); left in place")
                continue
            try:
                flush_to_device(os.path.dirname(out))  # the rename itself; best effort
            except OSError:
                pass
            print(f"{name} transferred (its complete partial copy on the drive was renamed)")

    def _manifest_rows(self) -> tuple[dict[str, str], bool]:
        """Clip filename -> its path relative to the save drive, from every manifest of this
        run, and whether every manifest could be read."""
        rows: dict[str, str] = {}
        all_read = True
        folder = temp_manifests_dir(self.config.temp_path)
        for manifest in sorted(folder.glob(f"*{MANIFEST_SUFFIX}")) if folder.is_dir() else []:
            try:
                with open(manifest, newline="") as f:
                    for row in csv.DictReader(f):
                        rows[os.path.basename(row["path"])] = row["path"]
            except (OSError, KeyError) as e:
                all_read = False
                print(f"WARNING: could not read {self.short(manifest)}: {e}")
        return rows, all_read

    def _gather_into_run_folder(self) -> bool:
        """Move the status stills, every loose top-level temp file and every non-empty folder
        (a per-camera folder with clips that could not be transferred, or anything the user
        left) into the metadata folder, and remove the folders that are empty. Returns whether
        the gathering completed; a failure is reported here with the OS message and leaves
        what was already gathered in place."""
        gathered = 0
        try:
            self.temp_run_dir.mkdir(parents=True, exist_ok=True)
            stills_src = Path(stills_dir(self.config.temp_path))
            if stills_src.is_dir():
                stills_dst = self.temp_run_dir / STILLS_DIRNAME
                stills_dst.mkdir(exist_ok=True)
                for f in sorted(stills_src.iterdir()):
                    if f.is_file() and f.name != ".DS_Store":
                        _ = shutil.move(str(f), str(stills_dst / f.name))
                        gathered += 1
                _ = _remove_if_empty(stills_src)
            for entry in sorted(Path(self.config.temp_path).iterdir()):
                if entry == self.log_path or entry == self.temp_run_dir or entry.name.startswith(self.status.path.name):
                    continue  # the log is closed and moved last; the run folder is the destination; the status file is written until the end
                if entry.is_file():
                    if entry.name != ".DS_Store":
                        target = _free_name(self.temp_run_dir, entry.name)
                        if target.name != entry.name:
                            print(f"{entry.name} moved into the run metadata folder as {target.name}: a file of that name was already there")
                        _ = shutil.move(str(entry), str(target))
                        gathered += 1
                elif entry.is_dir() and not _remove_if_empty(entry):
                    # BY DESIGN (design document item 4): a camera folder still holding clips
                    # travels into the metadata folder and from there onto the drive, where
                    # videoproc does not look for clips; the WARNING is their only trace. The
                    # room check before the move is one times the folder's size; the stop-time
                    # check demands room for all of temp plus a margin before the cleanup starts.
                    count = sum(1 for f in entry.rglob("*") if f.is_file())
                    target = _free_name(self.temp_run_dir, entry.name)
                    _ = shutil.move(str(entry), str(target))
                    print(f"WARNING: folder {entry.name} with {count} file(s) moved into the run metadata folder")
        except OSError as e:
            print(f"WARNING: gathering stills and loose temp files into the run metadata folder failed after {gathered} file(s): {_error_text(e)}")
            return False
        print(f"Gathered into the run metadata folder: {gathered} file(s) (stills and loose temp files)")
        return True

    def _move_run_folder_to_save(self) -> bool:
        """Move the metadata folder to the save drive if the drive is present with room for all
        of it; True if it arrived complete. Otherwise one WARNING with the reason, and the
        folder stays in temp. Nothing is copied when the room is not there, so a drive that
        is already full is not filled further by a copy that cannot complete."""
        reason = self._no_room_for(self.temp_run_dir)
        if reason is None:
            try:
                if _move_tree(self.temp_run_dir, self.run_dir):
                    print(f"Run metadata folder moved to {self.short(self.run_dir)}")
                    return True
                reason = "a copied file is missing or differs in size"
            except OSError as e:
                reason = f"copy failed: {_error_text(e)}"
        print(f"WARNING: run metadata folder not moved to {self.short(self.run_dir)}: {reason}; left in {self.short(self.temp_run_dir)}")
        return False

    def _no_room_for(self, folder: Path) -> str | None:
        """Why the save drive cannot take a copy of folder right now, or None if it can: the
        save folder is gone, cannot be queried, or has less free space than the folder's files
        add up to."""
        if not os.path.isdir(self.config.save_path):
            return "save folder not found"
        try:
            free = shutil.disk_usage(self.config.save_path).free
        except OSError as e:
            return f"save drive unavailable: {e.strerror or e}"
        needed = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        if free < needed:
            return f"save drive short of space ({_size_text(needed)} needed, {_size_text(free)} free)"
        return None


    def _add_unwritten_manifest_rows(self) -> None:
        """Every manifest-warning line in this run's terminal log becomes a row in that
        camera's manifest in the temp folder (created with its header if the camera never
        managed to write one), with row_written = now."""
        if not self.log_path.exists():
            print("WARNING: no terminal log for this run; manifest rows reported as unwritten cannot be added")
            return
        added, failed = 0, 0
        with open(self.log_path, errors="replace") as log:
            for line in log:
                fields = parse_manifest_warning(line)
                if fields is None:
                    continue
                manifest_path = str(temp_manifests_dir(self.config.temp_path) / fields["manifest"])
                try:
                    write_row(manifest_path, fields["path"], fields["subjID"], fields["view"], fields["clip_start"], fields["crf"])
                    added += 1
                except OSError as e:
                    failed += 1
                    print(f"WARNING: could not add {fields['path']} to {self.short(manifest_path)} at cleanup: {e.strerror or e}")
        print(f"Manifest rows added at cleanup: {added}" + (f" ({failed} could not be added)" if failed else ""))


def _opens_as_video(path: Path) -> bool:
    """Whether OpenCV can open the file and read a frame from it. A clip whose writer was
    never released (a camera killed mid-clip) has no index and fails here."""
    import cv2  # heavy; imported where used

    capture = cv2.VideoCapture(str(path))
    try:
        return capture.isOpened() and capture.read()[0]
    finally:
        capture.release()


def _error_text(e: OSError) -> str:
    """The message of an OS error without paths or error numbers. shutil.Error, raised by
    copytree, carries a list of (source, destination, why) tuples, one per file that failed,
    each why being a full error string with both paths: reported as the first why alone and
    the count."""
    if isinstance(e, shutil.Error) and e.args and isinstance(e.args[0], list):
        failures = cast(list[tuple[str, str, str]], e.args[0])
        if failures and len(failures[0]) == 3:
            why = re.sub(r"^\[Errno \d+\] ", "", str(failures[0][2])).split(": '")[0]
            return f"{why} ({len(failures)} file(s) could not be copied)"
    return e.strerror or str(e)


def _move_tree(src: Path, dst: Path) -> bool:
    """Copy a folder tree onto dst (files already there are replaced), verify that every file
    arrived with its size, flush every copied file and folder onto the drive, and only then
    remove the source. False, with the source kept, if anything is missing or differs in size
    after the copy. The flush matters because the tree may carry clips whose transfers failed
    and which exist nowhere else: a drive pulled right after the move must still hold them."""
    _ = shutil.copytree(src, dst, dirs_exist_ok=True)
    copied: list[Path] = []
    for f in src.rglob("*"):
        if f.is_file():
            target = dst / f.relative_to(src)
            if not target.is_file() or target.stat().st_size != f.stat().st_size:
                return False
            copied.append(target)
    for target in copied:
        flush_to_device(str(target))
    for folder in {target.parent for target in copied} | {dst}:
        flush_to_device(str(folder))
    shutil.rmtree(src)
    return True


def _free_name(folder: Path, name: str) -> Path:
    """folder/name if nothing is there yet, else the name with _from_temp (then _from_temp3,
    _from_temp4, ...) before the extension, so that nothing already in the folder is written
    over by a stray file or folder gathered from temp."""
    target = folder / name
    stem, ext = os.path.splitext(name)
    n = 1
    while target.exists():
        n += 1
        target = folder / f"{stem}_from_temp{n if n > 2 else ''}{ext}"
    return target


def _remove_if_empty(folder: Path) -> bool:
    """Remove a folder that holds nothing but a .DS_Store; True if it is gone."""
    for f in folder.iterdir():
        if f.name == ".DS_Store":
            f.unlink()
        else:
            return False
    folder.rmdir()
    return True
