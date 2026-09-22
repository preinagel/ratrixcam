#!/usr/bin/env python3 -u
"""Verify that every video under a source folder has a valid compressed copy at its standard
destination path, report what is and is not safe to delete, and -- only with --delete --
remove the verified source files. Writes nothing to the destination except its own run
folder under videoproc_run_metadata/.

Checks run in two passes: everything cheap first (filename, codec/frame-count probe, age),
so a run that would end in a refusal is stopped before the expensive per-file decode."""

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from pprint import pprint

from classify import (
    LOG_FIELDS as BASE_LOG_FIELDS,
    MAX_LISTED_PATTERN_MISMATCHES,
    STATUS_ALREADY_DONE,
    STATUS_DECODE_FAILED,
    STATUS_ERROR,
    STATUS_PATTERN_MISMATCH,
    STATUS_TOO_RECENT,
    STATUS_UNPARSEABLE,
    SkipFile,
    classify_file,
    find_unmatched_files,
)
from compress_drive import append_index_row, build_manifest, start_terminal_log, write_manifest
from util import build_output_path, parse_filenames

EXTRA_FIELDS = [
    "source_codec",
    "destination_codec",
    "destination_mtime",
    "age_hours",
    "size_ratio",
    "decode_ok",
    "deletable",
]
LOG_FIELDS = BASE_LOG_FIELDS + EXTRA_FIELDS
DELETION_FIELDS = ["input_path", "output_path", "input_size_bytes", "deleted_at"]


def destination_inside_source(source: Path, destination: Path) -> bool:
    """True if the destination root is the source folder itself or lies inside it; deleting
    sources could then remove destination files. The reverse nesting (source folder inside
    the destination root) is allowed -- per-file output paths are checked in main()."""
    source, destination = source.resolve(), destination.resolve()
    return destination == source or source in destination.parents


