"""Render several dodge & burn band settings side by side in one image.

Each variant is a list of bands "LO:HI:GAIN[:TAPER]" (feature sizes in mm, see dodge_burn.py): sizes inside a band
are corrected with that band's gain, sizes in no band are left alone.
Every cell shows [after | change x8 (gray = no change)] for a crop around the subject's face.

Usage: uv run python -m shp_photocraft_tools.modules.dnb.compare_variants IMAGE [--out FILE]
"""
import argparse
from pathlib import Path

import cv2
import numpy as np

from . import combine_scale as C
from . import dodge_burn as D

VARIANTS = [  # each: list of "LO:HI:GAIN[:TAPER]" bands (mm)
    ["1.5:50:1.0"],
    ["1.5:40:1.0", "40:80:0.3"],
    ["1.5:40:1.0", "40:80:0.6"],
    ["1.5:10:1.0", "10:40:0.5", "40:80:0.2"],
    ["1.5:6:0.5", "6:40:1.0", "40:80:0.3"],
    ["3:40:1.0", "40:80:0.3"],
    ["1.5:40:1.5", "40:80:0.5"],
    ["1.5:40:1.0", "80:150:0.3"],
]
COMMON = dict(strength=1.0, edge=0.12, tau=1.0, max_ev=1.0, guided_up=True,
              shadows=(0.08, 0.25), highlights=(0.80, 0.95))
PANEL = 480


def label(img, text, sub=None):
    bar = np.full((52 if sub else 34, img.shape[1], 3), 24, np.uint8)
    cv2.putText(bar, text, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    if sub:
        cv2.putText(bar, sub, (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
    return np.vstack([bar, img])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out", default="out12/variants.jpg")
    a = ap.parse_args()
    path = Path(a.image)
    raw = cv2.imread(str(path), cv2.IMREAD_COLOR_BGR)
    img = raw.astype(np.float32) / 255.0
    h, w = img.shape[:2]
    S, conf, desc, centre = C.target_scale(path)
    print(f"S = {S:.0f} px ({conf}); {desc}")
    cx, cy = centre
    half = int(min(2.5 * S, min(w, h) / 2))
    x0, y0 = min(max(0, int(cx) - half), w - 2 * half), min(max(0, int(cy) - half), h - 2 * half)
    sl = (slice(y0, y0 + 2 * half), slice(x0, x0 + 2 * half))

    Y = D.luminance_linear(img).astype(np.float32)
    cb = D.to_srgb(Y).astype(np.float32)
    u8 = lambda x: np.round(np.clip(x, 0, 1) * 255).astype(np.uint8)
    rs = lambda x: cv2.resize(x, (PANEL, PANEL), interpolation=cv2.INTER_AREA)
    gray = lambda: rs(np.full((2 * half, 2 * half, 3), 128, np.uint8))

    cells = [np.hstack([rs(u8(img[sl])), gray()])]
    cells[0] = label(cells[0], "original", "no correction")
    for spec in VARIANTS:
        bands = [D.parse_band(b) for b in spec]
        change, diag = D.compute_change_band(Y, S, bands=bands, **COMMON)
        target = D.to_srgb(Y * np.exp(change)).astype(np.float32)
        cs = D.invert_softlight(cb, target).astype(np.float32)
        after = np.clip(D.softlight(img, cs[..., None]), 0, 1)
        d = (after - img)[sl].mean(axis=2)
        p95, mx = np.percentile(np.abs(d), 95) * 255, np.abs(d).max() * 255
        cell = np.hstack([rs(u8(after[sl])), rs(u8(np.repeat(np.clip(0.5 + d * 8, 0, 1)[..., None], 3, axis=2)))])
        cells.append(label(cell, "  ".join(f"{b[0]:g}-{b[1]:g}mm x{b[2]:g}" for b in bands), f"change p95 {p95:.1f}, max {mx:.1f} (of 255)"))
        print(f"{' '.join(spec)}: p95 {p95:.1f}, max {mx:.1f}")

    while len(cells) % 3:
        cells.append(np.zeros_like(cells[0]))
    sep = lambda hh: np.full((hh, 10, 3), 255, np.uint8)
    rows = []
    for i in range(0, len(cells), 3):
        row = []
        for c in cells[i:i + 3]:
            row += [c, sep(c.shape[0])]
        rows.append(np.hstack(row[:-1]))
        rows.append(np.full((10, rows[-1].shape[1], 3), 255, np.uint8))
    grid = np.vstack(rows[:-1])
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), grid, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"wrote {out} ({grid.shape[1]}x{grid.shape[0]})")


if __name__ == "__main__":
    main()
