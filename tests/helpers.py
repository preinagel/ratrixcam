"""Shared helpers for driving the videoproc CLIs as subprocesses and reading their output."""

import csv
import subprocess
import sys
from pathlib import Path

VIDEOPROC_DIR = Path(__file__).resolve().parents[1] / "videoproc"

SUBJECT = "rat901"
VIEW = "home"
DATE = "20250101"
STUDYNAME = "LS"
PATTERN = "**/Longitudinal*/*.mp4"

# time tokens for the compress_drive run-1/2/3 test cases (t01..t10 share SUBJECT/VIEW/DATE)
TIMES = {
    "t01": "10-01-00", "t02": "10-02-00", "t03": "10-03-00", "t04": "10-04-00",
    "t05": "10-05-00", "t06": "10-06-00", "t07": "10-07-00", "t08": "10-08-00",
    "t09": "10-09-00", "t10": "10-10-00",
}
T11_DATE = "20250102"
T11_TIME = "10-11-00"
HOM_TIME = "10-12-00"
T12_TIME = "10-13-00"


def fname(time, date=DATE, view=VIEW, subject=SUBJECT):
    return f"{subject}_{view}_{date}_{time}.mp4"


def session_folder_path(source_root: Path, subject=SUBJECT, view=VIEW, date=DATE) -> Path:
    return source_root / f"Longitudinal Study_{subject}_{view}_{date}"


def session_folder(source_root: Path, subject=SUBJECT, view=VIEW, date=DATE) -> Path:
    """Like session_folder_path, but also creates the folder -- use session_folder_path
    instead for a read-only existence check."""
    folder = session_folder_path(source_root, subject, view, date)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def source_path(source_root: Path, time: str, subject=SUBJECT, view=VIEW, date=DATE) -> Path:
    return session_folder_path(source_root, subject, view, date) / fname(time, date, view, subject)


def dest_path(dest_root: Path, time: str, subject=SUBJECT, view=VIEW, date=DATE, studyname=STUDYNAME) -> Path:
    return dest_root / subject / f"{studyname}_{subject}_{view}_{date}" / fname(time, date, view, subject)


def place(clip: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(clip.read_bytes())


def run_compress_drive(source: Path, dest: Path, extra_args=(), timeout=300) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "compress_drive.py", str(source), str(dest),
        "--output_studyname", STUDYNAME, "--pattern", PATTERN, *extra_args,
    ]
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input="\n", capture_output=True, text=True, timeout=timeout)


def run_confirm_and_delete(source: Path, dest: Path, extra_args=(), delete=False, timeout=300) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "confirm_and_delete.py", str(source), str(dest),
        "--output_studyname", STUDYNAME, "--pattern", PATTERN, *extra_args,
    ]
    if delete:
        cmd.append("--delete")
    stdin = "\nDELETE\n" if delete else "\n"
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input=stdin, capture_output=True, text=True, timeout=timeout)


def latest_run_dir(dest: Path, prefix: str) -> Path:
    runs_root = dest / "videoproc_run_metadata"
    candidates = sorted(p.name for p in runs_root.iterdir() if p.is_dir() and p.name.startswith(prefix))
    return runs_root / candidates[-1]


def read_log(run_dir: Path) -> dict:
    """log.csv rows keyed by the input file's basename."""
    with open(run_dir / "log.csv", newline="") as f:
        return {Path(row["input_path"]).name: row for row in csv.DictReader(f)}


def read_rows(path: Path) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def is_true(value: str) -> bool:
    return value == "True"
