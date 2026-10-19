#!/usr/bin/env python3
"""
Pre-flight validator for a single source date-folder, meant to be run before
compress_drive.py touches it (the two are independent tools -- compress_drive.py never
calls this, so running it directly on a folder is a full manual override of any flag here).
Checks that the folder's parseable video files form a temporally coherent recording:
inter-start-time gaps should cluster around one (or two, allowing for a run-boundary
mid-folder) consistent value(s). A leftover gap that's *longer* than everything established
is always fine (ratrixcam was plausibly just off between runs -- unlimited count). A
*shorter* leftover gap is checked against the preceding clip's own actual measured duration
(frames/fps): if the gap is at least that long, it's physically consistent (the clip simply
ended before its nominal length -- a self-recovering camera hiccup), and up to ONE such
anomaly per stream is tolerated; more than one starts to look like the known systemic
launch/record/close churn bug, so it's flagged for manual review. If the gap is *shorter*
than the preceding clip's own duration, that's a real overlap -- physically impossible for
one camera -- and is always flagged regardless of count.

Only one representative video file per (subjID, view) stream is opened (via cv2) to check
that its frame count plausibly accounts for its neighboring gap (tolerant of nominal fps
being imprecise, e.g. a real 10-minute slice can read as ~9:47 of frames). Opening every
file for THIS check would be needlessly expensive; a single consistent sample is strong
evidence the rest hold up too, and compress_drive.py's own per-file checks catch anything
unusual regardless. (Files near a flagged gap DO each get opened once, to measure their
actual duration -- that's unavoidable for this check specifically.)

Files that aren't valid/parseable video filenames (or aren't .mp4 at all) don't fail the
folder -- they're set aside and reported separately, since they'd otherwise be silently
lost if the source folder is later deleted.

Example:
    python3 validate_source_folder.py \\
        "/Volumes/rlab2/videos/OLD556_Longitudinal[REDUNDANT]/Longitudinal Study_rat556_buddy_20250717"
"""

import argparse
import cv2
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sys

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))  # ratrixcam_naming
sys.path.insert(0, str(_REPO / "videoproc"))  # get_codec_nframes
from ratrixcam_naming import parse_filenames, parse_recording_datetime  # noqa: E402
from util import get_codec_nframes  # noqa: E402
from needs_investigation import append_entry

# filenames round timestamps to the second, so true (sub-1/30s-precise) intervals can
# appear up to 1s apart in the parsed start times even when nothing is actually wrong
GAP_CLUSTER_TOLERANCE_SECONDS = 1.0
FRAME_COUNT_TOLERANCE_FRACTION = 0.05  # accounts for true fps differing slightly from nominal


RAW_CAMERA_CODEC = "FMP4"  # what ratrixcam's own video writer produces; anything else on a
# source file means it was already compressed by something at some point


@dataclass
class FolderValidation:
    folder: Path
    ok: bool
    stream_reports: list[dict] = field(default_factory=list)
    unparseable_video_files: list[Path] = field(default_factory=list)
    non_video_files: list[Path] = field(default_factory=list)
    codec_counts: dict[str, int] = field(default_factory=dict)
    already_compressed: bool = False


def _get_fps(path: Path) -> float | None:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) if cap.isOpened() else None
    cap.release()
    return fps or None


