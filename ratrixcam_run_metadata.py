"""Automatic record-keeping for a ratrixcam recording run (GitHub #34): a verbatim terminal
log, a copy of the camera config, and a shutdown step that gathers the run's loose artifacts
into one folder on the output drive. Nothing here touches camera capture or process control;
ratrixcam_IO.py calls into it at launch, at Start Recording, and after the cameras have stopped.
"""

import io
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from ratrixcam_manifest import write_row
from ratrixcam_naming import (
    NOTES_FILENAME,
    NOTES_TEMPLATE,
    RUN_METADATA_DIRNAME,
    STILLS_DIRNAME,
    code_version,
    parse_manifest_warning,
    run_id,
    run_metadata_dir,
    temp_manifests_dir,
    temp_run_metadata_dir,
    terminal_log_filename,
)
from ratrixcam_utils import Config, ensure_dir_exists, stills_dir

LOG_START_ATTEMPTS = 3
LOG_START_RETRY_DELAY = 0.5  # seconds


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
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        open(path, "a").close()  # fail here, before touching the file descriptors, if the log cannot be written
        sys.stdout.flush()
        sys.stderr.flush()
        self._saved = (os.dup(1), os.dup(2))
        self._tee = subprocess.Popen(["tee", "-a", str(path)], stdin=subprocess.PIPE)
        assert self._tee.stdin is not None  # stdin=PIPE above
        self._pipe = self._tee.stdin
        os.dup2(self._pipe.fileno(), 1)
        os.dup2(self._pipe.fileno(), 2)
        # fd 1 is now a pipe: keep this process line-buffered, and make spawned children
        # unbuffered, so messages reach the terminal when printed rather than at exit
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(line_buffering=True)
        os.environ["PYTHONUNBUFFERED"] = "1"
        self._active = True

    def stop(self) -> None:
        """Restore the terminal and let tee finish writing. Call after the camera processes
        have exited; any that are still alive keep the pipe open and tee waits for them."""
        if not self._active:
            return
        self._active = False
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self._saved[0], 1)
        os.dup2(self._saved[1], 2)
        for fd in self._saved:
            os.close(fd)
        self._pipe.close()
        try:
            self._tee.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"WARNING: terminal log writer still busy; {self.path} may be incomplete")


