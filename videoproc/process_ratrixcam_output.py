#!/usr/bin/env python3 -u
"""Process a folder written by ratrixcam (a save_path drive, or a copy of one) with the videoproc
tools, and carry the run metadata the tools themselves ignore.

    process_ratrixcam_output.py compress <root> <destination> [--studyname S] [--subject_prefix P]
                                         [compress_drive options]

compress: every folder under <root>/ratrixcam_run_metadata/ is a recording run; its manifests/
subfolder holds the per-camera manifests and its camera-config copy gives the study label and
the subject prefix, unless --studyname / --subject_prefix are given, which apply to every run.
compress_drive runs once per recording run with that run's manifests, <root> as the source root
and <destination> as the output root; then, appended to that compression run's own terminal
log, the run's folder is copied whole to <destination>/ratrixcam_run_metadata/<run id>/, never
overwriting a destination file that differs in size. Run folders that could not be processed
are carried the same way afterwards and reported on the terminal only. Files under <root>
outside the run folders are neither inspected nor touched; nothing under <root> is deleted.
"""

import argparse
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


def compress(
    root: Path, destination: Path, studyname: str | None = None, subject_prefix: str | None = None, **options: Any
) -> int:
    """compress_drive once per run found under root, each followed by its run folder; then
    the run folders that could not be processed, reported on the terminal only. `studyname`
    and `subject_prefix`, when given, apply to every run in place of its config copy.
    `options` are compress_drive's processing options as parsed by add_processing_arguments.
    Returns the exit code."""
    runs = find_runs(root)
    if not runs:
        print(f"ERROR: no run folder under {root / RUN_METADATA_DIRNAME}; nothing to process")
        return 1

    problems: list[str] = []
    processed: set[str] = set()
    for run in runs:
        run_studyname = studyname if studyname is not None else run.studyname
        run_prefix = subject_prefix if subject_prefix is not None else run.subject_prefix
        if run_studyname is None or run_prefix is None:
            problems.append(
                f"ERROR: run {run.run_id} skipped: study label and subject prefix unknown "
                "(see WARNING above; --studyname and --subject_prefix override the config copy)"
            )
            continue
        if not run.manifests:
            problems.append(f"WARNING: run {run.run_id} skipped: no manifest in its {MANIFESTS_DIRNAME}/ folder")
            continue
        run_dir = compress_drive.main(
            input=root, output=destination, manifest=run.manifests,
            studyname=run_studyname, subject_prefix=run_prefix, **options,
        )
        if run_dir is None:
            problems.append(f"ERROR: compress_drive stopped before processing run {run.run_id}")
            continue
        processed.add(run.run_id)
        with AppendedSection(run_dir, f"ratrixcam run {run.run_id}: run metadata folder"):
            print(transfer_folders("ratrixcam run-metadata", [run.run_dir], lambda d: metadata_destination(d, destination)))

    # terminal only: what could not be processed, and the run folders carried regardless
    print(f"\n==== ratrixcam folder {root}: runs not processed ====")
    for line in problems:
        print(line)
    others = [d for d in find_run_metadata_dirs(root) if d.name not in processed]
    if others:
        print(transfer_folders("ratrixcam run-metadata (not processed)", others, lambda d: metadata_destination(d, destination)))
    if not problems:
        print("none")
    print(f"runs processed: {len(processed)} of {len(runs)}")
    return 1 if problems or not processed else 0


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
    p.add_argument(
        "--studyname", type=str, default=None,
        help="study-name prefix for the destination day folders of every run, in place of each run's config copy",
    )
    p.add_argument(
        "--subject_prefix", type=str, default=None,
        help="subject prefix for the destination paths of every run, in place of each run's config copy; '' for none",
    )
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
