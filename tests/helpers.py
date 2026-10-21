"""Shared helpers for driving the videoproc CLIs as subprocesses and reading their output."""

import csv
import subprocess
import sys
from pathlib import Path

VIDEOPROC_DIR = Path(__file__).resolve().parents[1] / "videoproc"

SUBJECT_PREFIX = "rat"
SUBJ_ID = "901"
SUBJECT = SUBJECT_PREFIX + SUBJ_ID
VIEW = "home"
DATE = "20250101"
STUDYNAME = "LS"
MANIFEST_NAME = "test_manifest.csv"  # one manifest per test source tree, at its root

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


def clip_start(time: str, date=DATE) -> str:
    """The manifest's clip_start text for a test clip's date and time tokens."""
    return f"{date[:4]}-{date[4:6]}-{date[6:]} {time.replace('-', ':')}.000"


def manifest_path(source_root: Path) -> Path:
    return source_root / MANIFEST_NAME


def add_manifest_row(source_root: Path, rel_path: str, subj_id: str, view: str, clip_start_text: str, crf=None) -> None:
    path = manifest_path(source_root)
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["path", "subjID", "view", "clip_start", "row_written", "crf"])
        writer.writerow([rel_path, subj_id, view, clip_start_text, "", "" if crf is None else crf])


def place(clip: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(clip.read_bytes())


def place_clip(clip: Path, source_root: Path, time: str, subj_id=SUBJ_ID, view=VIEW, date=DATE, crf=None) -> Path:
    """Place a clip in the source tree AND list it in the tree's manifest."""
    target = source_path(source_root, time, SUBJECT_PREFIX + subj_id, view, date)
    place(clip, target)
    add_manifest_row(source_root, target.relative_to(source_root).as_posix(), subj_id, view, clip_start(time, date), crf)
    return target


def base_args(source: Path) -> list[str]:
    return ["--manifest", str(manifest_path(source)), "--studyname", STUDYNAME, "--subject_prefix", SUBJECT_PREFIX]


def run_compress_drive(source: Path, dest: Path, extra_args=(), timeout=300) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "compress_drive.py", str(source), str(dest), *base_args(source), *extra_args]
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input="\n", capture_output=True, text=True, timeout=timeout)


def run_confirm_and_delete(source: Path, dest: Path, extra_args=(), delete=False, timeout=300) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "confirm_and_delete.py", str(source), str(dest), *base_args(source), *extra_args]
    if delete:
        cmd.append("--delete")
    stdin = "\nDELETE\n" if delete else "\n"
    return subprocess.run(cmd, cwd=VIDEOPROC_DIR, input=stdin, capture_output=True, text=True, timeout=timeout)


def latest_run_dir(dest: Path, prefix: str) -> Path:
    """The run folder most recently written to. Run ids only resolve to the second, so two
    runs started within one second sort arbitrarily by name; folder mtime (ns) does not."""
    runs_root = dest / "videoproc_run_metadata"
    candidates = [p for p in runs_root.iterdir() if p.is_dir() and p.name.startswith(prefix)]
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def read_log(run_dir: Path) -> dict:
    """The run's per-file log (compression_log.csv or inspection_log.csv), rows keyed by the
    input file's basename."""
    for name in ("compression_log.csv", "inspection_log.csv"):
        if (run_dir / name).exists():
            with open(run_dir / name, newline="") as f:
                return {Path(row["input_path"]).name: row for row in csv.DictReader(f)}
    raise FileNotFoundError(f"no per-file log in {run_dir}")


def read_rows(path: Path) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def last_index_row(dest: Path) -> dict:
    return read_rows(dest / "videoproc_run_metadata" / "index.csv")[-1]


def is_true(value: str) -> bool:
    return value == "True"