def _get_actual_duration_seconds(path: Path) -> float:
    """Real measured content duration of a video, in seconds. 0 for an unopenable/empty
    file -- that's a valid duration for this purpose (any gap satisfies "gap >= 0")."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return 0.0
    n_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return n_frames / fps if n_frames and fps else 0.0


def _find_modal_clusters(gaps: list[float], tolerance: float) -> list[float]:
    """Find up to two modal gap values. First take the mode of all gaps; if everything
    left over after excluding that mode (+/-tolerance) itself clusters tightly around a
    single second value, accept that as a legitimate second interval (e.g. a run-boundary
    slice-duration change mid-folder). Otherwise there's only one established cluster, and
    the leftovers get judged individually (longer=downtime, shorter=blip-or-not)."""
    if not gaps:
        return []
    MIN_CLUSTER_SIZE = 5

    mode1 = Counter(round(g) for g in gaps).most_common(1)[0][0]
    leftover = [g for g in gaps if abs(g - mode1) > tolerance]
    if len(leftover) < MIN_CLUSTER_SIZE:
        return [mode1]  # too few to be real recurring evidence of a second cluster

    mode2 = Counter(round(g) for g in leftover).most_common(1)[0][0]
    if all(abs(g - mode2) <= tolerance for g in leftover):
        return [mode1, mode2]
    return [mode1]


def validate_stream(subj_ID: str, view: str, timed_files: list[tuple[datetime, Path]]) -> dict:
    timed_files = sorted(timed_files, key=lambda tf: tf[0])
    n = len(timed_files)
    report = {"subj_ID": subj_ID, "view": view, "n_files": n, "ok": True, "reason": ""}

    if n < 2:
        report["reason"] = "insufficient files to check timing (n<2); not flagged"
        return report

    gaps = [(t2 - t1).total_seconds() for (t1, _), (t2, _) in zip(timed_files, timed_files[1:])]
    established = _find_modal_clusters(gaps, GAP_CLUSTER_TOLERANCE_SECONDS)
    tol = GAP_CLUSTER_TOLERANCE_SECONDS
    hi = max(established)

    unexplained = []
    duration_explained = []
    for i, gap in enumerate(gaps):
        if any(abs(gap - v) <= tol for v in established):
            continue  # fits an established cluster
        if gap > hi + tol:
            continue  # longer than everything established: plausible camera-off downtime,
            # unlimited -- a recording can legitimately have several idle gaps
        preceding_file = timed_files[i][1]  # the file this gap follows
        actual_duration = _get_actual_duration_seconds(preceding_file)
        if gap >= actual_duration - tol:
            # physically consistent: the clip ended before its nominal length (whether
            # totally unopenable -- duration 0, trivially satisfied -- or a real, valid,
            # just-shorter-than-usual clip), and the next one started at or after that.
            # One such anomaly per stream is a benign camera hiccup; more starts to look
            # like the systemic launch/record/close churn bug, so it's capped, not excused
            # outright.
            duration_explained.append((i, gap, preceding_file.name, actual_duration))
        else:
            # the preceding clip's own measured content would extend past the point where
            # the next clip's timestamp claims recording had already resumed -- a real
            # overlap, physically impossible for one camera. Always unexplained.
            unexplained.append((i, gap, preceding_file.name))

    if unexplained:
        report["ok"] = False
        examples = ", ".join(f"{name}(+{g:.1f}s)" for _, g, name in unexplained[:5])
        report["reason"] = f"{len(unexplained)} unexplained inter-start interval(s), e.g. {examples}"
        report["established_clusters_sec"] = [round(v, 1) for v in established]
        return report

    if len(duration_explained) > 1:
        report["ok"] = False
        examples = ", ".join(f"{name}(+{g:.1f}s, clip lasted {d:.1f}s)" for _, g, name, d in duration_explained[:5])
        report["reason"] = (
            f"{len(duration_explained)} short-interval anomalies each individually explained by the "
            f"preceding clip's own duration, but more than one occurred in this stream -- flagging "
            f"for manual review rather than auto-passing, e.g. {examples}"
        )
        report["established_clusters_sec"] = [round(v, 1) for v in established]
        return report

    report["established_clusters_sec"] = [round(v, 1) for v in established]
    if duration_explained:
        _, g, name, d = duration_explained[0]
        report["duration_explained_anomaly"] = f"{name}(+{g:.1f}s, clip lasted {d:.1f}s)"

    # spot-check ONE representative file's frame count against its cluster's gap
    sample_gap = established[0]
    sample_idx = next(i for i, g in enumerate(gaps) if abs(g - sample_gap) <= tol)
    sample_path = timed_files[sample_idx][1]

    _, n_frames = get_codec_nframes(sample_path)
    fps = _get_fps(sample_path)
    if n_frames is None or fps is None:
        report["ok"] = False
        report["reason"] = f"representative file {sample_path.name} could not be opened or has no fps"
        return report

    expected_frames = sample_gap * fps
    discrepancy = abs(n_frames - expected_frames) / expected_frames
    report["sample_file"] = sample_path.name
    report["sample_frames"] = n_frames
    report["sample_expected_frames"] = round(expected_frames, 1)
    report["sample_discrepancy_pct"] = round(discrepancy * 100, 1)
    if discrepancy > FRAME_COUNT_TOLERANCE_FRACTION:
        report["ok"] = False
        report["reason"] = (
            f"representative file {sample_path.name} has {n_frames} frames, expected "
            f"~{expected_frames:.0f} (+/-{FRAME_COUNT_TOLERANCE_FRACTION:.0%}) for a {sample_gap:.0f}s gap"
        )

    return report


def validate_source_folder(folder: Path, pattern: str = "*.mp4") -> FolderValidation:
    all_mp4s = sorted(folder.glob(pattern))
    mp4_set = set(all_mp4s)
    non_video_files = sorted(p for p in folder.iterdir() if p.is_file() and p not in mp4_set)

    streams: dict[tuple[str, str], list[tuple[object, Path]]] = defaultdict(list)
    unparseable_video_files = []
    codec_counts: Counter = Counter()
    for path in all_mp4s:
        codec, _ = get_codec_nframes(path)
        codec_counts[codec or "unopenable"] += 1

        parsed = parse_filenames(path)
        if parsed is None:
            unparseable_video_files.append(path)
            continue
        subj_ID, view, date, time_ = parsed
        dt = parse_recording_datetime(date, time_)
        if dt is None:
            unparseable_video_files.append(path)
            continue
        streams[(subj_ID, view)].append((dt, path))

    stream_reports = [validate_stream(subj_ID, view, files) for (subj_ID, view), files in streams.items()]
    timing_ok = all(r["ok"] for r in stream_reports)

    non_raw_count = sum(c for codec, c in codec_counts.items() if codec not in (RAW_CAMERA_CODEC, "unopenable"))
    already_compressed = non_raw_count > 0

    return FolderValidation(
        folder=folder,
        ok=timing_ok and not already_compressed,
        stream_reports=stream_reports,
        unparseable_video_files=unparseable_video_files,
        non_video_files=non_video_files,
        codec_counts=dict(codec_counts),
        already_compressed=already_compressed,
    )


def find_video_folders(root: Path) -> list[Path]:
    """Any directory that directly contains at least one .mp4, regardless of how deeply
    nested (handles both flat _Longitudinal folders and camera-name/date _camnames trees)."""
    folders = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if any(f.endswith(".mp4") for f in filenames):
            folders.append(Path(dirpath))
    return sorted(folders)


def print_result(result: FolderValidation) -> None:
    print(f"{result.folder}: {'OK' if result.ok else 'FLAGGED'}")
    print(f"  codecs seen: {result.codec_counts}")
    if result.already_compressed:
        print("  ALREADY COMPRESSED: contains video(s) not in the raw camera codec")
    for r in result.stream_reports:
        status = "ok" if r["ok"] else "FLAGGED"
        print(f"  {r['subj_ID']}/{r['view']} (n={r['n_files']}): {status}  {r['reason']}")
        if "established_clusters_sec" in r:
            print(f"    established cluster(s): {r['established_clusters_sec']}s")
        if "duration_explained_anomaly" in r:
            print(f"    tolerated anomaly (1 allowed): {r['duration_explained_anomaly']}")
        if "sample_file" in r:
            print(
                f"    sample check: {r['sample_file']} -> {r['sample_frames']} frames "
                f"(expected ~{r['sample_expected_frames']}, {r['sample_discrepancy_pct']}% off)"
            )
    if result.unparseable_video_files:
        names = [p.name for p in result.unparseable_video_files]
        print(f"  {len(names)} unparseable video filename(s), skip individually (not lost unless folder deleted): {names}")
    if result.non_video_files:
        names = [p.name for p in result.non_video_files]
        print(f"  {len(names)} non-mp4 file(s) present (would be lost if source folder deleted): {names}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pre-flight validity check for source date-folder(s)")
    parser.add_argument("roots", type=Path, nargs="+", help="folders to scan (recursively, for any video-containing subfolder)")
    parser.add_argument("--pattern", default="*.mp4")
    parser.add_argument("--flagged-report", type=Path, default=None, help="write a text list of flagged folders here")
    parser.add_argument(
        "--videos-root",
        type=Path,
        default=None,
        help="root containing NEEDS_INVESTIGATION.txt; flagged folders get auto-appended there. "
        "Defaults to not writing to the master list at all if omitted.",
    )
    args = parser.parse_args()

    flagged_lines = []
    n_ok = n_flagged = n_already_compressed = n_exclusively_raw = 0
    exclusively_raw_folders = []
    for root in args.roots:
        subfolders = find_video_folders(root)
        root_flagged: list[tuple[Path, bool, bool]] = []  # (folder, already_compressed, timing_issue)

        for folder in subfolders:
            result = validate_source_folder(folder, args.pattern)
            print_result(result)
            if result.already_compressed:
                n_already_compressed += 1
            elif set(result.codec_counts) <= {RAW_CAMERA_CODEC, "unopenable"} and RAW_CAMERA_CODEC in result.codec_counts:
                n_exclusively_raw += 1
                exclusively_raw_folders.append(folder)

            if result.ok:
                n_ok += 1
            else:
                n_flagged += 1
                timing_issue = any(not r["ok"] for r in result.stream_reports)
                root_flagged.append((folder, result.already_compressed, timing_issue))
                for r in result.stream_reports:
                    if not r["ok"]:
                        flagged_lines.append(f"{folder}\t{r['reason']}")
                if result.already_compressed:
                    flagged_lines.append(f"{folder}\tcontains already-compressed video(s): {result.codec_counts}")

        # one consolidated NEEDS_INVESTIGATION entry per top-level root, not per sub-folder
        if root_flagged and args.videos_root is not None:
            n_flagged_here = len(root_flagged)
            n_already_compressed_here = sum(1 for _, ac, _ in root_flagged if ac)
            n_timing_here = sum(1 for _, _, t in root_flagged if t)
            scope = "ALL" if n_flagged_here == len(subfolders) else f"{n_flagged_here}/{len(subfolders)} (a subset of)"
            examples = ", ".join(str(f) for f, _, _ in root_flagged[:3])
            reason = (
                f"{scope} sub-folders flagged by pre-flight validation: "
                f"{n_already_compressed_here} already-compressed (non-{RAW_CAMERA_CODEC} codec), "
                f"{n_timing_here} with a timing anomaly. Examples: {examples}"
                f"{' ...' if n_flagged_here > 3 else ''}"
                " -- human decision needed on --recompress and/or manual timing review."
            )
            append_entry(args.videos_root, root, "validate_source_folder.py (pre-flight check)", reason)

    print()
    print(f"{n_ok} folder(s) OK, {n_flagged} folder(s) FLAGGED ({n_already_compressed} already-compressed)")
    print(f"{n_exclusively_raw} folder(s) contain EXCLUSIVELY uncompressed (raw {RAW_CAMERA_CODEC}) video:")
    for folder in exclusively_raw_folders:
        print(f"  {folder}")

    if args.flagged_report is not None:
        args.flagged_report.parent.mkdir(parents=True, exist_ok=True)
        args.flagged_report.write_text("\n".join(flagged_lines) + ("\n" if flagged_lines else ""))
        print(f"Flagged-folder list written to {args.flagged_report}")
