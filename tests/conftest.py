"""Session-scoped fixtures shared by the videoproc test suite: synthetic clips, and the
layout/run state that both test_compress_drive.py and test_confirm_and_delete.py build on.

Everything here runs under pytest's tmp_path tree -- nothing is read from or written to
/Volumes/rlab2 or any other real drive.
"""

import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from helpers import (
    HOM_TIME, T11_DATE, T11_TIME, TIMES, VIDEOPROC_DIR,
    dest_path, latest_run_dir, place, read_log, run_compress_drive, session_folder, source_path,
)

sys.path.insert(0, str(VIDEOPROC_DIR))
from util import RAW_CODEC, get_codec_nframes  # noqa: E402

WIDTH, HEIGHT, FPS = 640, 480, 30
SQUARE = 60


def _write_raw_clip(path: Path, n_frames: int) -> None:
    """A moving solid square over a static textured (random-noise) background, written
    with the FMP4 fourcc -- the FourCC ratrixcam itself records with."""
    fourcc = cv2.VideoWriter_fourcc(*"FMP4")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError(f"cv2.VideoWriter could not open {path} for writing")
    rng = np.random.default_rng(42)
    background = rng.integers(0, 255, (HEIGHT, WIDTH, 3), dtype=np.uint8)
    for i in range(n_frames):
        frame = background.copy()
        x = (i * 4) % (WIDTH - SQUARE)
        y = (i * 3) % (HEIGHT - SQUARE)
        frame[y : y + SQUARE, x : x + SQUARE] = (0, 0, 255)
        writer.write(frame)
    writer.release()


