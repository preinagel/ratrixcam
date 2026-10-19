"""Naming, path and video-probe helpers shared by the videoproc tools.

Intended to become the module shared with ratrixcam, so that both tools build and parse
file and folder names with one convention."""

import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root; the shared module moves next (#26)
from ratrixcam_naming import (  # noqa: E402
    build_output_path,
    code_version as _code_version,
    parse_filenames,
    parse_recording_datetime,
)


def code_version() -> str:
    """Git commit of the videoproc checkout ('+dirty' if edited), recorded in every run's
    config.json so settings hardwired in code can be recovered later."""
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
