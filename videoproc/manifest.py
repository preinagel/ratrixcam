"""The clip manifest that drives compress_drive and confirm_and_delete: which video files to
work on and what is known about each. Any CSV with the required columns will do, whoever
wrote it; ratrixcam writes one per camera per run (ratrixcam_manifest.py).

Required columns
    path        the video file, relative to the source root given on the command line,
                '/'-separated
    subjID      subject identifier, any string
    view        camera view, any string
    clip_start  date and time the clip started, ISO form: YYYY-MM-DD HH:MM:SS[.fff]
Optional column
    crf         ffmpeg quality for this clip when it has motion (DEFAULT_CRF if blank or absent)
Other columns are ignored. A path listed twice is processed once (the first row wins, with a
warning). Nothing is inferred from the file names."""

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REQUIRED_COLUMNS = ["path", "subjID", "view", "clip_start"]
DEFAULT_CRF = 25

SUMMARY_FILENAME = "summary_by_subj_and_view.csv"
SUMMARY_FIELDS = ["subjID", "view", "start", "end", "file_count"]
SummaryRow = dict[str, str | int]  # one line of summary_by_subj_and_view.csv


class ManifestError(ValueError):
    """A manifest that cannot be used: missing columns or an unusable value. The message
    names the file and line."""


@dataclass(frozen=True)
class ManifestRow:
    path: str  # relative to the source root, '/'-separated
    subjID: str
    view: str
    clip_start: datetime
    crf: int | None
    where: str  # "<manifest file>:<line number>", for messages


def read_manifest(path: Path) -> list[ManifestRow]:
    rows: list[ManifestRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ManifestError(f"{path}: missing column(s) {', '.join(missing)}")
        for line, r in enumerate(reader, start=2):
            where = f"{path}:{line}"
            rel = (r["path"] or "").strip()
            if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                raise ManifestError(f"{where}: path must be relative to the source root, got {rel!r}")
            subjID, view = (r["subjID"] or "").strip(), (r["view"] or "").strip()
            if not subjID or not view:
                raise ManifestError(f"{where}: subjID and view must not be blank")
            try:
                clip_start = datetime.fromisoformat((r["clip_start"] or "").strip())
            except ValueError:
                raise ManifestError(f"{where}: clip_start {r['clip_start']!r} is not a date and time") from None
            crf_text = (r.get("crf") or "").strip()
            try:
                crf = int(crf_text) if crf_text else None
            except ValueError:
                raise ManifestError(f"{where}: crf {crf_text!r} is not an integer") from None
            rows.append(ManifestRow(rel, subjID, view, clip_start, crf, where))
    return rows


def read_manifests(paths) -> list[ManifestRow]:
    """The rows of several manifests in order, a path listed more than once kept only the
    first time."""
    rows: list[ManifestRow] = []
    seen: dict[str, str] = {}
    for p in paths:
        for row in read_manifest(Path(p)):
            if row.path in seen:
                print(f"WARNING: {row.where}: {row.path} is already listed at {seen[row.path]}; this row is ignored")
                continue
            seen[row.path] = row.where
            rows.append(row)
    return rows


def summarize_by_subject_and_view(rows: list[ManifestRow]) -> list[SummaryRow]:
    """One row per (subjID, view): first and last clip start, number of files."""
    starts: dict[tuple[str, str], list[datetime]] = {}
    for row in rows:
        starts.setdefault((row.subjID, row.view), []).append(row.clip_start)
    return [
        {
            "subjID": subjID,
            "view": view,
            "start": min(times).strftime("%Y-%m-%d %H:%M:%S"),
            "end": max(times).strftime("%Y-%m-%d %H:%M:%S"),
            "file_count": len(times),
        }
        for (subjID, view), times in sorted(starts.items())
    ]


def write_summary(path: Path, summary: list[SummaryRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary)
