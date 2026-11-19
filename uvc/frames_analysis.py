#!/usr/bin/env python3
"""Analyse the recorder's per-frame records (<clip>_frames.csv: frame, arrived, waited_s,
worked_s) for periodic frame drops. Standard library only; reads files, touches no camera.

    uvc/.venv/bin/python -I uvc/frames_analysis.py for_uvc_developer

For each camera folder (Test_<subject>_<view>_<date>) and each clip: the camera's clock from
the median arrival interval, every arrival gap of about two intervals (one missing frame)
or more, the spacing in frames between consecutive single-frame gaps, and whether the loop
was idle at the gap (worked_s small, waited_s about the whole gap). Clips are analysed
separately (each starts at frame 0); the totals are per camera, never pooled across cameras.
"""

import csv
import os
import statistics
import sys
from collections import Counter
from datetime import datetime


def load(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append((int(r["frame"]), datetime.fromisoformat(r["arrived"]).timestamp(),
                         float(r["waited_s"]), float(r["worked_s"])))
    return rows


def analyse_clip(rows):
    times = [r[1] for r in rows]
    dts = [b - a for a, b in zip(times, times[1:])]
    if len(dts) < 10:
        return None
    med = statistics.median(dts)
    normal = [d for d in dts if d < 1.5 * med]
    clock = statistics.mean(normal)  # mean of undisturbed intervals: the camera's frame period
    singles, multis, idle = [], [], 0
    for i, d in enumerate(dts):
        if d >= 1.5 * med:
            frame = rows[i + 1][0]
            missing = round(d / clock) - 1
            (singles if missing == 1 else multis).append((frame, d, missing))
            waited, worked = rows[i + 1][2], rows[i + 1][3]
            if worked < 0.25 * d and waited > 0.6 * d:
                idle += 1
    spacing = [b[0] - a[0] for a, b in zip(singles, singles[1:])]
    duration = times[-1] - times[0]
    return {"frames": len(rows), "seconds": duration, "clock_ms": clock * 1000,
            "clock_fps": 1 / clock, "delivered_fps": (len(rows) - 1) / duration,
            "singles": singles, "multis": multis, "idle_gaps": idle, "spacing": spacing,
            "gap_ms": [round(g[1] * 1000, 1) for g in singles]}


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "for_uvc_developer"
    folders = sorted(d for d in os.listdir(root)
                     if d.startswith("Test_") and os.path.isdir(os.path.join(root, d)))
    print(f"frame records under {root}: {len(folders)} camera folders")
    print("view   clips  frames   s    clock_fps clock_ms delivered_fps  1-frame gaps/min  multi-gaps  "
          "idle-at-gap  spacing (frames between 1-frame gaps): most common")
    for folder in folders:
        view = folder.split("_")[2] if folder.count("_") >= 3 else folder
        path = os.path.join(root, folder)
        clips = sorted(f for f in os.listdir(path) if f.endswith("_frames.csv"))
        res = [r for r in (analyse_clip(load(os.path.join(path, c))) for c in clips) if r]
        frames = sum(r["frames"] for r in res)
        secs = sum(r["seconds"] for r in res)
        singles = sum(len(r["singles"]) for r in res)
        multis = sum(len(r["multis"]) for r in res)
        idle = sum(r["idle_gaps"] for r in res)
        gaps = singles + multis
        clock = statistics.mean(r["clock_fps"] for r in res)
        clock_ms = statistics.mean(r["clock_ms"] for r in res)
        delivered = (frames - len(res)) / secs
        spacing = Counter(s for r in res for s in r["spacing"])
        top = ", ".join(f"{k}x{v}" for k, v in spacing.most_common(4)) or "-"
        print(f"{view:6s} {len(res):5d} {frames:7d} {secs:5.0f}   {clock:7.3f}  {clock_ms:7.3f}   {delivered:7.3f}"
              f"        {singles / secs * 60:6.1f}          {multis:5d}      {idle}/{gaps}       {top}")
    print("\nper clip (view, clip start, 1-frame gaps, spacings, gap lengths ms):")
    for folder in folders:
        view = folder.split("_")[2] if folder.count("_") >= 3 else folder
        path = os.path.join(root, folder)
        for c in sorted(f for f in os.listdir(path) if f.endswith("_frames.csv")):
            r = analyse_clip(load(os.path.join(path, c)))
            if not r:
                continue
            start = c.split("_")[3]
            sp = sorted(Counter(r["spacing"]).items())
            gl = sorted(r["gap_ms"])
            gl_txt = f"{gl[0]}-{gl[-1]}" if gl else "-"
            print(f"  {view} {start}: frames {r['frames']}, clock {r['clock_fps']:.3f} fps, 1-frame gaps {len(r['singles'])}, "
                  f"multi {len(r['multis'])}, spacings {dict(sp)}, gap ms {gl_txt}")


if __name__ == "__main__":
    main()
