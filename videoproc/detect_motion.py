#!/usr/bin/env python3

import argparse
from pathlib import Path

import cv2 as cv
import numpy as np


def play_frame(frame):
    """display frame, exit if 'q' is pressed"""
    cv.imshow("motion detection", frame)
    return cv.waitKey(1) & 0xFF == ord("q")


N_FRAMES_TO_SKIP = 30  # frames used only to stabilize the background model
SAMPLE_EVERY = 10  # analyse every Nth frame
RESIZE_DIVISOR = 3  # frame dimensions divided by this before analysis
MOG2_HISTORY = 600  # number of frames that affect the background model
MOG2_VAR_THRESHOLD = 16  # sensitivity threshold
OPENING_KERNEL = (3, 3)

PARAMETERS = {
    "n_frames_to_skip": N_FRAMES_TO_SKIP,
    "sample_every": SAMPLE_EVERY,
    "resize_divisor": RESIZE_DIVISOR,
    "mog2_history": MOG2_HISTORY,
    "mog2_var_threshold": MOG2_VAR_THRESHOLD,
    "opening_kernel": OPENING_KERNEL,
}


def main(path, play_video):
    cap = cv.VideoCapture(path)

    mog = cv.createBackgroundSubtractorMOG2(
        history=MOG2_HISTORY, varThreshold=MOG2_VAR_THRESHOLD, detectShadows=False
    )
    kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, OPENING_KERNEL)

    motion_by_frame = []
    n_frames = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        n_frames += 1

        if n_frames % SAMPLE_EVERY != 0:
            continue

        frame = cv.resize(frame, (frame.shape[1] // RESIZE_DIVISOR, frame.shape[0] // RESIZE_DIVISOR))

        # skip frames for MOG stability
        if n_frames <= N_FRAMES_TO_SKIP:
            mog.apply(frame)
            continue

        # (1) background subtraction using MOG
        fg_mask = mog.apply(frame)

        # (2) morphological opening to remove noise
        fg_mask_filt = cv.morphologyEx(fg_mask, cv.MORPH_OPEN, kernel)

        # motion by frame
        motion_by_frame.append(cv.countNonZero(fg_mask_filt) / fg_mask_filt.size)

        # display frame if show_frames is enabled
        if play_video and play_frame(fg_mask_filt):
            break

    cap.release()

    return np.asarray(motion_by_frame)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect motion in video.")
    parser.add_argument("path", type=Path, help="Path to the video file.")
    parser.add_argument("--play_video", action="store_true", help="Play video during processing (press 'q' to exit).")
    args = parser.parse_args()

    motion_by_frame = main(args.path, args.play_video)
    print(f"motion-99-perc: {np.percentile(motion_by_frame, 99)}")
