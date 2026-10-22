#!/usr/bin/env python3 -u
"""Process a folder written by ratrixcam (a save_path drive, or a copy of one) with the videoproc
tools, and carry the run metadata the tools themselves ignore.

    process_ratrixcam_output.py compress <root> <destination> [--studyname S] [--subject_prefix P]
                                         [compress_drive options]
    process_ratrixcam_output.py confirm  <root> <destination> [--studyname S] [--subject_prefix P]
                                         [confirm_and_delete options]

compress: every folder under <root>/ratrixcam_run_metadata/ is a recording run; its manifests/
subfolder holds the per-camera manifests and its camera-config copy gives the study label and
the subject prefix, unless --studyname / --subject_prefix are given, which apply to every run.
compress_drive runs once per recording run with that run's manifests, <root> as the source root
and <destination> as the output root; then, appended to that compression run's own terminal
log, the run's folder is copied whole to <destination>/ratrixcam_run_metadata/<run id>/, never
overwriting a destination file that differs in size. Run folders that could not be processed
are carried the same way afterwards and reported on the terminal only. Files under <root>
outside the run folders are neither inspected nor touched; nothing under <root> is deleted.

confirm: the same run by run, with confirm_and_delete: each run's listed clips are verified
against the destination and, with --delete, the verified ones are deleted after the tool's
own typed confirmation, once per run. Then, appended to that confirm run's own terminal log,
the run's folder is compared byte for byte with its destination copy; with --delete, a
verified folder whose listed clips are all gone from the source is removed after its own
typed confirmation and logged in that run's deletion_log.csv. A folder that differs, or
whose clips remain, is kept and the reason printed.
"""

