#!/usr/bin/env python3
"""Regenerate <destination>/videoproc_run_metadata/index.csv from the run folders themselves.

Every run folder's manifest.csv carries what its index line summarizes, so the index can be
rebuilt whenever a line is missing (a run could not get the index lock) or duplicated.
The existing index.csv is kept as index.csv.bak."""

import argparse
import csv
import shutil
from pathlib import Path

from compress_drive import INDEX_FIELDS, _file_lock, index_row


def rebuild(metadata_root: Path) -> int:
    rows = []
    # chronological: run ids are <kind>_<YYYYMMDD>_<HHMMSS>_<random>
    run_dirs = sorted((p for p in metadata_root.iterdir() if p.is_dir()), key=lambda p: p.name.split("_", 1)[-1])
    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.csv"
        if not manifest_path.exists():
            continue
        with open(manifest_path, newline="") as f:
            manifest = [
                {**r, "file_count": int(r["file_count"])} for r in csv.DictReader(f)
            ]
        rows.append(index_row(run_dir.name, manifest))

    index_path = metadata_root / "index.csv"
    with _file_lock(index_path.with_suffix(".csv.lock")):
        if index_path.exists():
            shutil.copy2(index_path, index_path.with_suffix(".csv.bak"))
        with open(index_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="rebuild videoproc_run_metadata/index.csv from the run folders")
    parser.add_argument("destination", type=Path, help="destination root given to compress_drive / confirm_and_delete")
    args = parser.parse_args()
    root = args.destination / "videoproc_run_metadata"
    if not root.is_dir():
        raise NotADirectoryError(f"{root} not found")
    n = rebuild(root)
    print(f"index.csv rebuilt from {n} run folder(s); previous copy saved as index.csv.bak")
