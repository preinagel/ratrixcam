#!/usr/bin/env python3
"""
Read-only audit of a legacy video backlog, meant to be run before compress_drive.py's real
compression pass. Scans one or more source roots for video files, parses each filename
(reusing compress_drive.parse_filenames, including its legacy fallback), predicts each
file's final destination path, and reports files that failed to parse, files whose
predicted output already exists, and (subjID, view, date, time) keys that collide across
more than one source file. Makes no changes to any file; only writes the CSV report path
you specify.

Example:
    python3 legacy_audit_backlog.py \\
        "/Volumes/rlab2/videos/OLD560_Longitudinal" \\
        --dest /Volumes/rlab2/videos \\
        --output_studyname LS \\
        --report /tmp/old560_longitudinal_audit.csv
"""

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

import sys

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))  # ratrixcam_naming
sys.path.insert(0, str(_REPO / "videoproc"))  # get_codec_nframes
from ratrixcam_naming import build_output_path, parse_filenames  # noqa: E402
from util import get_codec_nframes  # noqa: E402
from legacy_datehist import load_or_build_datehist

REPORT_FIELDS = [
    "source_root",
    "source_path",
    "subj_ID",
    "view",
    "date",
    "time",
    "parsed",
    "predicted_output_path",
    "output_already_exists",
    "output_created",
    "duplicate_key",
    "input_frame_count",
    "output_frame_count",
    "frames_match",
]


def scan_root(root: Path, pattern: str) -> list[dict]:
    rows = []
    for path in sorted(root.glob(pattern)):
        parsed = parse_filenames(path)
        row = {"source_root": str(root), "source_path": str(path)}
        if parsed is None:
            row.update(subj_ID=None, view=None, date=None, time=None, parsed=False)
        else:
            subj_ID, view, date, time_ = parsed
            row.update(subj_ID=subj_ID, view=view, date=date, time=time_, parsed=True)
        rows.append(row)
    return rows


def audit(
    roots: list[Path],
    dest: Path,
    output_studyname: str,
    pattern: str,
    report: Path,
    verify_frames: bool,
    datehist_cache: Path,
    refresh_datehist: bool,
) -> None:
    all_rows: list[dict] = []
    for root in roots:
        found = scan_root(root, pattern)
        print(f"{root}: found {len(found)} files matching '{pattern}'")
        all_rows.extend(found)

    key_counts: dict[tuple, int] = {}
    for row in all_rows:
        if not row["parsed"]:
            continue
        key = (row["subj_ID"], row["view"], row["date"], row["time"])
        key_counts[key] = key_counts.get(key, 0) + 1

    unparseable = already_exists = duplicates = frame_mismatches = 0

    for row in all_rows:
        row.update(
            predicted_output_path="",
            output_already_exists="",
            output_created="",
            duplicate_key="",
            input_frame_count="",
            output_frame_count="",
            frames_match="",
        )
        if not row["parsed"]:
            unparseable += 1
            continue

        out_path = build_output_path(dest, output_studyname, row["subj_ID"], row["view"], row["date"], row["time"])
        row["predicted_output_path"] = str(out_path)
        row["output_already_exists"] = out_path.exists()

        key = (row["subj_ID"], row["view"], row["date"], row["time"])
        row["duplicate_key"] = key_counts[key] > 1
        duplicates += row["duplicate_key"]

        if not row["output_already_exists"]:
            continue
        already_exists += 1

        # creation date is cheap (just a stat()), so always record it; macOS/APFS exposes a
        # true birth time, other filesystems fall back to last-modified
        stat = out_path.stat()
        created = getattr(stat, "st_birthtime", stat.st_mtime)
        row["output_created"] = datetime.fromtimestamp(created).strftime("%Y-%m-%d")

        if verify_frames:
            _, input_n_frames = get_codec_nframes(Path(row["source_path"]))
            _, output_n_frames = get_codec_nframes(out_path)
            row["input_frame_count"] = input_n_frames if input_n_frames is not None else ""
            row["output_frame_count"] = output_n_frames if output_n_frames is not None else ""
            if input_n_frames is not None and output_n_frames is not None:
                row["frames_match"] = input_n_frames == output_n_frames
                frame_mismatches += not row["frames_match"]

    report.parent.mkdir(parents=True, exist_ok=True)
    with open(report, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    print()
    print(f"Scanned {len(all_rows)} files across {len(roots)} root(s).")
    print(f"Unparseable (neither modern nor legacy shape recognized): {unparseable}")
    print(f"Predicted output already exists at destination: {already_exists}")
    print(f"Files sharing a duplicate (subj_ID, view, date, time) key: {duplicates}")
    if verify_frames:
        print(f"Existing outputs whose frame count does NOT match the source: {frame_mismatches}")

    if already_exists:
        print()
        print(f"Loading reference creation-date histogram for the whole archive ({dest}/rat*/*/*.mp4)...")
        reference_hist = load_or_build_datehist(dest, datehist_cache, refresh=refresh_datehist)
        print(f"  (from cache: {datehist_cache})" if not refresh_datehist and datehist_cache.exists() else "  (freshly built)")
        print(f"  reference archive spans {min(reference_hist)} to {max(reference_hist)}, {sum(reference_hist.values())} files")

        found_dates = Counter(r["output_created"] for r in all_rows if r["output_created"])
        print()
        print("This scan's existing-output creation dates, vs. the reference archive's count for that same date:")
        for date in sorted(found_dates):
            print(f"  {date}: {found_dates[date]} found here | {reference_hist.get(date, 0)} in reference archive that day")
        earliest_in_archive = min(reference_hist)
        outliers = [d for d in found_dates if d < earliest_in_archive]
        if outliers:
            print(f"  NOTE: {len(outliers)} date(s) found here predate the entire reference archive -- investigate: {sorted(outliers)}")

    print()
    print(f"Full per-file report written to {report}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only audit of a legacy video backlog before running compress_drive.py")
    parser.add_argument("roots", type=Path, nargs="+", help="source folder(s) to scan")
    parser.add_argument(
        "--dest", type=Path, required=True, help="final compressed-video destination root, to predict output paths"
    )
    parser.add_argument(
        "--output_studyname",
        type=str,
        required=True,
        help="study-name prefix that the real compression run would use (must match what you'd pass to compress_drive.py)",
    )
    parser.add_argument("--pattern", type=str, default="**/*.mp4", help="glob pattern for video files within each root")
    parser.add_argument("--report", type=Path, required=True, help="path to write the per-file CSV report")
    parser.add_argument(
        "--verify-frames",
        action="store_true",
        help="for files whose output already exists, compare source/destination frame counts "
        "(reads container metadata only via cv2.CAP_PROP_FRAME_COUNT, does not decode frames)",
    )
    parser.add_argument(
        "--datehist-cache",
        type=Path,
        default=None,
        help="cache path for the whole-archive creation-date reference histogram; defaults to <dest>/datehist.csv",
    )
    parser.add_argument(
        "--refresh-datehist",
        action="store_true",
        help="rebuild the reference histogram from <dest>/rat*/*/*.mp4 instead of using the cache",
    )
    args = parser.parse_args()

    for root in args.roots:
        if not root.is_dir():
            raise NotADirectoryError(f"{root} is not a valid directory")

    datehist_cache = args.datehist_cache if args.datehist_cache is not None else args.dest / "datehist.csv"
    audit(
        args.roots,
        args.dest,
        args.output_studyname,
        args.pattern,
        args.report,
        args.verify_frames,
        datehist_cache,
        args.refresh_datehist,
    )