def _compress(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", str(dst)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def _truncated_raw_copy(src: Path, dst: Path, keep_fraction: float = 0.8) -> None:
    """A raw-codec copy of `src` whose frame-count header stays intact (moov moved to the
    front first) but whose last (1 - keep_fraction) of frame data is physically missing --
    OpenCV reports the original frame count, but re-encoding it yields fewer frames."""
    faststart = dst.with_suffix(".faststart.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-c:v", "mpeg4", "-movflags", "+faststart", str(faststart)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    data = faststart.read_bytes()
    dst.write_bytes(data[: int(len(data) * keep_fraction)])
    faststart.unlink()


def _zero_bytes_copy(src: Path, dst: Path, offset_fraction: float, n_bytes: int) -> None:
    data = bytearray(src.read_bytes())
    offset = int(len(data) * offset_fraction)
    for i in range(offset, min(offset + n_bytes, len(data))):
        data[i] = 0
    dst.write_bytes(bytes(data))


@pytest.fixture(scope="session")
def clips(tmp_path_factory) -> dict:
    """Paths to the fixed set of synthetic source clips used across the suite, verified
    against util.get_codec_nframes before any test runs."""
    d = tmp_path_factory.mktemp("clips")

    r30 = d / "R30.mp4"
    _write_raw_clip(r30, 900)
    r20 = d / "R20.mp4"
    _write_raw_clip(r20, 600)

    c30 = d / "C30.mp4"
    _compress(r30, c30)
    c20 = d / "C20.mp4"
    _compress(r20, c20)

    junk = d / "junk.mp4"
    junk.write_bytes(r30.read_bytes()[: 2 * 1024 * 1024])

    r30_corrupt = d / "R30_corrupt.mp4"
    _truncated_raw_copy(r30, r30_corrupt)

    c30_corrupt = d / "C30_corrupt.mp4"
    _zero_bytes_copy(c30, c30_corrupt, offset_fraction=0.40, n_bytes=256 * 1024)

    paths = {
        "R30": r30, "R20": r20, "C30": c30, "C20": c20, "junk": junk,
        "R30_corrupt": r30_corrupt, "C30_corrupt": c30_corrupt,
    }

    for name in ("R30", "R20", "R30_corrupt"):
        codec, _ = get_codec_nframes(paths[name])
        assert codec == RAW_CODEC, f"{name}: expected codec {RAW_CODEC!r}, got {codec!r}"
    for name in ("C30", "C20", "C30_corrupt"):
        codec, _ = get_codec_nframes(paths[name])
        assert codec in ("h264", "avc1"), f"{name}: expected h264/avc1, got {codec!r}"
    codec, n_frames = get_codec_nframes(paths["junk"])
    assert codec is None and n_frames is None, f"junk: expected unopenable, got ({codec!r}, {n_frames!r})"

    _, r30_n = get_codec_nframes(r30)
    _, r20_n = get_codec_nframes(r20)
    assert r30_n == 900 and r20_n == 600 and r30_n != r20_n

    return paths


@pytest.fixture(scope="session")
def layout(tmp_path_factory, clips):
    """Builds the source/destination tree for compress_drive run 1 (t01-t11, the misspelt
    camera view, the pattern-mismatch strays, and the non-.mp4 file)."""
    source_root = tmp_path_factory.mktemp("cd_source")
    dest_root = tmp_path_factory.mktemp("cd_dest")

    sources = {
        "t01": clips["R30"], "t02": clips["C30"], "t03": clips["R30"], "t04": clips["R30"],
        "t05": clips["R30"], "t06": clips["junk"], "t07": clips["junk"], "t08": clips["junk"],
        "t09": clips["junk"], "t10": clips["R30_corrupt"],
    }
    for key, clip in sources.items():
        place(clip, source_path(source_root, TIMES[key]))

    predest = {
        "t03": clips["R30"], "t04": clips["C20"], "t05": clips["junk"],
        "t07": clips["junk"], "t08": clips["C30"], "t09": clips["R30"],
    }
    for key, clip in predest.items():
        place(clip, dest_path(dest_root, TIMES[key]))

    place(clips["R30"], session_folder(source_root) / "weird_clip.mp4")

    place(clips["C30"], source_path(source_root, T11_TIME, date=T11_DATE))
    t11_dest_dir = dest_path(dest_root, T11_TIME, date=T11_DATE).parent
    t11_dest_dir.mkdir(parents=True)
    t11_dest_dir.chmod(0o555)

    place(clips["R30"], source_path(source_root, HOM_TIME, view="hom"))

    stray_dir = source_root / "not_a_session_folder"
    stray_dir.mkdir()
    place(clips["R30"], stray_dir / "stray_one.mp4")
    place(clips["R30"], stray_dir / "stray_two.mp4")

    (source_root / "notes.txt").write_text("not a video\n")

    return source_root, dest_root, t11_dest_dir


@pytest.fixture(scope="session")
def run1(layout):
    source_root, dest_root, _ = layout
    result = run_compress_drive(source_root, dest_root, extra_args=[
        "--n_threads", "4", "--motion_detection", "--motion_threshold", "0.001",
    ])
    assert result.returncode == 0, result.stderr
    run_dir = latest_run_dir(dest_root, "videoproc_")
    return {"result": result, "run_dir": run_dir, "log": read_log(run_dir)}


@pytest.fixture(scope="session")
def run2(run1, layout):
    source_root, dest_root, t11_dest_dir = layout
    t11_dest_dir.chmod(0o755)  # restore write access so run2's t11 case can succeed
    result = run_compress_drive(source_root, dest_root, extra_args=[
        "--n_threads", "4", "--motion_detection", "--motion_threshold", "0.001", "--overwrite_raw",
    ])
    assert result.returncode == 0, result.stderr
    run_dir = latest_run_dir(dest_root, "videoproc_")
    return {"result": result, "run_dir": run_dir, "log": read_log(run_dir)}


@pytest.fixture(scope="session")
def run3(run2, layout):
    source_root, dest_root, _ = layout
    result = run_compress_drive(source_root, dest_root, extra_args=[
        "--n_threads", "4", "--motion_detection", "--motion_threshold", "0.001", "--overwrite_compressed",
    ])
    assert result.returncode == 0, result.stderr
    run_dir = latest_run_dir(dest_root, "videoproc_")
    return {"result": result, "run_dir": run_dir, "log": read_log(run_dir)}
