#!/usr/bin/env python3 -u
"""Process a folder written by ratrixcam (a save_path drive, or a copy of one) with the videoproc
tools, and carry everything else such a folder holds that the tools themselves ignore.

    process_ratrixcam_output.py compress <root> <destination> [compress_drive options]

compress: every folder under <root>/ratrixcam_run_metadata/ is a recording run; its manifests/
subfolder holds the per-camera manifests and its camera-config copy gives the study label and
the subject prefix. compress_drive runs once per recording run with that run's manifests,
<root> as the source root and <destination> as the output root; then, appended to that
compression run's own terminal log, the run's folder is copied whole to
<destination>/ratrixcam_run_metadata/<run id>/, never overwriting a destination file that
differs in size. After the last run, appended to its records: run folders that could not be
processed are carried the same way, video files under <root> that no manifest lists are logged
as not_in_manifest rows in the compression log (the first ten individually) and flagged for
review, and other files are counted. Nothing under <root> is deleted.
"""

import argparse
import csv
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from pprint import pprint
from typing import IO, Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root; the shared module moves next (#26)
from ratrixcam_naming import MANIFEST_SUFFIX, MANIFESTS_DIRNAME, RUN_METADATA_DIRNAME  # noqa: E402

import compress_drive  # noqa: E402
from classify import LOG_FIELDS  # noqa: E402
from manifest import ManifestError, read_manifests  # noqa: E402

STATUS_NOT_IN_MANIFEST = "not_in_manifest"
MAX_LISTED_NOT_IN_MANIFEST = 10
VIDEO_SUFFIX = ".mp4"


# ----------------------------------------------------------------------------- the runs in the folder

@dataclass
class RatrixcamRun:
    run_id: str
    run_dir: Path  # <root>/ratrixcam_run_metadata/<run id>
    manifests: list[Path]  # the files in its manifests/ subfolder
    studyname: str | None  # from the config copy; None when it could not be read
    subject_prefix: str | None


def find_runs(root: Path) -> list[RatrixcamRun]:
    """One entry per run folder under <root>/ratrixcam_run_metadata/, with the study label and
    subject prefix read from the run's config copy. Problems are printed, not raised."""
    runs: list[RatrixcamRun] = []
    container = root / RUN_METADATA_DIRNAME
    if not container.is_dir():
        return runs
    for run_dir in sorted(p for p in container.iterdir() if p.is_dir()):
        run_id = run_dir.name
        manifests_dir = run_dir / MANIFESTS_DIRNAME
        manifests = sorted(p for p in manifests_dir.glob(f"*{MANIFEST_SUFFIX}") if p.is_file()) if manifests_dir.is_dir() else []
        studyname = prefix = None
        configs = sorted(run_dir.glob("*.json"))
        if len(configs) != 1:
            print(f"WARNING: run {run_id}: expected one camera config copy in {run_dir}, found {len(configs)}")
        else:
            try:
                config = json.loads(configs[0].read_text())
                studyname = config["study_label"]
                prefix = config.get("subject_prefix", "subj")
            except (OSError, ValueError, KeyError) as e:
                print(f"WARNING: run {run_id}: could not read study label from {configs[0]}: {e}")
        runs.append(RatrixcamRun(run_id, run_dir, manifests, studyname, prefix))
    return runs


# ----------------------------------------------------------------------------- carrying folders

def _files(folder: Path) -> dict[str, int]:
    return {p.relative_to(folder).as_posix(): p.stat().st_size for p in folder.rglob("*") if p.is_file()}


def compare(src_dir: Path, dest_dir: Path) -> tuple[list[str], list[str]]:
    """(files missing at the destination, files present with a different size)."""
    src, dst = _files(src_dir), (_files(dest_dir) if dest_dir.exists() else {})
    missing = sorted(f for f in src if f not in dst)
    differing = sorted(f for f in src if f in dst and dst[f] != src[f])
    return missing, differing


def transfer(src_dir: Path, dest_dir: Path) -> tuple[int, list[str]]:
    """Copy the files the destination lacks; leave files that already exist alone. Returns
    (number copied, files whose destination copy differs in size -- never overwritten)."""
    missing, differing = compare(src_dir, dest_dir)
    for rel in missing:
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_dir / rel, target)
    return len(missing), differing


def find_run_metadata_dirs(root: Path) -> list[Path]:
    """Every run folder under any ratrixcam_run_metadata/ folder in the tree."""
    found: list[Path] = []
    for container in root.rglob(RUN_METADATA_DIRNAME):
        if container.is_dir():
            found.extend(p for p in sorted(container.iterdir()) if p.is_dir())
    return found


def metadata_destination(folder: Path, destination: Path) -> Path:
    return destination / RUN_METADATA_DIRNAME / folder.name


def transfer_folders(label: str, folders: list[Path], destination_of: Callable[[Path], Path]) -> str:
    """Carry each folder to destination_of(folder); print one line per folder and return the
    summary line."""
    n_files, problems = 0, []
    for folder in folders:
        dest_dir = destination_of(folder)
        n_copied, differing = transfer(folder, dest_dir)
        n_files += n_copied
        problems += [f"{folder.name}/{rel}" for rel in differing]
        print(f"{label} {folder.name}: {n_copied} file(s) copied to {dest_dir}")
        for rel in differing:
            print(f"    WARNING: {rel} already exists at the destination with a different size; not overwritten")
    line = f"{label} folders transferred: {len(folders)} ({n_files} file(s) copied)"
    if problems:
        line += f"; WARNING: {len(problems)} file(s) differ at the destination and were not overwritten"
    return line


# ----------------------------------------------------------------------------- files outside the manifests

