"""Experiment: is skin texture a usable scale yardstick?

For faces with a known scale S (px per 63 mm), cut cheek/forehead patches of a fixed PHYSICAL size
(0.4*S px, ~25 mm), compute their radially averaged power spectra and plot them against physical
frequency (cycles/mm). If skin texture has a consistent physical scale, the curves from different
photos line up once the x axis is physical; if they only line up in cycles/pixel, they reflect the
camera/lens/noise instead of the skin.

Usage: uv run python -m shp_photocraft_tools.modules.dnb.texture_experiment   (uses the S values listed in KNOWN below)
"""
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from . import estimate_scale as F
from .paths import DATA

ROOT = DATA
KNOWN = {  # S from combine_scale.py (px per 63 mm)
    "SHP04234.JPG": 261,           # medium
    "SHP04378.JPG": 298,           # high
    "SHP04540.JPG": 265,           # high
    "SHP04683_Smoll.jpg": 178,     # medium
    "SHP04049-Edit_small.jpg": 164,  # low
}
IPD_MM = 63.0


def patches(p, S):
    half = int(0.2 * S)
    pts = {"cheek_a": p[205, :2], "cheek_b": p[425, :2], "forehead": p[151, :2] + 0.15 * (p[9, :2] - p[151, :2])}
    return {k: (int(v[0]) - half, int(v[1]) - half, 2 * half) for k, v in pts.items()}


def radial_spectrum(patch):
    n = patch.shape[0]
    y, x = np.mgrid[:n, :n]
    A = np.column_stack([np.ones(n * n), x.ravel(), y.ravel()])
    coef, *_ = np.linalg.lstsq(A, patch.ravel(), rcond=None)
    patch = patch - (A @ coef).reshape(n, n)  # remove mean + linear shading
    win = np.outer(np.hanning(n), np.hanning(n))
    P = np.abs(np.fft.fftshift(np.fft.fft2(patch * win))) ** 2
    fy, fx = np.mgrid[:n, :n] - n // 2
    r = np.hypot(fx, fy)
    bins = np.arange(1, n // 2)
    ps = np.array([P[(r >= b - 0.5) & (r < b + 0.5)].mean() for b in bins])
    return bins / n, ps  # cycles per pixel, power


def main():
    out = ROOT / "out5"
    out.mkdir(exist_ok=True)
    fig, ax = plt.subplots(1, 2, figsize=(14, 5.5))
    print("image                      S   mm/px  patch  f(10% power, cyc/mm)  f(10%, cyc/px)")
    for name, S in KNOWN.items():
        img = cv2.imread(str(ROOT / "testpictures" / name))
        faces = F.detect(img)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        _, p, pres = F.landmarks_for(rgb, faces[0])
        lum = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float64) / 255.0
        mm_px = IPD_MM / S
        specs = []
        for k, (x, y, n) in patches(p, S).items():
            if x < 0 or y < 0 or x + n > lum.shape[1] or y + n > lum.shape[0]:
                continue
            fpx, ps = radial_spectrum(lum[y:y + n, x:x + n])
            specs.append(ps)
        if not specs:
            continue
        ps = np.exp(np.mean(np.log(specs), axis=0))  # geometric mean over patches
        ps = ps / ps[:3].mean()
        f_mm = fpx / mm_px
        below = np.where(ps < 0.1)[0]
        i10 = below[0] if len(below) else len(ps) - 1
        print(f"{name:24s}{S:5d} {mm_px:6.3f} {n:5d}  {f_mm[i10]:8.2f}            {fpx[i10]:.3f}")
        ax[0].loglog(f_mm, ps, label=f"{name} (S={S}, {mm_px:.2f} mm/px)")
        ax[1].loglog(fpx, ps, label=name)
    ax[0].set_xlabel("cycles / mm (using face-derived S)")
    ax[1].set_xlabel("cycles / pixel")
    for a in ax:
        a.set_ylabel("power (normalised to lowest bins)")
        a.grid(True, which="both", alpha=0.3)
    ax[0].legend(fontsize=7)
    ax[0].set_title("If skin has a physical texture scale, these curves line up")
    ax[1].set_title("Same data in pixel units")
    fig.tight_layout()
    fig.savefig(out / "texture_spectra.png", dpi=110)
    print("wrote", out / "texture_spectra.png")


if __name__ == "__main__":
    main()
