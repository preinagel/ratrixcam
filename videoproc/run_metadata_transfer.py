"""Carry ratrixcam run-metadata folders (config copy, terminal log, stills, notes --
see ratrixcam_naming.py) from a source tree to the archive, and verify them there (GitHub #34)."""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root; the shared module moves next (#26)
from ratrixcam_naming import RUN_METADATA_DIRNAME  # noqa: E402


def find_run_metadata_dirs(source_root: Path) -> list[Path]:
    """Every run folder under any `ratrixcam_run_metadata/` directory in the source tree."""
    runs = []
    for container in source_root.rglob(RUN_METADATA_DIRNAME):
        if container.is_dir():
            runs.extend(p for p in sorted(container.iterdir()) if p.is_dir())
    return runs


def is_run_metadata_path(path: Path, source_root: Path) -> bool:
    return RUN_METADATA_DIRNAME in path.relative_to(source_root).parts


def destination_for(run_dir: Path, destination_root: Path) -> Path:
    return destination_root / RUN_METADATA_DIRNAME / run_dir.name


def _files(root: Path) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def compare(run_dir: Path, dest_dir: Path) -> tuple[list[str], list[str]]:
    """(files missing at the destination, files present with a different size)."""
    src, dst = _files(run_dir), (_files(dest_dir) if dest_dir.exists() else {})
    missing = sorted(f for f in src if f not in dst)
    differing = sorted(f for f in src if f in dst and dst[f] != src[f])
    return missing, differing


def transfer(run_dir: Path, dest_dir: Path) -> tuple[int, list[str]]:
    """Copy the files the destination lacks; leave files that already exist alone. Returns
    (number copied, files whose destination copy differs in size -- never overwritten)."""
    missing, differing = compare(run_dir, dest_dir)
    for rel in missing:
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_dir / rel, target)
    return len(missing), differing


def total_size(run_dir: Path) -> int:
    return sum(_files(run_dir).values())


def remove(run_dir: Path) -> None:
    shutil.rmtree(run_dir)
    container = run_dir.parent
    if container.name == RUN_METADATA_DIRNAME and not any(container.iterdir()):
        container.rmdir()
