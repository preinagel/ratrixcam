"""Automatic record-keeping for a ratrixcam recording run (GitHub #34): a verbatim terminal
log, a copy of the camera config, and a shutdown step that gathers the run's loose artifacts
into one folder on the output drive. Nothing here touches camera capture or process control;
ratrixcam_IO.py calls into it at launch, at Start Recording, and after the cameras have stopped.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ratrix_naming import (
    CAMERA_CONFIG_FILENAME,
    NOTES_FILENAME,
    NOTES_TEMPLATE,
    STILLS_DIRNAME,
    code_version,
    run_id,
    run_metadata_dir,
    terminal_log_filename,
)
from ratrix_utils import Config, ensure_dir_exists


class TerminalLog:
    """Tee of file descriptors 1 and 2 into a log file, so output from every process of the
    run -- this one, the multicam supervisor and each camera server (spawned, so they do not
    share Python's sys.stdout) and native libraries -- reaches both the terminal and the file."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout.flush()
        sys.stderr.flush()
        self._saved = (os.dup(1), os.dup(2))
        self._tee = subprocess.Popen(["tee", "-a", str(path)], stdin=subprocess.PIPE)
        os.dup2(self._tee.stdin.fileno(), 1)
        os.dup2(self._tee.stdin.fileno(), 2)
        # fd 1 is now a pipe: keep this process line-buffered, and make spawned children
        # unbuffered, so messages reach the terminal when printed rather than at exit
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
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
        self._tee.stdin.close()
        try:
            self._tee.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"WARNING: terminal log writer still busy; {self.path} may be incomplete")


class RunMetadata:
    """One ratrixcam launch: its id, terminal log, and metadata folder on the output drive."""

    def __init__(self, config: Config, config_path: str, started: datetime | None = None):
        self.config = config
        self.config_path = config_path
        self.started = started or datetime.now()
        self.run_id = run_id(config.rack_name, self.started)
        self.log_path = Path(config.temp_path) / terminal_log_filename(self.run_id)
        self.run_dir = run_metadata_dir(config.save_path, self.run_id)
        self.terminal_log: TerminalLog | None = None
        self._collected = False

    def start_terminal_log(self) -> None:
        if not ensure_dir_exists(self.config.temp_path):
            print("WARNING: temp folder unavailable; terminal output will not be logged to a file")
            return
        self.terminal_log = TerminalLog(self.log_path)
        print(f"ratrixcam run {self.run_id} -- code version {code_version()}")
        print(f"Terminal output is being copied to {self.log_path}")

    def record_start(self) -> None:
        """Called when recording starts: copy the config actually in use (after any edits in
        the configuration editor) to the output drive and lay down the notes template."""
        if not ensure_dir_exists(str(self.run_dir)):
            print(f"WARNING: could not create {self.run_dir}; run metadata will not be saved")
            return
        try:
            shutil.copy2(self.config_path, self.run_dir / CAMERA_CONFIG_FILENAME)
            notes = self.run_dir / NOTES_FILENAME
            if not notes.exists():
                notes.write_text(NOTES_TEMPLATE)
            print(f"Run metadata folder: {self.run_dir}")
        except OSError as e:
            print(f"WARNING: could not write run metadata to {self.run_dir}: {e}")

    def collect_at_shutdown(self) -> None:
        """After the cameras have stopped: move the status stills and every loose file from
        the temp folder (screenshots, anything the user dropped there) into the run's
        metadata folder, remove per-camera temp folders that are empty, and report. The
        terminal log is closed and moved last, so the report itself is in it. Folders that
        still hold files -- untransferred videos -- are left alone and reported; otherwise
        the temp folder is removed entirely."""
        if self._collected:
            return
        self._collected = True
        print(f"Collecting run metadata into {self.run_dir}")
        if not ensure_dir_exists(str(self.run_dir)):
            print(f"WARNING: could not create {self.run_dir}; run artifacts left in {self.config.temp_path}")
            if self.terminal_log is not None:
                self.terminal_log.stop()
            return
        temp = Path(self.config.temp_path)
        moved, kept = [], []
        try:
            stills_src = Path(self.config.stills_path)
            if stills_src.is_dir():
                stills_dst = self.run_dir / STILLS_DIRNAME
                stills_dst.mkdir(exist_ok=True)
                for f in sorted(stills_src.iterdir()):
                    if f.is_file() and f.name != ".DS_Store":
                        shutil.move(str(f), str(stills_dst / f.name))
                        moved.append(f.name)
                _remove_if_empty(stills_src)
            for entry in sorted(temp.iterdir()):
                if entry == self.log_path:
                    continue  # closed and moved last, below
                if entry.is_file():
                    if entry.name != ".DS_Store":
                        shutil.move(str(entry), str(self.run_dir / entry.name))
                        moved.append(entry.name)
                elif entry.is_dir() and not _remove_if_empty(entry):
                    kept.append(entry.name)
        except OSError as e:
            print(f"WARNING: collecting run metadata failed part-way: {e}")
        print(f"Run metadata collected: {len(moved)} file(s) moved from the temp folder")
        for name in kept:
            print(f"WARNING: temp folder '{name}' still contains files (untransferred videos?); left in place")
        if self.terminal_log is not None:
            print(f"Closing the terminal log and saving it as {self.run_dir / self.log_path.name}")
            self.terminal_log.stop()
        if self.log_path.exists():
            try:
                shutil.move(str(self.log_path), str(self.run_dir / self.log_path.name))
            except OSError as e:
                print(f"WARNING: could not move the terminal log into {self.run_dir}: {e}")
        if not kept and temp.is_dir() and _remove_if_empty(temp):
            print(f"Temp folder {temp} removed")


def _remove_if_empty(folder: Path) -> bool:
    """Remove a folder that holds nothing but a .DS_Store; True if it is gone."""
    for f in folder.iterdir():
        if f.name == ".DS_Store":
            f.unlink()
        else:
            return False
    folder.rmdir()
    return True
