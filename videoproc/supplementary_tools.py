"""Early-draft helpers for characterising and calibrating motion-detection criteria from the
motion_timeseries.csv files that compress_drive writes. Not a finished tool and not used by
the pipeline; kept so the analysis behind the lab's threshold can be repeated or adapted
(e.g. for a supplementary figure). Interfaces may change."""

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

import detect_motion
from util import parse_filenames


def read_motion_timeseries(run_dir: Path) -> dict[str, np.ndarray]:
    """{clip filename: motion score per sampled frame} for one compression run. Accepts the
    compact format (clip_filename, motion_values) and the older long format
    (clip_filename, sample_index, motion_value)."""
    path = Path(run_dir) / "motion_timeseries.csv"
    series: dict[str, list[float]] = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header[1] == "motion_values":
            for clip, values in reader:
                series[clip] = [float(x) for x in values.split()]
        else:
            for clip, _index, value in reader:
                series[clip].append(float(value))
    return {clip: np.asarray(values) for clip, values in series.items()}


def sampled_frame_numbers(n_samples: int, parameters: dict = detect_motion.PARAMETERS) -> np.ndarray:
    """0-based video frame number of each sampled score, given the detector parameters."""
    skip, every = parameters["n_frames_to_skip"], parameters["sample_every"]
    first = (skip // every + 1) * every - 1
    return first + every * np.arange(n_samples)


def has_motion(values: np.ndarray, threshold: float, percentile: float = 99.9) -> bool:
    """The classification rule compress_drive applies."""
    return bool(np.percentile(values, percentile) >= threshold)


def frames_needed(n_samples: int, percentile: float = 99.9) -> int:
    """How many sampled frames must reach the threshold for has_motion to be true regardless
    of their size (numpy's linear interpolation between order statistics)."""
    rank = (n_samples - 1) * percentile / 100
    return n_samples - 1 - int(np.floor(rank)) + 1


def pooled_score_histograms(run_dirs, threshold: float, percentile: float = 99.9, views=None, edges=None):
    """Pool every sampled score over clips, split by (view, 'motion' | 'no-motion').
    Returns (edges, {(view, class): counts}, {(view, class): n_clips}); counts[0] is the
    number of scores exactly 0, counts[i] the number in [edges[i], edges[i+1])."""
    if edges is None:
        edges = np.concatenate([[0.0], np.logspace(-5, 0, 101)])
    hist = defaultdict(lambda: np.zeros(len(edges), dtype=np.int64))
    n_clips = defaultdict(int)
    for run_dir in run_dirs:
        for clip, values in read_motion_timeseries(run_dir).items():
            parsed = parse_filenames(Path(clip))
            if parsed is None or (views is not None and parsed[1] not in views):
                continue
            key = (parsed[1], "motion" if has_motion(values, threshold, percentile) else "no-motion")
            idx = np.searchsorted(edges, values, side="right") - 1
            idx[values == 0] = 0
            idx[(values > 0) & (idx == 0)] = 1
            hist[key] += np.bincount(idx, minlength=len(edges))[: len(edges)]
            n_clips[key] += 1
    return edges, dict(hist), dict(n_clips)


def plot_score_distributions(edges, hist, n_clips, threshold, cumulative=True, out_path=None, title=None):
    """Per-view curves, solid for motion clips and dashed for no-motion clips, on log axes.
    cumulative=True: fraction of sampled frames with score >= s. cumulative=False: fraction
    of sampled frames per log-spaced bin, with the exactly-zero fraction as a marker at the
    left edge. Returns the matplotlib figure."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6.5))
    views = sorted({view for view, _ in hist})
    colors = {view: f"C{i}" for i, view in enumerate(views)}
    centers = np.sqrt(edges[1:-1] * edges[2:])
    for view in views:
        for cls, style, marker in (("motion", "-", "o"), ("no-motion", "--", "s")):
            h = hist.get((view, cls))
            if h is None or h.sum() == 0:
                continue
            total = h.sum()
            label = f"{view} {cls} ({n_clips[(view, cls)]} clips)"
            if cumulative:
                ax.plot(edges[1:], (total - np.cumsum(h)[:-1]) / total, style, color=colors[view], lw=2, label=label)
            else:
                ax.plot(centers, h[1:-1] / total, style, color=colors[view], lw=2, label=label)
                ax.plot(
                    [edges[1] * 0.6], [h[0] / total], marker, color=colors[view], ms=8,
                    mfc=colors[view] if cls == "motion" else "white",
                )
    ax.axvline(threshold, color="k", lw=1, ls=":")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("motion score (fraction of pixels changed in a sampled frame)")
    ax.set_ylabel("fraction of sampled frames with score >= s" if cumulative else "fraction of sampled frames in bin")
    ax.set_title(
        title
        or (
            "Motion-score distributions, pooled per view; solid = motion clips, dashed = no-motion clips"
            + ("" if cumulative else "; markers = fraction scoring exactly 0")
        )
    )
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    if out_path:
        fig.savefig(out_path, dpi=150)
    return fig
