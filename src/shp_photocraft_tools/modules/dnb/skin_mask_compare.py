"""Compare two ways to get a skin mask, on all photos in a folder.

A  MediaPipe selfie multiclass segmenter (ONNX, 256x256, Apache-2.0): skin = body-skin + face-skin, run on a crop
   around the person and refined against the full-resolution image with a guided filter.
B  No new model: face mesh polygon (minus eyes, lips, brows) + a colour model sampled from the face (or from limb
   patches if there is no face), limited to the region around the pose keypoints, refined the same way.

Writes out15/skin_masks.jpg (all photos: original | A | B, mask shown as a green tint) and out15/skin_zoom.jpg
(larger crops of a few photos), and prints coverage numbers and timings.

Usage: uv run python -m shp_photocraft_tools.modules.dnb.skin_mask_compare [FOLDER]
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from . import dnb_gpu  # noqa: F401
import onnxruntime as ort

from . import combine_scale as C
from . import dodge_burn as D
from . import estimate_body_scale as B
from .paths import DATA, model_path

ROOT = DATA
_SEG = None


def segmenter():
    global _SEG
    if _SEG is None:
        session = ort.InferenceSession(str(model_path("selfie_multiclass_256x256.onnx")), providers=dnb_gpu.PROVIDERS)
        _SEG = session, session.get_inputs()[0].name
    return _SEG

FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149,
             150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]
L_EYE = [263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466]
R_EYE = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
LIPS = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]
L_BROW = [276, 283, 282, 295, 285, 300, 293, 334, 296, 336]
R_BROW = [46, 53, 52, 65, 55, 70, 63, 105, 66, 107]
LIMBS = [("l_sho", "l_elb"), ("r_sho", "r_elb"), ("l_elb", "l_wri"), ("r_elb", "r_wri"),
         ("l_hip", "l_kne"), ("r_hip", "r_kne"), ("l_kne", "l_ank"), ("r_kne", "r_ank")]


def softmax(x):
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def refine(prob, crop_bgr):
    """Snap a coarse probability map to edges in the image (guided filter), then sharpen it."""
    guide = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    r = max(4, round(max(prob.shape) * 0.006))
    out = D.guided_filter(guide, prob.astype(np.float32), r, 1e-3)
    return D.smoothstep(0.35, 0.65, np.clip(out, 0, 1)).astype(np.float32)


def square_crop(shape, box, margin=1.1):
    h, w = shape[:2]
    x0, y0, x1, y1 = box
    side = int(min(max(x1 - x0, y1 - y0) * margin, min(h, w)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return int(min(max(0, cx - side / 2), w - side)), int(min(max(0, cy - side / 2), h - side)), side


def rect_crop(shape, box, margin=1.1):
    """Integer crop rectangle (x0, y0, x1, y1) around box, grown by margin, clamped to the image. Not forced square:
    a tall person in a portrait photo would otherwise be cut off."""
    h, w = shape[:2]
    x0, y0, x1, y1 = box
    cx, cy, bw, bh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * margin, (y1 - y0) * margin
    return (int(max(0, cx - bw / 2)), int(max(0, cy - bh / 2)), int(min(w, cx + bw / 2)), int(min(h, cy + bh / 2)))


def mask_A(img_u8, box):
    h, w = img_u8.shape[:2]
    x0, y0, x1, y1 = rect_crop(img_u8.shape, box, 1.05)
    crop = img_u8[y0:y1, x0:x1]
    ch, cw = crop.shape[:2]
    k = 256.0 / max(ch, cw)
    rw, rh = max(1, round(cw * k)), max(1, round(ch * k))
    small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), (rw, rh), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    inp = np.zeros((256, 256, 3), np.float32)           # letterbox: keep the aspect ratio, pad with black
    oy, ox = (256 - rh) // 2, (256 - rw) // 2
    inp[oy:oy + rh, ox:ox + rw] = small
    session, input_name = segmenter()
    p = softmax(session.run(None, {input_name: inp[None]})[0][0])
    skin = (p[..., 2] + p[..., 3])[oy:oy + rh, ox:ox + rw]
    skin = cv2.resize(skin, (cw, ch), interpolation=cv2.INTER_CUBIC)
    full = np.zeros((h, w), np.float32)
    full[y0:y1, x0:x1] = refine(np.clip(skin, 0, 1), crop)
    return full


def poly_mask(shape, pts, ids):
    m = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(m, [pts[ids].astype(np.int32)], 255)
    return m


def fit_colour(lab_pixels):
    """Robust mean/covariance of Lab samples (drops outliers such as hair strands and speculars)."""
    mu = np.median(lab_pixels, axis=0)
    for _ in range(2):
        d = np.abs(lab_pixels - mu) / (lab_pixels.std(axis=0) + 1e-3)
        keep = (d < 2.5).all(axis=1)
        if keep.sum() > 50:
            lab_pixels = lab_pixels[keep]
        mu = lab_pixels.mean(axis=0)
    cov = np.cov(lab_pixels.T)
    floor = np.diag([18.0 ** 2, 3.5 ** 2, 3.5 ** 2])  # shading changes L a lot; keep a/b fairly tight
    return mu, np.maximum(cov, 0) * 1.5 + floor


def mask_B(img_u8, face_info, kp, sc):
    h, w = img_u8.shape[:2]
    f32 = img_u8.astype(np.float32) / 255.0
    lab = cv2.cvtColor(f32, cv2.COLOR_BGR2Lab)
    pts_vis = kp[:17][sc[:17] >= 0.3] if kp is not None else np.zeros((0, 2))
    face_poly = np.zeros((h, w), np.uint8)
    features = np.zeros((h, w), np.uint8)
    samples = None
    if face_info is not None and face_info[2] >= 0.5:
        p = face_info[0][:, :2]
        face_poly = poly_mask(img_u8.shape, p, FACE_OVAL)
        for ids in (L_EYE, R_EYE, LIPS, L_BROW, R_BROW):
            features |= poly_mask(img_u8.shape, p, ids)
        features = cv2.dilate(features, np.ones((5, 5), np.uint8))
        core = cv2.erode(face_poly, np.ones((9, 9), np.uint8)) & ~features
        samples = lab[core > 0]
    elif kp is not None:
        patches = []
        for a, b in LIMBS:
            ia, ib = B.I[a], B.I[b]
            if sc[ia] >= 0.5 and sc[ib] >= 0.5:
                mid = (kp[ia] + kp[ib]) / 2
                r = max(4, int(0.12 * np.linalg.norm(kp[ia] - kp[ib])))
                x0, y0 = int(max(0, mid[0] - r)), int(max(0, mid[1] - r))
                patch = lab[y0:y0 + 2 * r, x0:x0 + 2 * r].reshape(-1, 3)
                if len(patch) > 20:
                    patches.append(patch)
        if patches:
            meds = np.array([np.median(p, axis=0) for p in patches])
            centre = np.median(meds[:, 1:], axis=0)
            good = [p for p, m in zip(patches, meds) if np.linalg.norm(m[1:] - centre) < 6]
            if good:
                samples = np.vstack(good)
    prob = np.zeros((h, w), np.float32)
    if samples is not None and len(samples) > 50:
        mu, cov = fit_colour(samples)
        diff = lab.reshape(-1, 3) - mu
        d2 = np.einsum("ij,jk,ik->i", diff, np.linalg.inv(cov), diff).reshape(h, w)
        colour = 1.0 - D.smoothstep(2.0, 3.6, np.sqrt(d2))
        region = np.zeros((h, w), np.uint8)
        if len(pts_vis) >= 3:
            hull = cv2.convexHull(pts_vis.astype(np.int32))
            cv2.fillConvexPoly(region, hull, 255)
            diag = float(np.hypot(*(pts_vis.max(0) - pts_vis.min(0))))
            k = max(3, int(diag * 0.12)) | 1
            region = cv2.dilate(region, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        prob = colour.astype(np.float32) * (region > 0)
        prob = cv2.morphologyEx(prob, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    prob = np.maximum(prob, ((face_poly > 0) & (features == 0)).astype(np.float32))
    prob[features > 0] = 0.0
    ys, xs = np.where(prob > 0.05)
    if len(xs) < 50:
        return prob
    box = (xs.min(), ys.min(), xs.max(), ys.max())
    x0, y0, x1, y1 = rect_crop(img_u8.shape, box, 1.1)
    out = np.zeros((h, w), np.float32)
    out[y0:y1, x0:x1] = refine(prob[y0:y1, x0:x1], img_u8[y0:y1, x0:x1])
    return out


def pick_target(path, img):
    subjects, bodies, af, target, faces = C.build_subjects(str(path), img)
    cands = [(i, C.combine(s[5])) for i, s in enumerate(subjects)]
    cands = [(i, r) for i, r in cands if r["S"] is not None]
    if not cands:
        return None
    i = target if target is not None and any(c[0] == target for c in cands) else min(cands, key=lambda c: c[1]["sigma"])[0]
    kind, f, face_info, bi, est, cues = subjects[i]
    kp, sc = bodies[bi] if bi is not None else (None, None)
    r = dict(cands)[i]
    why = "AF target" if i == target else ("most trustworthy subject (no AF point)" if af is None else "most trustworthy subject")
    desc = f"subject #{i}, {why}, cues " + ", ".join(f"{c[0]}={c[1]:.0f}" for c in r["used"])
    if f is not None:
        centre = (float(f[0] + f[2] / 2), float(f[1] + f[3] / 2))
    elif kp is not None and (sc[:17] >= 0.3).any():
        pts = kp[:17][sc[:17] >= 0.3]
        centre = (float(pts[:, 0].mean()), float(pts[:, 1].mean()))
    else:
        centre = None
    seen = [{"face": s[1], "kp": bodies[s[3]][0] if s[3] is not None else None, "sc": bodies[s[3]][1] if s[3] is not None else None,
             "face_info": s[2], "target": j == i} for j, s in enumerate(subjects)]
    return {"face": f, "face_info": face_info, "kp": kp, "sc": sc, "S": r["S"], "conf": r["confidence"], "desc": desc, "centre": centre,
            "seen": seen}


def person_box(t, shape):
    """Box around the person from pose keypoints and face box, grown so the head top and the limbs' ends are inside
    (keypoints stop at eyes/ankles, so the raw box would cut off forehead and feet)."""
    h, w = shape[:2]
    pts = []
    if t["kp"] is not None:
        pts.append(t["kp"][:17][t["sc"][:17] >= 0.3])
    if t["face"] is not None:
        x, y, bw, bh = t["face"][:4]
        pts.append(np.array([[x - 0.3 * bw, y - 0.8 * bh], [x + 1.3 * bw, y + 1.2 * bh]]))
    if not pts or sum(len(p) for p in pts) < 2:
        return (0, 0, w, h)
    p = np.vstack(pts)
    x0, y0, x1, y1 = p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()
    bh_, bw_ = y1 - y0, x1 - x0
    if t["face"] is None:
        y0 -= 0.25 * bh_
    return (max(0, x0 - 0.12 * bw_), max(0, y0 - 0.06 * bh_), min(w, x1 + 0.12 * bw_), min(h, y1 + 0.12 * bh_))


def tint(img_u8, mask, colour=(0, 255, 0), alpha=0.55):
    m = (mask * alpha)[..., None]
    return np.clip(img_u8 * (1 - m) + np.array(colour, np.float32) * m, 0, 255).astype(np.uint8)


def letterbox(img, size):
    h, w = img.shape[:2]
    k = size / max(h, w)
    r = cv2.resize(img, (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA)
    out = np.full((size, size, 3), 20, np.uint8)
    y0, x0 = (size - r.shape[0]) // 2, (size - r.shape[1]) // 2
    out[y0:y0 + r.shape[0], x0:x0 + r.shape[1]] = r
    return out


def caption(img, lines):
    bar = np.full((22 * len(lines) + 6, img.shape[1], 3), 24, np.uint8)
    for i, t in enumerate(lines):
        cv2.putText(bar, t, (6, 20 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
    return np.vstack([bar, img])


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "testpictures"
    out = ROOT / "out15"
    out.mkdir(exist_ok=True)
    rows, zooms = [], []
    zoom_names = {"SHP04378", "SHP06277", "SHP06404"}
    for path in sorted(p for p in folder.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"}):
        img = cv2.imread(str(path))
        t = pick_target(path, img)
        if t is None:
            print(f"{path.name}: no subject found")
            continue
        box = person_box(t, img.shape)
        t0 = time.time()
        a = mask_A(img, box)
        ta = time.time() - t0
        t0 = time.time()
        b = mask_B(img, t["face_info"], t["kp"], t["sc"])
        tb = time.time() - t0
        ma, mb = a > 0.5, b > 0.5
        x0, y0, x1, y1 = (int(v) for v in box)
        pad = int(0.1 * max(x1 - x0, y1 - y0))
        reg = (slice(max(0, y0 - pad), y1 + pad), slice(max(0, x0 - pad), x1 + pad))
        inter, union = (ma[reg] & mb[reg]).sum(), (ma[reg] | mb[reg]).sum()
        h, w = img.shape[:2]
        print(f"{path.name}: S={t['S']:.0f} ({t['conf']}), A covers {100 * ma.mean():.1f}% of image ({ta:.2f}s), "
              f"B {100 * mb.mean():.1f}% ({tb:.2f}s), overlap IoU in person area {inter / max(union, 1):.2f}")
        S = 420
        row = np.hstack([letterbox(img, S), letterbox(tint(img, a), S), letterbox(tint(img, b), S)])
        rows.append(caption(row, [f"{path.name}  S={t['S']:.0f} ({t['conf']})",
                                  f"A selfie-seg {100 * ma.mean():.1f}% | B face+colour {100 * mb.mean():.1f}% | IoU {inter / max(union, 1):.2f}"]))
        if path.stem in zoom_names:
            side = int(min(max(x1 - x0, y1 - y0) * 1.15, min(h, w)))
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if path.stem == "SHP04378" and t["face"] is not None:
                fx, fy, fw, fh = t["face"][:4]
                cx, cy, side = fx + fw / 2, fy + fh / 2, int(min(fw * 3.2, min(h, w)))
            xa, ya = int(min(max(0, cx - side / 2), w - side)), int(min(max(0, cy - side / 2), h - side))
            sl = (slice(ya, ya + side), slice(xa, xa + side))
            Z = 640
            z = np.hstack([cv2.resize(img[sl], (Z, Z), interpolation=cv2.INTER_AREA),
                           cv2.resize(tint(img, a)[sl], (Z, Z), interpolation=cv2.INTER_AREA),
                           cv2.resize(tint(img, b)[sl], (Z, Z), interpolation=cv2.INTER_AREA)])
            zooms.append(caption(z, [f"{path.name}: original | A selfie-seg | B face mesh + colour"]))
    if len(rows) % 2:
        rows.append(np.zeros_like(rows[0]))
    pairs = [np.hstack([rows[i], np.full((rows[i].shape[0], 12, 3), 255, np.uint8), rows[i + 1]]) for i in range(0, len(rows), 2)]
    sep = lambda im: np.full((8, im.shape[1], 3), 255, np.uint8)
    grid = np.vstack(sum([[p, sep(p)] for p in pairs], [])[:-1])
    cv2.imwrite(str(out / "skin_masks.jpg"), grid, [cv2.IMWRITE_JPEG_QUALITY, 90])
    zgrid = np.vstack(sum([[z, sep(z)] for z in zooms], [])[:-1])
    cv2.imwrite(str(out / "skin_zoom.jpg"), zgrid, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"wrote {out}/skin_masks.jpg ({grid.shape[1]}x{grid.shape[0]}) and skin_zoom.jpg ({zgrid.shape[1]}x{zgrid.shape[0]})")


if __name__ == "__main__":
    main()
