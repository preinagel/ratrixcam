#!/usr/bin/env python3
"""Where in the picture does the frame-to-frame change (the `noise` column) come from?
Reads the two consecutive auto-exposure frames dimsweep saves per camera
(<loc>_<kind>_auto_pair1.png and _pair2.png). Files only: decodes PNG with zlib and numpy
(no OpenCV), opens no camera.

    uvc/.venv/bin/python -I uvc/pair_analysis.py uvc/results/dim-YYYY-MM-DD-HHMMSS

Per camera, on the luminance Y = 0.299 R + 0.587 G + 0.114 B at full resolution:
- noise: std(Y2 - Y1) / sqrt(2) over all pixels (the probe's figure uses every 2nd pixel
  and the median over many pairs; this is one pair, so it is a cross-check, not the same);
- mean change: mean(Y2 - Y1): a whole-picture brightness change between the frames
  (what lamp flicker would show as);
- texture: mean gradient magnitude of Y1 (how much fine detail the scene has);
- noise in the most and least detailed quarter of the pixels (by local gradient), and
  their ratio: sensor noise depends on brightness, not on detail; changes that sit on
  detail and edges point to compression (or motion);
- 8x8 block-edge ratio: mean |Y2 - Y1| on pixels at the edge of the 8x8 JPEG blocks
  (column or row index 7 or 0 mod 8) divided by the same inside the blocks. JPEG works in
  8x8 blocks, so compression changes concentrate at block edges; sensor noise does not.
- the same two ratios restricted to pixels of middle brightness (Y 60-200), so that
  clipped or black areas, where nothing can change, do not drive them.
Measurements only."""

import os
import struct
import sys
import zlib

try:
    import numpy as np
except ImportError:  # numpy lives in the base interpreter's site-packages
    sys.path.append(os.path.join(sys.base_prefix, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}",
                                 "site-packages"))
    import numpy as np


def read_png(path):
    """8-bit RGB or RGBA (or grey) non-interlaced PNG -> uint8 array (h, w, channels)."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    pos, idat, w = 8, [], None
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace:
                raise ValueError(f"{path}: depth {depth}, interlace {interlace} not supported")
            ch = {0: 1, 2: 3, 6: 4, 4: 2}[ctype]
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), dtype=np.uint8)
    stride = w * ch
    rows = raw.reshape(h, stride + 1)
    out = np.zeros((h, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int32)
    for y in range(h):
        f, line = rows[y, 0], rows[y, 1:].astype(np.int32)
        if f == 0:
            cur = line
        elif f == 2:
            cur = (line + prev) & 0xFF
        else:  # 1 sub, 3 average, 4 paeth: sequential within the row
            cur = line.copy()
            for x in range(stride):
                a = cur[x - ch] if x >= ch else 0
                if f == 1:
                    cur[x] = (cur[x] + a) & 0xFF
                elif f == 3:
                    cur[x] = (cur[x] + ((a + prev[x]) >> 1)) & 0xFF
                else:
                    b, c = prev[x], (prev[x - ch] if x >= ch else 0)
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                    cur[x] = (cur[x] + pred) & 0xFF
        out[y] = cur
        prev = cur
    return out.reshape(h, w, ch)


def luminance(img):
    """OpenCV writes BGR in its PNG? No: PNG is RGB on disk; cv2.imwrite converts BGR->RGB."""
    f = img[..., :3].astype(np.float64)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def stats(y1, y2):
    d = y2 - y1
    gy, gx = np.gradient(y1)
    grad = np.hypot(gx, gy)
    h, w = y1.shape
    edge = np.zeros((h, w), bool)
    edge[:, [x for x in range(w) if x % 8 in (0, 7)]] = True
    edge[[r for r in range(h) if r % 8 in (0, 7)], :] = True
    out = {"noise": d.std() / np.sqrt(2), "mean_change": d.mean(), "texture": grad.mean()}
    for tag, sel in (("all", np.ones_like(edge)), ("mid", (y1 >= 60) & (y1 <= 200) & (y2 >= 60) & (y2 <= 200))):
        if sel.sum() < 1000:
            out[f"{tag}_detail_ratio"] = out[f"{tag}_block_ratio"] = float("nan")
            out[f"{tag}_frac"] = sel.mean()
            continue
        g = grad[sel]
        lo, hi = np.percentile(g, (25, 75))
        dd = d[sel]
        n_hi = dd[g >= hi].std() / np.sqrt(2)
        n_lo = dd[g <= lo].std() / np.sqrt(2)
        out[f"{tag}_noise_detailed"], out[f"{tag}_noise_flat"] = n_hi, n_lo
        out[f"{tag}_detail_ratio"] = n_hi / n_lo if n_lo > 0 else float("inf")
        ad = np.abs(d)
        out[f"{tag}_block_ratio"] = ad[sel & edge].mean() / ad[sel & ~edge].mean()
        out[f"{tag}_frac"] = sel.mean()
    return out


def main(folder):
    rows = []
    for sub in sorted(os.listdir(folder)):
        p = os.path.join(folder, sub)
        if not os.path.isdir(p):
            continue
        pair = sorted(f for f in os.listdir(p) if "_auto_pair" in f)
        if len(pair) != 2:
            continue
        y1, y2 = (luminance(read_png(os.path.join(p, f))) for f in pair)
        rows.append((sub, stats(y1, y2)))
    print("| camera folder | noise | mean change | texture | noise detailed / flat (ratio) | 8x8 block-edge ratio | mid-grey: detail ratio | mid-grey: block-edge ratio | mid-grey share |")
    print("|---|---|---|---|---|---|---|---|---|")
    for sub, s in rows:
        print(f"| {sub} | {s['noise']:.2f} | {s['mean_change']:+.2f} | {s['texture']:.1f} | "
              f"{s.get('all_noise_detailed', float('nan')):.2f} / {s.get('all_noise_flat', float('nan')):.2f} "
              f"({s['all_detail_ratio']:.1f}) | {s['all_block_ratio']:.2f} | {s['mid_detail_ratio']:.1f} | "
              f"{s['mid_block_ratio']:.2f} | {s['mid_frac'] * 100:.0f}% |")


if __name__ == "__main__":
    main(sys.argv[1])
