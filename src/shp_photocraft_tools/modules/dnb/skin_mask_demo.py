"""Show skin_mask.py results: A alone vs the final mask, with what was removed drawn in colour.

green = kept skin, red = removed as eyes/brows/lips (face mesh), yellow = removed for colour (tattoo, ink).
Writes out16/skin_final.jpg (all photos) and out16/skin_final_zoom.jpg (larger crops).

Usage: uv run python -m shp_photocraft_tools.modules.dnb.skin_mask_demo [FOLDER]
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from . import skin_mask as K
from . import skin_mask_compare as M  # pick_target, person_box, letterbox, caption (shared helpers)
from .paths import DATA

ROOT = DATA


def overlay(img, res):
    out = img.astype(np.float32)
    A = res["A"]
    feat = A * res["features"]
    col = A * (1 - res["colour"]) * (1 - res["features"])
    for mask, colour, al in ((res["skin"], (0, 255, 0), 0.55), (feat, (0, 0, 255), 0.7), (col, (0, 255, 255), 0.7)):
        m = (np.clip(mask, 0, 1) * al)[..., None]
        out = out * (1 - m) + np.array(colour, np.float32) * m
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "testpictures"
    out = ROOT / "out16"
    out.mkdir(exist_ok=True)
    rows, zooms = [], []
    zoom = {"SHP04378": "face", "SHP04234": "body", "SHP04540": "body", "SHP06277": "face"}
    for path in sorted(p for p in folder.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"}):
        img = cv2.imread(str(path))
        t = M.pick_target(path, img)
        if t is None:
            continue
        box = M.person_box(t, img.shape)
        t0 = time.time()
        res = K.skin_mask(img, t["face_info"], box, t["S"])
        dt = time.time() - t0
        A, skin = res["A"], res["skin"]
        a_area = float((A > 0.5).sum())
        f_area = float(((A * res["features"]) > 0.5).sum())
        c_area = float(((A * (1 - res["colour"]) * (1 - res["features"])) > 0.5).sum())
        print(f"{path.name}: S={t['S']:.0f}; segmenter area {100 * (A > 0.5).mean():.1f}% of image; removed by features "
              f"{100 * f_area / max(a_area, 1):.1f}% of it, by colour {100 * c_area / max(a_area, 1):.1f}%; crop {res['rect']}; {dt:.2f}s")
        S = 420
        a_only = {"A": A, "features": np.zeros_like(A), "colour": np.ones_like(A), "skin": A}
        row = np.hstack([M.letterbox(M.tint(img, A), S), M.letterbox(overlay(img, res), S)])
        rows.append(M.caption(row, [f"{path.name}  S={t['S']:.0f}", f"A alone | final (red: eyes/brows/lips {100 * f_area / max(a_area, 1):.1f}%, yellow: colour {100 * c_area / max(a_area, 1):.1f}%)"]))
        if path.stem in zoom:
            h, w = img.shape[:2]
            if zoom[path.stem] == "face" and t["face"] is not None:
                fx, fy, fw, fh = t["face"][:4]
                cx, cy, side = fx + fw / 2, fy + fh / 2, int(min(fw * 2.6, min(h, w)))
            else:
                x0, y0, x1, y1 = box
                cx, cy, side = (x0 + x1) / 2, (y0 + y1) / 2, int(min(max(x1 - x0, y1 - y0), min(h, w)))
            xa, ya = int(min(max(0, cx - side / 2), w - side)), int(min(max(0, cy - side / 2), h - side))
            sl = (slice(ya, ya + side), slice(xa, xa + side))
            Z = 700
            rs = lambda im: cv2.resize(im[sl], (Z, Z), interpolation=cv2.INTER_AREA)
            zooms.append(M.caption(np.hstack([rs(img), rs(M.tint(img, A)), rs(overlay(img, res))]),
                                   [f"{path.name}: original | A alone | final (green kept, red eyes/brows/lips, yellow colour)"]))
    if len(rows) % 2:
        rows.append(np.zeros_like(rows[0]))
    sep = np.full((rows[0].shape[0], 12, 3), 255, np.uint8)
    pairs = [np.hstack([rows[i], sep, rows[i + 1]]) for i in range(0, len(rows), 2)]
    hs = lambda im: np.full((8, im.shape[1], 3), 255, np.uint8)
    cv2.imwrite(str(out / "skin_final.jpg"), np.vstack(sum([[p, hs(p)] for p in pairs], [])[:-1]), [cv2.IMWRITE_JPEG_QUALITY, 90])
    cv2.imwrite(str(out / "skin_final_zoom.jpg"), np.vstack(sum([[z, hs(z)] for z in zooms], [])[:-1]), [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"wrote {out}/skin_final.jpg and skin_final_zoom.jpg")


if __name__ == "__main__":
    main()