import argparse
import filecmp
import json
import shutil
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from pprint import pprint
from typing import IO, Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root; the shared module moves next (#26)
from ratrixcam_naming import MANIFEST_SUFFIX, MANIFESTS_DIRNAME, RUN_METADATA_DIRNAME  # noqa: E402

import compress_drive  # noqa: E402
import confirm_and_delete  # noqa: E402


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


def compare_bytes(src_dir: Path, dest_dir: Path) -> tuple[list[str], list[str]]:
    """(files missing at the destination, files whose destination copy differs byte for byte)."""
    missing, differing = [], []
    for rel in sorted(_files(src_dir)):
        target = dest_dir / rel
        if not target.is_file():
            missing.append(rel)
        elif not filecmp.cmp(src_dir / rel, target, shallow=False):
            differing.append(rel)
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


# ----------------------------------------------------------------------------- the two commands

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


def runs_to_process(
    root: Path, studyname: str | None, subject_prefix: str | None, problems: list[str]
) -> list[tuple[RatrixcamRun, str, str]]:
    """The runs under root that can be processed, each with its effective study label and
    subject prefix; the others are explained in `problems`."""
    ready: list[tuple[RatrixcamRun, str, str]] = []
    for run in find_runs(root):
        run_studyname = studyname if studyname is not None else run.studyname
        run_prefix = subject_prefix if subject_prefix is not None else run.subject_prefix
        if run_studyname is None or run_prefix is None:
            problems.append(
                f"ERROR: run {run.run_id} skipped: study label and subject prefix unknown "
                "(see WARNING above; --studyname and --subject_prefix override the config copy)"
            )
        elif not run.manifests:
            problems.append(f"WARNING: run {run.run_id} skipped: no manifest in its {MANIFESTS_DIRNAME}/ folder")
        else:
            ready.append((run, run_studyname, run_prefix))
    return ready


def compress(
    root: Path, destination: Path, studyname: str | None = None, subject_prefix: str | None = None, **options: Any
) -> int:
    """compress_drive once per run found under root, each followed by its run folder; then
    the run folders that could not be processed, reported on the terminal only. `studyname`
    and `subject_prefix`, when given, apply to every run in place of its config copy.
    `options` are compress_drive's processing options as parsed by add_processing_arguments.
    Returns the exit code."""
    if not (root / RUN_METADATA_DIRNAME).is_dir():
        print(f"ERROR: no {RUN_METADATA_DIRNAME} folder under {root}; nothing to process")
        return 1
    problems: list[str] = []
    ready = runs_to_process(root, studyname, subject_prefix, problems)
    processed: set[str] = set()
    for run, run_studyname, run_prefix in ready:
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
    print(f"runs processed: {len(processed)} of {len(ready) + len(problems)}")
    return 1 if problems or not processed else 0


def settle_run_folder(run: RatrixcamRun, outcome: confirm_and_delete.Outcome, destination: Path, delete: bool) -> None:
    """Compare the run folder with its destination copy byte for byte; with `delete`, remove a
    verified folder whose listed clips are all gone from the source, after a typed DELETE,
    and log it in the run's deletion_log.csv."""
    dest_dir = metadata_destination(run.run_dir, destination)
    missing, differing = compare_bytes(run.run_dir, dest_dir)
    if missing or differing:
        print(f"run folder NOT verified at {dest_dir}: {len(missing)} file(s) missing, {len(differing)} differing; kept")
        for rel in missing:
            print(f"    missing: {rel}")
        for rel in differing:
            print(f"    differs: {rel}")
        return
    print(f"run folder verified byte for byte at {dest_dir}")
    if not delete:
        print("run folder kept: --delete not set")
        return
    if outcome.n_remaining:
        print(f"run folder kept: {outcome.n_remaining} of its {outcome.n_listed} listed clip(s) remain at the source")
        return
    print(f"\nAbout to permanently delete the run folder {run.run_dir} ({outcome.n_listed} listed clip(s), all gone from the source)")
    try:
        answer = input("Type DELETE to proceed, anything else to keep it: ")
    except EOFError:
        answer = ""
    if answer.strip() != "DELETE":
        print("Run folder kept.")
        return
    size = sum(_files(run.run_dir).values())
    shutil.rmtree(run.run_dir)
    log = confirm_and_delete.append_deletion_rows(
        outcome.run_dir, [[run.run_dir, dest_dir, size, datetime.now().strftime("%Y-%m-%d %H:%M:%S")]]
    )
    print(f"Run folder deleted; record in {log}")


def confirm(
    root: Path, destination: Path, studyname: str | None = None, subject_prefix: str | None = None, **options: Any
) -> int:
    """confirm_and_delete once per run found under root, on that run's listed clips only;
    then the runs that could not be processed, reported on the terminal only. `options` are
    confirm_and_delete's options as parsed by its add_processing_arguments. Returns the
    exit code."""
    if not (root / RUN_METADATA_DIRNAME).is_dir():
        print(f"ERROR: no {RUN_METADATA_DIRNAME} folder under {root}; nothing to process")
        return 1
    problems: list[str] = []
    ready = runs_to_process(root, studyname, subject_prefix, problems)
    processed = 0
    for run, run_studyname, run_prefix in ready:
        outcome = confirm_and_delete.main(
            source=root, destination=destination, manifest=run.manifests,
            studyname=run_studyname, subject_prefix=run_prefix, **options,
        )
        if outcome is None:
            problems.append(f"ERROR: confirm_and_delete stopped before checking run {run.run_id}")
            continue
        processed += 1
        with AppendedSection(outcome.run_dir, f"ratrixcam run {run.run_id}: run metadata folder"):
            settle_run_folder(run, outcome, destination, delete=bool(options.get("delete")))

    print(f"\n==== ratrixcam folder {root}: runs not processed ====")
    for line in problems:
        print(line)
    if not problems:
        print("none")
    print(f"runs processed: {processed} of {len(ready) + len(problems)}")
    return 1 if problems or not processed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="process a folder written by ratrixcam with the videoproc tools")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser(
        "compress",
        help="compress_drive once per run in the folder, each followed by its run metadata folder",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    c = commands.add_parser(
        "confirm",
        help="confirm_and_delete once per run in the folder, on the clips its manifests list",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for sub in (p, c):
        sub.add_argument("root", type=Path, help="folder written by ratrixcam: a save_path drive, or a copy of one")
        sub.add_argument("destination", type=Path, help="destination root, as for compress_drive")
        sub.add_argument(
            "--studyname", type=str, default=None,
            help="study-name prefix of the destination day folders of every run, in place of each run's config copy",
        )
        sub.add_argument(
            "--subject_prefix", type=str, default=None,
            help="subject prefix of the destination paths of every run, in place of each run's config copy; '' for none",
        )
    compress_drive.add_processing_arguments(p)
    confirm_and_delete.add_processing_arguments(c)
    kwargs = vars(parser.parse_args())
    command = kwargs.pop("command")

    if not kwargs["root"].is_dir():
        raise NotADirectoryError(f"{kwargs['root']} is not a valid directory")
    if not kwargs["destination"].is_dir():
        raise NotADirectoryError(f"{kwargs['destination']} is not a valid directory")
    if kwargs["root"].resolve() == kwargs["destination"].resolve():
        raise ValueError("root and destination cannot be the same")
    if command == "confirm":
        if confirm_and_delete.destination_inside_source(kwargs["root"], kwargs["destination"]):
            raise ValueError("destination must not be the root folder or lie inside it")
        if confirm_and_delete.destination_inside_source(kwargs["root"], kwargs["destination"] / "videoproc_run_metadata"):
            raise ValueError("the run-metadata folder under destination would lie inside the root folder")

    print(f"\nconfiguration ({command}):")
    pprint(kwargs, sort_dicts=False)
    input("\n[enter] to continue: ")

    sys.exit({"compress": compress, "confirm": confirm}[command](**kwargs))