class RunMetadata:
    """One ratrixcam launch: its id, terminal log, and metadata folder, born in the temp folder
    and moved whole to the save drive at shutdown."""

    def __init__(self, config: Config, config_path: str | Path, started: datetime | None = None):
        self.config = config
        self.config_path = config_path
        self.started = started or datetime.now()
        self.run_id = run_id(self.started)
        self.log_path = Path(config.temp_path) / terminal_log_filename(self.run_id)
        self.temp_run_dir = temp_run_metadata_dir(config.temp_path)  # during the run
        self.run_dir = run_metadata_dir(config.save_path, self.run_id)  # after shutdown
        self.terminal_log: TerminalLog | None = None
        self._collected = False

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
        print(f"Terminal output is being copied to {self.log_path}")
        return True

    def record_start(self) -> None:
        """Called when recording starts: lay down the run's metadata folder in temp with the
        config copy and the notes template, then put a copy of it on the save drive at once,
        so the run has a record there even if this computer dies mid-run. The shutdown
        collection replaces that copy with the final folder."""
        try:
            self.temp_run_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.config_path, self.temp_run_dir / os.path.basename(self.config_path))
            notes = self.temp_run_dir / NOTES_FILENAME
            if not notes.exists():
                notes.write_text(NOTES_TEMPLATE)
            print(f"Run metadata folder: {self.temp_run_dir} (moves to {self.run_dir} at shutdown)")
        except OSError as e:
            print(f"WARNING: could not create the run metadata folder {self.temp_run_dir}: {e}")
            return
        try:
            shutil.copytree(self.temp_run_dir, self.run_dir, dirs_exist_ok=True)
            print(f"Provisional copy of the run metadata folder: {self.run_dir}")
        except OSError as e:
            print(f"WARNING: could not write the provisional run metadata copy to {self.run_dir}: {e}")

    def collect_at_shutdown(self) -> None:
        """After the cameras have stopped: add to the per-camera manifests every row the
        cameras reported they could not write (read back from this run's terminal log);
        gather the status stills and every loose file in temp (screenshots, anything the
        user dropped there) into the run's metadata folder; remove per-camera temp folders
        that are empty; move the metadata folder whole to the save drive under the run id,
        replacing the provisional copy made at the start; and report. The terminal log is
        closed and moved into it last, so the report itself is in it. Folders that still
        hold files -- untransferred videos -- are left alone and reported; otherwise the
        temp folder is removed entirely."""
        if self._collected:
            return
        self._collected = True
        print(f"Collecting the run metadata as {RUN_METADATA_DIRNAME}/{self.run_id}")
        temp = Path(self.config.temp_path)
        kept: list[str] = []
        gathered = 0
        self._add_unwritten_manifest_rows()
        try:
            self.temp_run_dir.mkdir(parents=True, exist_ok=True)
            stills_src = Path(stills_dir(self.config.temp_path))
            if stills_src.is_dir():
                stills_dst = self.temp_run_dir / STILLS_DIRNAME
                stills_dst.mkdir(exist_ok=True)
                for f in sorted(stills_src.iterdir()):
                    if f.is_file() and f.name != ".DS_Store":
                        shutil.move(str(f), str(stills_dst / f.name))
                        gathered += 1
                _remove_if_empty(stills_src)
            for entry in sorted(temp.iterdir()):
                if entry == self.log_path or entry == self.temp_run_dir:
                    continue  # the log is closed and moved last; the run folder is the destination
                if entry.is_file():
                    if entry.name != ".DS_Store":
                        shutil.move(str(entry), str(self.temp_run_dir / entry.name))
                        gathered += 1
                elif entry.is_dir() and not _remove_if_empty(entry):
                    kept.append(entry.name)
            print(f"Gathered into the run metadata folder: {gathered} file(s) (stills and loose temp files)")
            if _move_tree(self.temp_run_dir, self.run_dir):
                print(f"Run metadata folder moved to {self.run_dir}")
            else:
                print(f"WARNING: run metadata folder could not be fully copied to {self.run_dir}; left in {self.temp_run_dir}")
                kept.append(self.temp_run_dir.name)
        except OSError as e:
            print(f"WARNING: collecting run metadata failed part-way: {e}")
        for name in kept:
            print(f"WARNING: temp folder '{name}' still contains files (untransferred videos?); left in place")
        if self.terminal_log is not None:
            print(
                f"Closing the terminal log and saving it as {RUN_METADATA_DIRNAME}/{self.run_id}/{self.log_path.name}"
            )
            self.terminal_log.stop()
        if self.log_path.exists() and self.run_dir.is_dir():
            try:
                shutil.move(str(self.log_path), str(self.run_dir / self.log_path.name))
            except OSError as e:
                print(f"WARNING: could not move the terminal log into {self.run_dir}: {e}")
        if not kept and temp.is_dir() and _remove_if_empty(temp):
            print("Temp folder removed")


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
                    print(f"WARNING: could not add {fields['path']} to {manifest_path} at cleanup: {e}")
        print(f"Manifest rows added at cleanup: {added}" + (f" ({failed} could not be added)" if failed else ""))


def _move_tree(src: Path, dst: Path) -> bool:
    """Copy a folder tree onto dst (files already there are replaced), verify that every file
    arrived with its size, and only then remove the source. False, with the source kept, if
    anything is missing or differs in size after the copy."""
    shutil.copytree(src, dst, dirs_exist_ok=True)
    for f in src.rglob("*"):
        if f.is_file():
            target = dst / f.relative_to(src)
            if not target.is_file() or target.stat().st_size != f.stat().st_size:
                return False
    shutil.rmtree(src)
    return True


def _remove_if_empty(folder: Path) -> bool:
    """Remove a folder that holds nothing but a .DS_Store; True if it is gone."""
    for f in folder.iterdir():
        if f.name == ".DS_Store":
            f.unlink()
        else:
            return False
    folder.rmdir()
    return True
