#!/usr/bin/env python3

import argparse
from pathlib import Path
from typing import cast

import cv2 as cv
import numpy as np
import numpy.typing as npt
from cv2.typing import MatLike

Scores = npt.NDArray[np.float64]  # one motion score per sampled frame


def play_frame(frame: MatLike) -> bool:
    """display frame, exit if 'q' is pressed"""
    cv.imshow("motion detection", frame)
    return cv.waitKey(1) & 0xFF == ord("q")


N_FRAMES_TO_SKIP = 30  # frames used only to stabilize the background model
SAMPLE_EVERY = 10  # analyse every Nth frame
RESIZE_DIVISOR = 3  # frame dimensions divided by this before analysis
MOG2_HISTORY = 600  # number of frames that affect the background model
MOG2_VAR_THRESHOLD = 16  # sensitivity threshold
OPENING_KERNEL = (3, 3)


def motion_scores(
    path: Path | str, play_video: bool, return_peak: bool = False
) -> Scores | tuple[Scores, MatLike | None, int | None]:
    """Per-sampled-frame motion scores for a clip. With return_peak, also returns the
    full-resolution frame that scored highest and its 0-based frame number, for a still
    image of the moment most likely to show an animal."""
    cap = cv.VideoCapture(str(path))

    background_model = cv.createBackgroundSubtractorMOG2(
        history=MOG2_HISTORY, varThreshold=MOG2_VAR_THRESHOLD, detectShadows=False
    )
    kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, OPENING_KERNEL)

    motion_by_frame: list[float] = []
    frames_read = 0
    peak_value, peak_frame, peak_frame_number = -1.0, None, None
    peak_frame: MatLike | None
    peak_frame_number: int | None

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frames_read += 1

        if frames_read % SAMPLE_EVERY != 0:
            continue

        full_frame = frame
        frame = cv.resize(frame, (frame.shape[1] // RESIZE_DIVISOR, frame.shape[0] // RESIZE_DIVISOR))  # pyright: ignore[reportAny]  # OpenCV frames are untyped arrays

        # the first frames only feed the background model
        if frames_read <= N_FRAMES_TO_SKIP:
            _ = background_model.apply(frame)
            continue

        # (1) background subtraction
        fg_mask = background_model.apply(frame)

        # (2) morphological opening to remove noise
        fg_mask_filt = cv.morphologyEx(fg_mask, cv.MORPH_OPEN, kernel)

        # (3) the score: the fraction of the mask that moved
        value = cv.countNonZero(fg_mask_filt) / fg_mask_filt.size
        motion_by_frame.append(value)
        if return_peak and value > peak_value:
            peak_value, peak_frame, peak_frame_number = value, full_frame.copy(), frames_read - 1

        if play_video and play_frame(fg_mask_filt):
            break

    cap.release()

    scores = np.asarray(motion_by_frame, dtype=np.float64)
    if return_peak:
        return scores, peak_frame, peak_frame_number
    return scores


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect motion in video.")
    _ = parser.add_argument("path", type=Path, help="Path to the video file.")
    _ = parser.add_argument("--play_video", action="store_true", help="Show the motion mask while processing (press 'q' to exit).")
    args = parser.parse_args()

    result = motion_scores(cast(Path, args.path), cast(bool, args.play_video))
    motion_by_frame = result[0] if isinstance(result, tuple) else result
    print(f"motion-99-perc: {np.percentile(motion_by_frame, 99)}")