def is_expected_structure(path: Path, root: Path) -> bool:
    """True for files inside run-metadata folders, which are carried as units."""
    return RUN_METADATA_DIRNAME in path.relative_to(root).parts


def inventory_unlisted(root: Path, listed: set[Path]) -> tuple[list[Path], int]:
    """(video files under root that no manifest lists, count of other files), ignoring the
    run-metadata folders and .DS_Store."""
    videos: list[Path] = []
    n_other = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.name == ".DS_Store" or is_expected_structure(path, root):
            continue
        if path.resolve() in listed:
            continue
        if path.suffix.lower() == VIDEO_SUFFIX:
            videos.append(path)
        else:
            n_other += 1
    return sorted(videos), n_other


def append_unlisted_rows(run_dir: Path, videos: list[Path]) -> None:
    """One not_in_manifest row per unlisted video, flagged for review, in the run's
    compression log and action-items file (the first MAX_LISTED_NOT_IN_MANIFEST only)."""
    rows = []
    for path in videos[:MAX_LISTED_NOT_IN_MANIFEST]:
        row = {field: "" for field in LOG_FIELDS}
        row.update(
            input_path=str(path), status=STATUS_NOT_IN_MANIFEST, action_item="True",
            skipped_reason="not listed in any manifest; not processed",
        )
        rows.append(row)
    for name in ("compression_log.csv", "action_items.csv"):
        with open(run_dir / name, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writerows(rows)


def report_unlisted(videos: list[Path], n_other: int, root: Path) -> None:
    n = len(videos)
    if n:
        detail = "listed in compression_log.csv" if n <= MAX_LISTED_NOT_IN_MANIFEST else "too many to list individually"
        print(f"WARNING: {n} video file(s) under {root} are not listed in any manifest and were not processed ({detail})")
    else:
        print(f"video files under {root} not listed in any manifest: 0")
    print(f"other files under {root} (not checked or copied): {n_other}")


# ----------------------------------------------------------------------------- compress

class AppendedSection:
    """Reopen a compression run's terminal log so that what is printed inside the block is
    appended to it, as well as shown on the terminal."""

    def __init__(self, run_dir: Path, title: str):
        self.run_dir, self.title = run_dir, title
        self._log_file: IO[str] | None = None

    def __enter__(self):
        self._log_file = compress_drive.start_terminal_log(self.run_dir / "terminal.log")
        print(f"\n==== {self.title} ====")
        return self

    def __exit__(self, *_exc: object) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        if self._log_file is not None:
            self._log_file.close()


def compress(root: Path, destination: Path, **options: Any) -> int:
    """compress_drive once per run found under root, each followed by its folders; then the
    leftovers and the inventory, appended to the last compression run's records. `options`
    are compress_drive's processing options as parsed by add_processing_arguments. Returns
    the exit code."""
    runs = find_runs(root)
    if not runs:
        print(f"ERROR: no run folder under {root / RUN_METADATA_DIRNAME}; nothing to process")
        return 1

    problems: list[str] = []
    listed: set[Path] = set()
    processed: set[str] = set()
    last_run_dir: Path | None = None
    for run in runs:
        if run.studyname is None or run.subject_prefix is None:
            problems.append(f"ERROR: run {run.run_id} skipped: study label and subject prefix unknown (see WARNING above)")
            continue
        if not run.manifests:
            problems.append(f"WARNING: run {run.run_id} skipped: no manifest in its {MANIFESTS_DIRNAME}/ folder")
            continue
        try:
            listed.update((root / row.path).resolve() for row in read_manifests(run.manifests))
        except ManifestError:
            pass  # compress_drive reports it
        run_dir = compress_drive.main(
            input=root, output=destination, manifest=run.manifests,
            studyname=run.studyname, subject_prefix=run.subject_prefix, **options,
        )
        if run_dir is None:
            problems.append(f"ERROR: compress_drive stopped before processing run {run.run_id}")
            continue
        processed.add(run.run_id)
        last_run_dir = run_dir
        with AppendedSection(run_dir, f"ratrixcam run {run.run_id}: run metadata folder"):
            print(transfer_folders("ratrixcam run-metadata", [run.run_dir], lambda d: metadata_destination(d, destination)))

    for line in problems:
        print(line)
    if last_run_dir is None:
        print("ERROR: no run could be processed")
        return 1

    with AppendedSection(last_run_dir, f"ratrixcam folder {root}: everything outside the processed runs"):
        for line in problems:
            print(line)
        others = [d for d in find_run_metadata_dirs(root) if d.name not in processed]
        if others:
            print(transfer_folders("ratrixcam run-metadata (not processed)", others, lambda d: metadata_destination(d, destination)))
        videos, n_other = inventory_unlisted(root, listed)
        append_unlisted_rows(last_run_dir, videos)
        report_unlisted(videos, n_other, root)
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="process a folder written by ratrixcam with the videoproc tools")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser(
        "compress",
        help="compress_drive once per run in the folder, each followed by its run metadata folder",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("root", type=Path, help="folder written by ratrixcam: a save_path drive, or a copy of one")
    p.add_argument("destination", type=Path, help="destination root, as for compress_drive")
    compress_drive.add_processing_arguments(p)
    kwargs = vars(parser.parse_args())
    command = kwargs.pop("command")

    if not kwargs["root"].is_dir():
        raise NotADirectoryError(f"{kwargs['root']} is not a valid directory")
    if not kwargs["destination"].is_dir():
        raise NotADirectoryError(f"{kwargs['destination']} is not a valid directory")
    if kwargs["root"].resolve() == kwargs["destination"].resolve():
        raise ValueError("root and destination cannot be the same")

    print("\nconfiguration:")
    pprint(kwargs, sort_dicts=False)
    input("\n[enter] to continue: ")

    sys.exit(compress(**kwargs))
