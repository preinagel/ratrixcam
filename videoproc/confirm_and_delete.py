#!/usr/bin/env python3 -u
"""Verify that every video listed in a manifest has a valid compressed copy at its standard
destination path, report what is and is not safe to delete, and -- only with --delete --
remove the verified source files. Writes nothing to the destination except its own run
folder under videoproc_run_metadata/.

Checks run in two passes: everything cheap first (presence, codec/frame-count probe, age),
so a run that would end in a refusal is stopped before the expensive per-file decode."""

import argparse
import csv
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pprint import pprint
from typing import Any, Callable

from classify import (
    LOG_FIELDS as BASE_LOG_FIELDS,
    STATUS_ALREADY_DONE,
    STATUS_DECODE_FAILED,
    STATUS_ERROR,
    STATUS_MISSING_SOURCE,
    STATUS_TOO_RECENT,
    SkipFile,
    classify_file,
)
from compress_drive import append_index_row, start_terminal_log
from manifest import SUMMARY_FILENAME, ManifestError, read_manifests, summarize_by_subject_and_view, write_summary
from util import build_output_path, code_version, get_codec_nframes, shorten

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
Row = dict[str, Any]  # one inspection_log.csv line, keyed by LOG_FIELDS


@dataclass
class Outcome:
    """What a run did with the listed clips, for a caller that acts on the result."""

    run_dir: Path
    n_listed: int
    n_deleted: int
    n_remaining: int  # listed clips still at the source, plus those missing with no destination copy


