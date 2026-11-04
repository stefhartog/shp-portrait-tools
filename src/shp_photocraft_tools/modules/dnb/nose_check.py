"""Check the nostril detection: crops around the nose, original | nostril mask in red | candidate region in blue.
Usage: uv run python -m shp_photocraft_tools.modules.dnb.nose_check   (writes $DNB_DATA/out17/nose_check.jpg)"""
from pathlib import Path

import cv2
import numpy as np

from . import skin_mask as K
from . import skin_mask_compare as M
from .paths import DATA

ROOT = DATA


def main():
    out = ROOT / "out17"
    out.mkdir(exist_ok=True)
    rows = []
    for stem in ("SHP04049-Edit_small", "SHP04234", "SHP04378", "SHP04540", "SHP04683_Smoll"):
        path = next((ROOT / "testpictures").glob(stem + ".*"))
        img = cv2.imread(str(path))
        t = M.pick_target(path, img)
        if t is None or t["face_info"] is None or t["face_info"][2] < 0.5:
            continue
        S = t["S"]
        nm = K.nostril_mask(img, t["face_info"], S)
        p = t["face_info"][0][:, :2]
        cx, cy = p[1]
        half = int(S / 63 * 28)
        h, w = img.shape[:2]
        x0, y0 = int(min(max(0, cx - half), w - 2 * half)), int(min(max(0, cy - half), h - 2 * half))
        sl = (slice(y0, y0 + 2 * half), slice(x0, x0 + 2 * half))
        crop = img[sl].copy()
        ov = crop.astype(np.float32)
        m = (nm[sl] * 0.8)[..., None]
        ov = ov * (1 - m) + np.array((0, 0, 255), np.float32) * m
        ov = ov.astype(np.uint8)
        hull = cv2.convexHull((p[K.NOSE_REGION] - [x0, y0]).astype(np.int32))
        cv2.polylines(ov, [hull], True, (255, 120, 0), max(1, half // 150))
        Z = 520
        r = lambda im: cv2.resize(im, (Z, Z), interpolation=cv2.INTER_AREA)
        row = np.hstack([r(crop), r(ov)])
        bar = np.full((28, row.shape[1], 3), 24, np.uint8)
        cv2.putText(bar, f"{path.name}  S={S:.0f}  nostril area {nm.sum() / (S / 63) ** 2:.0f} mm2 (soft)", (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
        rows.append(np.vstack([bar, row]))
        print(path.name, f"nostril mask area ~{(nm > 0.5).sum() / (S / 63) ** 2:.1f} mm2")
    pairs = [np.hstack([rows[i], rows[i + 1]]) if i + 1 < len(rows) else np.hstack([rows[i], np.zeros_like(rows[i])]) for i in range(0, len(rows), 2)]
    cv2.imwrite(str(out / "nose_check.jpg"), np.vstack(pairs), [cv2.IMWRITE_JPEG_QUALITY, 92])
    print("wrote", out / "nose_check.jpg")


if __name__ == "__main__":
    main()
