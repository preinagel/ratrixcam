#!/usr/bin/env python3

import argparse
from pathlib import Path

import cv2 as cv
import numpy as np


def play_frame(frame):
    """display frame, exit if 'q' is pressed"""
    cv.imshow("motion detection", frame)
    return cv.waitKey(1) & 0xFF == ord("q")


def main(path, play_video):
    cap = cv.VideoCapture(path)

    # initialize background subtractor and kernel
    mog = cv.createBackgroundSubtractorMOG2(
        history=600,  # Number of frames that affect the background model
        varThreshold=16,  # Sensitivity threshold
        detectShadows=False,  # Increases speed
    )
    kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (3, 3))

    # motion detection
    motion_by_frame = []
    n_frames_to_skip = 30
    n_frames = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        n_frames += 1

        # skip every 10 frames (1/3 sec) for faster processing
        if n_frames % 10 != 0:
            continue

        # reduce frame resolution for faster processing
        frame = cv.resize(frame, (frame.shape[1] // 3, frame.shape[0] // 3))

        # skip frames for MOG stability
        if n_frames <= n_frames_to_skip:
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