def append_deletion_rows(run_dir: Path, rows: list[list[object]]) -> Path:
    """Append rows to the run's deletion_log.csv, creating it with its header if needed."""
    deletion_log = run_dir / "deletion_log.csv"
    new = not deletion_log.exists()
    with open(deletion_log, "a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(DELETION_FIELDS)
        writer.writerows(rows)
    return deletion_log


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


def new_row(input_path: Path) -> Row:
    row: Row = {field: None for field in LOG_FIELDS}
    row.update(
        input_path=input_path,
        start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        action_item=False,
        deletable=False,
    )
    return row


def mark_skipped(row: Row, skip: SkipFile) -> None:
    row.update(status=skip.status, action_item=skip.action_item, skipped_reason=str(skip), deletable=False)


def write_rows(rows: list[Row], log_path: Path, action_items_path: Path) -> None:
    for path, subset in ((log_path, rows), (action_items_path, [r for r in rows if r["action_item"]])):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
            for r in subset:
                writer.writerow({k: ("" if v is None else v) for k, v in r.items()})


def print_summary(
    rows: list[Row],
    n_listed: int,
    min_age_hours: float,
    decoded: bool,
    action_items_path: str,
) -> None:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    n_deletable = sum(1 for r in rows if r["deletable"])
    n_flagged = sum(1 for r in rows if r["action_item"])
    n_missing = counts.get(STATUS_MISSING_SOURCE, 0)
    n_blocking = len(rows) - n_deletable - n_missing
    checks = f"already_done, age >= {min_age_hours} h" + (", decode ok" if decoded else ", decode not run")
    print("\n==== verification summary ====")
    print(f"clips listed in the manifest(s): {n_listed}")
    print(f"verified deletable ({checks}): {n_deletable}")
    print(f"listed but not at the source (nothing to delete): {n_missing}")
    if n_blocking:
        print("not deletable, by reason:")
        for status, count in sorted(counts.items()):
            if status not in (STATUS_ALREADY_DONE, STATUS_MISSING_SOURCE):
                print(f"    {status}: {count}")
        print(
            f"{n_blocking} clip(s) would block --delete; --delete_partial would delete the {n_deletable} "
            f"verified one(s) and leave those {n_blocking}"
        )
    else:
        print("every listed clip present at the source is verified: --delete would delete them all")
    if not decoded:
        print("note: the decode check was not run (default without --delete); a clip verified here can still fail it under --delete")
    print(f"rows flagged for human review: {n_flagged} -> {action_items_path}")


def remove_emptied_folders(source_root: Path, folders: set[Path]) -> int:
    """Remove each of `folders`, and then its parents, while they hold nothing (a lone
    .DS_Store counts as nothing), stopping below source_root. Returns the number removed."""
    source_root = source_root.resolve()
    removed = 0
    for folder in sorted({f.resolve() for f in folders}, key=lambda p: len(p.parts), reverse=True):
        while folder != source_root and source_root in folder.parents:
            entries = list(folder.iterdir())
            if len(entries) == 1 and entries[0].name == ".DS_Store":
                entries[0].unlink()
                entries = []
            if entries:
                break
            folder.rmdir()
            removed += 1
            folder = folder.parent
    return removed


def delete_sources(
    source_root: Path, deletable: list[tuple[Path, Path]], run_dir: Path, short: Callable[[Path | str], str] = str
) -> int:
    """Delete verified source files one by one, logging each as it goes so an interrupted run
    leaves a complete record; then remove the folders this left empty. Returns the number of
    files deleted."""
    print(f"\nAbout to permanently delete {len(deletable)} verified source file(s) under {short(source_root)}")
    try:
        answer = input("Type DELETE to proceed, anything else to abort: ")
    except EOFError:
        answer = ""
    if answer.strip() != "DELETE":
        print("Aborted; nothing deleted.")
        return 0

    deletion_log = run_dir / "deletion_log.csv"
    new = not deletion_log.exists()
    n_deleted = 0
    with open(deletion_log, "a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(DELETION_FIELDS)
        for src, dst in deletable:
            size = src.stat().st_size
            src.unlink()
            writer.writerow([src, dst, size, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            f.flush()
            n_deleted += 1
    print(f"Deleted {n_deleted} file(s); record in {short(deletion_log)}")
    n_folders = remove_emptied_folders(source_root, {src.parent for src, _ in deletable})
    if n_folders:
        print(f"Removed {n_folders} source folder(s) left empty")
    return n_deleted


def main(
    source: Path,
    destination: Path,
    manifest: list[Path],
    studyname: str | None,
    subject_prefix: str,
    min_age_hours: float,
    decode: bool | None,
    delete: bool,
    delete_partial: bool,
    call_note: str | None = None,
) -> Outcome | None:
    """Verify, and with --delete remove, the clips the manifest(s) list. `decode` None means
    the default: on with --delete, off otherwise. `call_note`, given by a caller such as the
    ratrixcam wrapper, is printed in place of the command line. Returns the run's outcome,
    or None if the run stopped before checking anything."""
    if decode is None:
        decode = delete

    def short(path: Path | str) -> str:
        return shorten(path, source, destination)

    run_id = f"confirm_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    run_dir = destination / "videoproc_run_metadata" / run_id
    log_path = run_dir / "inspection_log.csv"
    action_items_path = run_dir / "action_items.csv"
    log_file = start_terminal_log(run_dir / "terminal.log")
    print(f"Starting confirm_and_delete run {run_id} -- code version {code_version()}")
    print(call_note if call_note else f"Call: python3 {' '.join(sys.argv)}")
    print(f"source root      {source.resolve()}")
    print(f"destination root {destination.resolve()}")

    try:
        if decode and shutil.which("ffmpeg") is None:
            print("Cannot find ffmpeg (needed for --decode)!")
            return

        try:
            listed = read_manifests(manifest)
        except ManifestError as e:
            print(f"ERROR: {e}. Exiting.")
            return
        if not listed:
            print("the manifest(s) list no clips. Exiting.")
            return
        print(f"{len(listed)} clip(s) listed in {len(manifest)} manifest(s)")

        summary = summarize_by_subject_and_view(listed)
        write_summary(run_dir / SUMMARY_FILENAME, summary)
        append_index_row(destination / "videoproc_run_metadata" / "index.csv", run_id, summary)

        rows: list[Row] = []

        # ---- pass 1: cheap checks for every file (presence, probe, age) ----
        now = time.time()
        source_resolved = source.resolve()
        print(f"checking {len(listed)} listed clip(s); only exceptions are printed")
        for i, listed_row in enumerate(listed, 1):
            input_path = source / listed_row.path
            row = new_row(input_path)
            row.update(subjID=listed_row.subjID, view=listed_row.view)
            rows.append(row)
            if i % 100 == 0 or i == len(listed):
                print(f"    checked {i}/{len(listed)}")
            try:
                output_path = build_output_path(
                    destination, subject_prefix, studyname, listed_row.subjID, listed_row.view, listed_row.clip_start, input_path.name
                )
                row["output_path"] = output_path
                if not input_path.is_file():
                    # nothing to delete; the row records whether the destination has the clip
                    row["destination_codec"], _ = get_codec_nframes(output_path) if output_path.is_file() else (None, None)
                    archived = "destination copy present" if row["destination_codec"] else "no destination copy"
                    raise SkipFile(STATUS_MISSING_SOURCE, f"listed in the manifest but not found at the source; {archived}", action_item=True)
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
                print(f"    {label} {short(input_path)}: {s}")
            except Exception as e:
                row.update(status=STATUS_ERROR, action_item=True, error=str(e), deletable=False)
                print(f"    ERROR {short(input_path)}: {e}")

        write_rows(rows, log_path, action_items_path)
        print_summary(rows, len(listed), min_age_hours, False, short(action_items_path))

        def outcome(n_deleted: int = 0) -> Outcome:
            unresolved = sum(1 for r in rows if r["status"] == STATUS_MISSING_SOURCE and not r["destination_codec"])
            present = sum(1 for r in rows if r["status"] != STATUS_MISSING_SOURCE)
            return Outcome(run_dir, len(listed), n_deleted, present - n_deleted + unresolved)

        # ---- refusal gate: stop before decoding if the run cannot end in a deletion ----
        # a file missing at the source cannot be deleted and does not block the others
        n_missing = sum(1 for r in rows if r["status"] == STATUS_MISSING_SOURCE)
        candidates = [r for r in rows if r["deletable"]]
        n_not_deletable = len(listed) - len(candidates) - n_missing
        if delete and n_not_deletable and not delete_partial:
            print(
                f"\nREFUSING to delete: {n_not_deletable} file(s) are not verified deletable "
                "and --delete_partial is not set. Decode check skipped."
            )
            return outcome()
        if not delete and not decode:
            print("\nReport only: --delete not set, nothing was deleted.")
            return outcome()

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
                    print(f"    FLAGGED {short(r['input_path'])}: decode failed")
                if i % 100 == 0 or i == len(candidates):
                    print(f"    decoded {i}/{len(candidates)}")
            write_rows(rows, log_path, action_items_path)
            print_summary(rows, len(listed), min_age_hours, True, short(action_items_path))

        if not delete:
            print("\nReport only: --delete not set, nothing was deleted.")
            return outcome()
        deletable = [(Path(r["input_path"]), Path(r["output_path"])) for r in rows if r["deletable"]]
        n_not_deletable = len(listed) - len(deletable) - n_missing
        if n_not_deletable and not delete_partial:
            print(
                f"\nREFUSING to delete: {n_not_deletable} file(s) are not verified deletable "
                "and --delete_partial is not set."
            )
            return outcome()
        if not deletable:
            print("\nNothing verified deletable; nothing deleted.")
            return outcome()
        return outcome(delete_sources(source, deletable, run_dir, short))

    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_file.close()


def add_processing_arguments(parser: argparse.ArgumentParser) -> None:
    """The options that control the verification and the deletion, shared with the ratrixcam
    wrapper (process_ratrixcam_output.py); the source, destination and manifest arguments are
    the caller's."""
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="verify compressed copies exist and are sound, then optionally delete the raw sources",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="source root of the raw videos you are considering deleting; manifest paths are relative to it")
    parser.add_argument(
        "destination", type=Path, help="destination root (same as compress_drive's output)"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        required=True,
        help="manifest CSV listing the clips to check (the same one(s) given to compress_drive); repeatable",
    )
    parser.add_argument(
        "--studyname", type=str, default=None, help="study-name prefix of the destination day folders, as given to compress_drive"
    )
    parser.add_argument(
        "--subject_prefix", type=str, default="subj", help="subject prefix of the destination paths, as given to compress_drive"
    )
    add_processing_arguments(parser)
    kwargs = vars(parser.parse_args())

    if not kwargs["source"].is_dir():
        raise NotADirectoryError(f"{kwargs['source']} is not a valid directory")
    if not kwargs["destination"].is_dir():
        raise NotADirectoryError(f"{kwargs['destination']} is not a valid directory")
    if destination_inside_source(kwargs["source"], kwargs["destination"]):
        raise ValueError("destination must not be the source folder or lie inside it")
    if destination_inside_source(kwargs["source"], kwargs["destination"] / "videoproc_run_metadata"):
        raise ValueError("the run-metadata folder under destination would lie inside the source folder")
    for m in kwargs["manifest"]:
        if not m.is_file():
            raise FileNotFoundError(f"manifest {m} not found")

    print("\nconfiguration:")
    pprint(kwargs, sort_dicts=False)
    input("\n[enter] to continue: ")

    main(**kwargs)