def decode_check(path: Path) -> tuple[bool, str]:
    """Fully decode a video with ffmpeg; returns (ok, error text)."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    err = result.stderr.decode("utf-8", errors="replace").strip()
    return result.returncode == 0 and not err, err


def new_row(input_path: Path) -> dict:
    row = {field: None for field in LOG_FIELDS}
    row.update(
        input_path=input_path,
        start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        action_item=False,
        deletable=False,
    )
    return row


def mark_skipped(row: dict, skip: SkipFile) -> None:
    row.update(status=skip.status, action_item=skip.action_item, skipped_reason=str(skip), deletable=False)


def write_rows(rows: list[dict], log_path: Path, action_items_path: Path) -> None:
    for path, subset in ((log_path, rows), (action_items_path, [r for r in rows if r["action_item"]])):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
            for r in subset:
                writer.writerow({k: ("" if v is None else v) for k, v in r.items()})


def unmatched_rows(unmatched_mp4: list[Path], n_other: int, pattern: str) -> list[dict]:
    rows = []
    n = len(unmatched_mp4)
    if 0 < n <= MAX_LISTED_PATTERN_MISMATCHES:
        for path in unmatched_mp4:
            row = new_row(path)
            row.update(
                status=STATUS_PATTERN_MISMATCH,
                action_item=True,
                skipped_reason=f"does not match --pattern '{pattern}'; not checked",
            )
            rows.append(row)
    if n:
        detail = "listed in log.csv" if n <= MAX_LISTED_PATTERN_MISMATCHES else "too many to list individually"
        print(f"WARNING: {n} .mp4 file(s) in source do not match --pattern '{pattern}' and were not checked ({detail})")
    if n_other:
        print(f"NOTE: {n_other} non-.mp4 file(s) in source were not checked")
    return rows


def print_summary(
    rows: list[dict],
    n_matched: int,
    n_unmatched_mp4: int,
    n_other: int,
    min_age_hours: float,
    decoded: bool,
    action_items_path: Path,
) -> None:
    file_rows = [r for r in rows if r["status"] != STATUS_PATTERN_MISMATCH]
    counts: dict[str, int] = {}
    for r in file_rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    n_deletable = sum(1 for r in file_rows if r["deletable"])
    n_flagged = sum(1 for r in rows if r["action_item"])
    checks = f"already_done, age >= {min_age_hours} h" + (", decode ok" if decoded else ", decode not run")
    print("\n==== verification summary ====")
    print(f"files matching --pattern: {n_matched}")
    print(f"rows logged for them: {len(file_rows)}")
    print(f"verified deletable ({checks}): {n_deletable}")
    print("not deletable, by reason:")
    for status, count in sorted(counts.items()):
        if status != STATUS_ALREADY_DONE:
            print(f"    {status}: {count}")
    unlisted = " (not listed individually; review by hand)" if n_unmatched_mp4 > MAX_LISTED_PATTERN_MISMATCHES else ""
    print(f".mp4 files not matching --pattern (not checked): {n_unmatched_mp4}{unlisted}")
    print(f"non-.mp4 files in source (not checked): {n_other}")
    print(f"rows flagged for human review: {n_flagged} -> {action_items_path}")


def delete_sources(source_root: Path, deletable: list[tuple[Path, Path]], run_dir: Path) -> None:
    """Delete verified source files one by one, logging each, then report what remains."""
    print(f"\nAbout to permanently delete {len(deletable)} verified source file(s) under {source_root}")
    try:
        answer = input("Type DELETE to proceed, anything else to abort: ")
    except EOFError:
        answer = ""
    if answer.strip() != "DELETE":
        print("Aborted; nothing deleted.")
        return

    deletion_log = run_dir / "deletion_log.csv"
    n_deleted = 0
    with open(deletion_log, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(DELETION_FIELDS)
        for src, dst in deletable:
            size = src.stat().st_size
            src.unlink()
            writer.writerow([src, dst, size, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            n_deleted += 1
    print(f"Deleted {n_deleted} file(s); record in {deletion_log}")

    # remove folders left empty (a lone .DS_Store counts as empty), deepest first, so only
    # folders that still hold something remain for inspection
    for d in sorted((p for p in source_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        entries = list(d.iterdir())
        if len(entries) == 1 and entries[0].name == ".DS_Store":
            entries[0].unlink()
            entries = []
        if not entries:
            d.rmdir()

    leftovers = [p for p in source_root.rglob("*") if p.is_file() and p.name != ".DS_Store"]
    if leftovers:
        print(f"{len(leftovers)} file(s) remain under {source_root}; folder retained. Showing up to 50:")
        for p in leftovers[:50]:
            print("   ", p.relative_to(source_root))
        return
    for ds in source_root.glob(".DS_Store"):
        ds.unlink()
    source_root.rmdir()
    print(f"{source_root} was empty after deletion and has been removed.")


def main(
    source: Path,
    destination: Path,
    pattern: str,
    output_studyname: str,
    min_age_hours: float,
    decode: bool,
    delete: bool,
    delete_partial: bool,
):
    kwargs = locals()

    run_id = f"confirm_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    run_dir = destination / "videoproc_run_metadata" / run_id
    log_path = run_dir / "log.csv"
    action_items_path = run_dir / "action_items.csv"
    log_file = start_terminal_log(run_dir / "terminal.log")
    print(f"Starting confirm_and_delete run {run_id}")
    print(f"Call: python3 {' '.join(sys.argv)}")

    try:
        if decode and shutil.which("ffmpeg") is None:
            print("Cannot find ffmpeg (needed for --decode)!")
            return

        input_paths: list[Path] = sorted(source.glob(pattern))
        if not input_paths:
            print(f"no video files found in {source.resolve()} matching pattern '{pattern}'. Exiting.")
            return
        print("found", len(input_paths), "video files in", source.resolve())

        manifest = build_manifest(input_paths)
        write_manifest(run_dir / "manifest.csv", manifest)
        append_index_row(destination / "videoproc_run_metadata" / "index.csv", run_id, manifest)
        (run_dir / "config.json").write_text(json.dumps(kwargs, indent=4, default=str))

        unmatched_mp4, n_other_files = find_unmatched_files(source, input_paths)
        rows: list[dict] = unmatched_rows(unmatched_mp4, n_other_files, pattern)

        # ---- pass 1: cheap checks for every file (parse, probe, age) ----
        now = time.time()
        source_resolved = source.resolve()
        for input_path in input_paths:
            row = new_row(input_path)
            rows.append(row)
            print(f"checking {input_path.name}")
            try:
                parsed = parse_filenames(input_path)
                if parsed is None:
                    raise SkipFile(STATUS_UNPARSEABLE, "could not parse filename", action_item=True)
                subj_ID, view, recording_date, recording_time = parsed
                output_path = build_output_path(
                    destination, output_studyname, subj_ID, view, recording_date, recording_time
                )
                row["output_path"] = output_path
                if source_resolved in output_path.resolve().parents:
                    raise SkipFile(
                        STATUS_ERROR, "computed destination path lies inside the source folder", action_item=True
                    )

                decision = classify_file(
                    input_path,
                    output_path,
                    recompress=False,
                    overwrite_raw=False,
                    overwrite_compressed=False,
                    verify_only=True,
                )
                row["source_codec"] = decision.input_codec
                if decision.output_codec is not None:
                    row["destination_codec"] = decision.output_codec
                    mtime = output_path.stat().st_mtime
                    row["destination_mtime"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    row["age_hours"] = round((now - mtime) / 3600, 1)
                    out_size = output_path.stat().st_size
                    if out_size:
                        row["size_ratio"] = round(input_path.stat().st_size / out_size, 1)

                if decision.skip is None:
                    raise RuntimeError("verify-only classification returned an action")
                if decision.skip.status != STATUS_ALREADY_DONE:
                    raise decision.skip
                if row["age_hours"] < min_age_hours:
                    raise SkipFile(
                        STATUS_TOO_RECENT,
                        f"destination written {row['age_hours']} h ago; --min_age_hours is {min_age_hours}",
                        action_item=True,
                    )
                row.update(status=STATUS_ALREADY_DONE, skipped_reason=str(decision.skip), deletable=True)

            except SkipFile as s:
                mark_skipped(row, s)
                label = "FLAGGED" if s.action_item else "NOT DELETABLE"
                print(f"    {label} {input_path.resolve()}: {s}")
            except Exception as e:
                row.update(status=STATUS_ERROR, action_item=True, error=str(e), deletable=False)
                print(f"    ERROR {input_path.resolve()}: {e}")

        write_rows(rows, log_path, action_items_path)
        print_summary(rows, len(input_paths), len(unmatched_mp4), n_other_files, min_age_hours, False, action_items_path)

        # ---- refusal gate: stop before decoding if the run cannot end in a deletion ----
        candidates = [r for r in rows if r["deletable"]]
        n_not_deletable = len(input_paths) - len(candidates) + len(unmatched_mp4)
        if delete and n_not_deletable and not delete_partial:
            print(
                f"\nREFUSING to delete: {n_not_deletable} file(s) are not verified deletable "
                "and --delete_partial is not set. Decode check skipped."
            )
            return
        if not delete and not decode:
            print("\nReport only: --delete not set, nothing was deleted.")
            return

        # ---- pass 2: full decode of the candidates ----
        if decode:
            print(f"\ndecoding {len(candidates)} destination file(s) with ffmpeg ...")
            for i, r in enumerate(candidates, 1):
                ok, err = decode_check(Path(r["output_path"]))
                r["decode_ok"] = ok
                if not ok:
                    r.update(
                        status=STATUS_DECODE_FAILED,
                        action_item=True,
                        skipped_reason="ffmpeg reported errors decoding the destination file",
                        error=err[:500],
                        deletable=False,
                    )
                    print(f"    FLAGGED {r['input_path']}: decode failed")
                if i % 100 == 0 or i == len(candidates):
                    print(f"    decoded {i}/{len(candidates)}")
            write_rows(rows, log_path, action_items_path)
            print_summary(rows, len(input_paths), len(unmatched_mp4), n_other_files, min_age_hours, True, action_items_path)

        if not delete:
            print("\nReport only: --delete not set, nothing was deleted.")
            return
        deletable = [(Path(r["input_path"]), Path(r["output_path"])) for r in rows if r["deletable"]]
        n_not_deletable = len(input_paths) - len(deletable) + len(unmatched_mp4)
        if n_not_deletable and not delete_partial:
            print(
                f"\nREFUSING to delete: {n_not_deletable} file(s) are not verified deletable "
                "and --delete_partial is not set."
            )
            return
        if not deletable:
            print("\nNothing verified deletable; nothing deleted.")
            return
        delete_sources(source, deletable, run_dir)

    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_file.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="verify compressed copies exist and are sound, then optionally delete the raw sources",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="folder holding the raw videos you are considering deleting")
    parser.add_argument(
        "destination", type=Path, help="root of the compressed archive (same as compress_drive's output)"
    )
    parser.add_argument(
        "--output_studyname", type=str, required=True, help="study-name prefix used for the compressed folders"
    )
    parser.add_argument("--pattern", default="**/LS*/*.mp4", type=str, help="pattern selecting the source videos to check")
    parser.add_argument(
        "--min_age_hours",
        default=24.0,
        type=float,
        help="a destination file modified more recently than this is not deletable yet -- a proxy for "
        "'a backup has run'; backups themselves are not checked",
    )
    parser.add_argument(
        "--decode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="fully decode every destination file with ffmpeg to catch corruption the frame-count check cannot. "
        "Costs roughly 10%% of the original compression run's time (about 1-3 s per 10-minute clip). "
        "Default: on when --delete is set, off otherwise; --no-decode turns it off",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="after the report, delete the verified source files (asks for typed confirmation)",
    )
    parser.add_argument(
        "--delete_partial",
        action="store_true",
        help="with --delete: proceed even if some files are not verified deletable (they are left in place). "
        "Without it, any unverified file blocks all deletion",
    )
    kwargs = vars(parser.parse_args())

    if not kwargs["source"].is_dir():
        raise NotADirectoryError(f"{kwargs['source']} is not a valid directory")
    if not kwargs["destination"].is_dir():
        raise NotADirectoryError(f"{kwargs['destination']} is not a valid directory")
    if destination_inside_source(kwargs["source"], kwargs["destination"]):
        raise ValueError("destination must not be the source folder or lie inside it")
    if destination_inside_source(kwargs["source"], kwargs["destination"] / "videoproc_run_metadata"):
        raise ValueError("the run-metadata folder under destination would lie inside the source folder")
    if kwargs["decode"] is None:
        kwargs["decode"] = kwargs["delete"]

    print("\nconfiguration:")
    pprint(kwargs, sort_dicts=False)
    input("\n[enter] to continue: ")

    main(**kwargs)
