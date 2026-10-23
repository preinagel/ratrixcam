"""Helper functions needed only by videoproc, not used during recording"""

import sys
from datetime import datetime
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root; the shared module moves next (#26)
from ratrixcam_naming import code_version as _code_version  # noqa: E402


def code_version() -> str:
    """Git commit of the videoproc checkout ('+dirty' if edited), printed at the start of
    every run's terminal log."""
    return _code_version(Path(__file__))


# FourCC ratrixcam writes when it records; any other openable codec has been through
# downstream processing and is treated as "compressed"
RAW_CODEC = "FMP4"


def get_codec_nframes(path: Path):
    """(FourCC string, frame count) of a video, or (None, None) if it cannot be opened."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None, None
    codec = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((codec >> 8 * i) & 0xFF) for i in range(4)])
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return codec, n_frames


def equivalent_call(script: str, positionals: list[object], options: dict[str, object]) -> str:
    """The command line equivalent to a call of a tool's main(), for the run's terminal log:
    True options as flags, lists repeated, None and False omitted."""
    parts = ["python3", script, *(str(p) for p in positionals)]
    for name, value in options.items():
        if value is None or value is False:
            continue
        if value is True:
            parts.append(f"--{name}")
        elif isinstance(value, (list, tuple)):
            parts += [f"--{name} {v}" for v in value]
        else:
            parts.append(f"--{name} {value}")
    return " ".join(parts)


def shorten(path: Path | str, source: Path, destination: Path) -> str:
    """A path for the terminal, with the source or destination root replaced by <source> or
    <destination>; other paths unchanged."""
    text = str(path)
    for root, name in ((str(destination.resolve()), "<destination>"), (str(source.resolve()), "<source>")):
        if text == root:
            return name
        if text.startswith(root + "/"):
            return name + text[len(root):]
    return text


def build_output_path(
    output_root: Path,
    subject_prefix: str,
    studyname: str | None,
    subjID: str,
    view: str,
    clip_start: datetime,
    filename: str,
) -> Path:
    """Where a clip goes in the archive: <output_root>/<subject>/<day folder>/<filename>, with
    subject = subject_prefix + subjID and day folder = [studyname_]subject_view_YYYYMMDD.
    The file keeps its name."""
    subject = f"{subject_prefix}{subjID}"
    day_folder = f"{subject}_{view}_{clip_start:%Y%m%d}"
    if studyname:
        day_folder = f"{studyname}_{day_folder}"
    return Path(output_root) / subject / day_folder / filename
