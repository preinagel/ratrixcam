"""
Builds/caches a histogram of compressed-output creation dates across the real destination
archive (rat*/*/*.mp4), so legacy_audit_backlog.py can compare a legacy folder's existing
outputs against the overall distribution of when compression actually happened -- an
outlier-early creation date may mean the output was produced by an earlier code version or
compression settings we later changed, and is thus a candidate for recompression rather
than a keeper. One-off, motivated entirely by the legacy backlog cleanup project.
"""

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path


def file_created_date(path: Path) -> str:
    stat = path.stat()
    created = getattr(stat, "st_birthtime", stat.st_mtime)
    return datetime.fromtimestamp(created).strftime("%Y-%m-%d")


def build_datehist(archive_root: Path, pattern: str = "rat*/*/*.mp4") -> Counter:
    counts: Counter = Counter()
    for path in archive_root.glob(pattern):
        counts[file_created_date(path)] += 1
    return counts


def load_datehist(cache_path: Path) -> Counter:
    counts: Counter = Counter()
    with open(cache_path, newline="") as f:
        for row in csv.DictReader(f):
            counts[row["date"]] = int(row["count"])
    return counts


def save_datehist(counts: Counter, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "count"])
        for date in sorted(counts):
            writer.writerow([date, counts[date]])


def load_or_build_datehist(archive_root: Path, cache_path: Path, refresh: bool = False) -> Counter:
    if cache_path.exists() and not refresh:
        return load_datehist(cache_path)
    counts = build_datehist(archive_root)
    save_datehist(counts, cache_path)
    return counts
