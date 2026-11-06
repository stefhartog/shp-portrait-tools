"""Skin mask = segmenter (A) minus face features (eyes, brows, lips from the face mesh) minus tattoos/off-colour areas.

A  MediaPipe selfie multiclass segmenter (ONNX, 256x256): skin = body-skin + face-skin, run on a crop around the person
   (the crop grows if the mask touches its edge), refined to the image edges with a guided filter.
Features  eyes, brows and lips from the 478-point face mesh, grown by FEATURE_GROW_MM and feathered; plus the nostrils
          (dark blobs found inside the lower-nose region the mesh gives).
Colour    a robust Lab colour model fitted on the confident skin pixels of the mask itself; pixels of the mask that are
          darker than the skin average AND far from it in colour (tattoos, ink, stray clothing) are removed. Brightness itself
          is ignored, so shadowed skin stays; brighter pixels (highlights) are never removed.

skin_mask(img_u8, face_info, box, S) -> dict with 'skin' (final 0..1), 'A', 'features', 'colour' (all full-size float32).
"""
from pathlib import Path

import cv2
import numpy as np

from . import dnb_gpu  # noqa: F401
import onnxruntime as ort

from . import dodge_burn as D

from .paths import model_path
_SEG = None

L_EYE = [263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466]
R_EYE = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
LIPS = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]
# Both mesh chains run outer-to-inner; reverse the upper chain to close the perimeter.
L_BROW = [276, 283, 282, 295, 285, 336, 296, 334, 293, 300]
R_BROW = [46, 53, 52, 65, 55, 107, 66, 105, 63, 70]
FEATURE_GROW_MM = 2.0
FEATURE_FEATHER_MM = 1.2


def seg():
    global _SEG
    if _SEG is None:
        s = ort.InferenceSession(str(model_path("selfie_multiclass_256x256.onnx")), providers=dnb_gpu.PROVIDERS)
        _SEG = (s, s.get_inputs()[0].name)
    return _SEG


def softmax(x):
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def refine(prob, crop_bgr):
    """Snap a coarse probability map to edges in the image (guided filter), then sharpen it."""
    guide = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    r = max(4, round(max(prob.shape) * 0.006))
    out = D.guided_filter(guide, prob.astype(np.float32), r, 1e-3)
    return D.smoothstep(0.35, 0.65, np.clip(out, 0, 1)).astype(np.float32)


def rect_crop(shape, box, margin=1.1):
    h, w = shape[:2]
    x0, y0, x1, y1 = box
    cx, cy, bw, bh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * margin, (y1 - y0) * margin
    return [int(max(0, cx - bw / 2)), int(max(0, cy - bh / 2)), int(min(w, cx + bw / 2)), int(min(h, cy + bh / 2))]


def _run_seg(img_u8, rect):
    x0, y0, x1, y1 = rect
    crop = img_u8[y0:y1, x0:x1]
    ch, cw = crop.shape[:2]
    k = 256.0 / max(ch, cw)
    rw, rh = max(1, round(cw * k)), max(1, round(ch * k))
    small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), (rw, rh), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    inp = np.zeros((256, 256, 3), np.float32)  # letterbox: keep the aspect ratio, pad with black
    oy, ox = (256 - rh) // 2, (256 - rw) // 2
    inp[oy:oy + rh, ox:ox + rw] = small
    s, name = seg()
    p = softmax(s.run(None, {name: inp[None]})[0][0])
    skin = cv2.resize((p[..., 2] + p[..., 3])[oy:oy + rh, ox:ox + rw], (cw, ch), interpolation=cv2.INTER_CUBIC)
    return refine(np.clip(skin, 0, 1), crop)


def mask_A(img_u8, box, margin=1.15, max_grow=3):
    """Segmenter mask on a crop around box. If the mask runs into an edge of the crop (and that edge is not the image
    edge), the crop is enlarged on that side and the segmenter is run again."""
    h, w = img_u8.shape[:2]
    rect = rect_crop(img_u8.shape, box, margin)
    for it in range(max_grow + 1):
        m = _run_seg(img_u8, rect)
        ys, xs = np.where(m > 0.5)
        if len(xs) == 0:
            break
        x0, y0, x1, y1 = rect
        cw, ch = x1 - x0, y1 - y0
        tx, ty = 0.015 * cw, 0.015 * ch
        grow = [xs.min() < tx and x0 > 0, ys.min() < ty and y0 > 0, xs.max() > cw - tx and x1 < w, ys.max() > ch - ty and y1 < h]
        if not any(grow) or it == max_grow:
            break
        rect = [max(0, int(x0 - (0.25 * cw if grow[0] else 0))), max(0, int(y0 - (0.25 * ch if grow[1] else 0))),
                min(w, int(x1 + (0.25 * cw if grow[2] else 0))), min(h, int(y1 + (0.25 * ch if grow[3] else 0)))]
    full = np.zeros((h, w), np.float32)
    full[rect[1]:rect[3], rect[0]:rect[2]] = m
    return full, rect


def feature_mask(shape, face_info, S):
    """Soft mask (1 on eyes, brows, lips and a margin around them) from the face mesh; zeros without a face."""
    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    if face_info is None or face_info[2] < 0.5:
        return m.astype(np.float32)
    p = face_info[0][:, :2]
    for ids in (L_EYE, R_EYE, LIPS, L_BROW, R_BROW):
        cv2.fillPoly(m, [p[ids].astype(np.int32)], 255)
    px_mm = S / D.IPD_MM
    k = max(1, int(FEATURE_GROW_MM * px_mm)) * 2 + 1
    m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    sig = max(0.8, FEATURE_FEATHER_MM * px_mm)
    return np.clip(cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), sig) * 1.4, 0, 1)


NOSE_REGION = [129, 358, 98, 327, 97, 326, 2, 1, 4]  # wings, base and tip of the nose: where the nostrils are


def nostril_mask(img_u8, face_info, S, max_mm2=40.0, min_mm2=1.0, dark_ratio=0.6):
    """Soft mask of the nostrils. The face mesh only says where the lower nose is. Inside that region we look for the
    (at most two) darkest local minima of the smoothed brightness that are clearly darker than the surrounding skin,
    and grow each one to about halfway between its darkest value and the skin around it."""
    h, w = img_u8.shape[:2]
    out = np.zeros((h, w), np.float32)
    if face_info is None or face_info[2] < 0.5:
        return out
    px_mm = S / D.IPD_MM
    p = face_info[0][:, :2]
    region = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(region, cv2.convexHull(p[NOSE_REGION].astype(np.int32)), 255)
    k = int(2.0 * px_mm) * 2 + 1
    region = cv2.dilate(region, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    ys, xs = np.where(region > 0)
    if len(xs) < 50:
        return out
    pad = int(3 * px_mm)
    y0, y1, x0, x1 = max(0, ys.min() - pad), min(h, ys.max() + pad), max(0, xs.min() - pad), min(w, xs.max() + pad)
    Y = D.luminance_linear(img_u8[y0:y1, x0:x1].astype(np.float32) / 255.0).astype(np.float32)
    Yf = cv2.GaussianBlur(Y, (0, 0), max(0.8, 0.3 * px_mm))     # for growing the blob
    Ys = cv2.GaussianBlur(Y, (0, 0), max(1.2, 1.0 * px_mm))     # for finding the minima
    reg = region[y0:y1, x0:x1] > 0
    ref = float(np.percentile(Y[reg], 70))
    kk = max(3, int(3.0 * px_mm)) | 1
    is_min = (Ys <= cv2.erode(Ys, np.ones((kk, kk), np.float32))) & reg & (Ys < dark_ratio * ref)
    my, mx = np.where(is_min)
    order = np.argsort(Ys[my, mx])
    chosen = []
    for i in order:
        if all(np.hypot(my[i] - cy, mx[i] - cx) > 3.5 * px_mm for cy, cx in chosen):
            chosen.append((my[i], mx[i]))
        if len(chosen) == 2:
            break
    keep = np.zeros(Y.shape, np.uint8)
    R = int(6 * px_mm)
    for cy, cx in chosen:
        wy0, wy1, wx0, wx1 = max(0, cy - R), min(Y.shape[0], cy + R + 1), max(0, cx - R), min(Y.shape[1], cx + R + 1)
        win = Yf[wy0:wy1, wx0:wx1]
        local_ref = float(np.percentile(win, 70))
        vmin = float(Ys[cy, cx])
        for f in (0.5, 0.35, 0.25):
            cand = (win < vmin + f * (local_ref - vmin)).astype(np.uint8)
            n, lab = cv2.connectedComponents(cand, connectivity=8)
            lbl = lab[cy - wy0, cx - wx0]
            if lbl == 0:
                continue
            comp = lab == lbl
            if comp.sum() <= max_mm2 * px_mm ** 2:
                if comp.sum() >= min_mm2 * px_mm ** 2:
                    keep[wy0:wy1, wx0:wx1][comp] = 255
                break
    if keep.any():
        k2 = int(0.6 * px_mm) * 2 + 1
        keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2)))
        soft = cv2.GaussianBlur(keep.astype(np.float32) / 255.0, (0, 0), max(0.8, 0.5 * px_mm))
        out[y0:y1, x0:x1] = np.clip(soft * 1.4, 0, 1)
    return out


def fit_colour(lab_pixels):
    """Robust mean/covariance of Lab samples; outliers (ink, hair strands, speculars) are trimmed away."""
    mu = np.median(lab_pixels, axis=0)
    for _ in range(3):
        d = np.abs(lab_pixels - mu) / (lab_pixels.std(axis=0) + 1e-3)
        keep = (d < 2.2).all(axis=1)
        if keep.sum() > 100:
            lab_pixels = lab_pixels[keep]
        mu = lab_pixels.mean(axis=0)
    cov = np.cov(lab_pixels.T)
    floor = np.diag([18.0 ** 2, 3.5 ** 2, 3.5 ** 2])  # brightness varies a lot with shading; chroma is what separates ink
    return mu, np.maximum(cov, 0) * 1.5 + floor


def colour_keep(img_u8, A, features, S, lo=3.5, hi=6.0):
    """1 where a mask pixel still looks like the person's skin, 0 where its colour is far off (tattoo, ink)."""
    ys, xs = np.where(A > 0.5)
    keep = np.ones(A.shape, np.float32)
    if len(xs) < 500:
        return keep
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = img_u8[y0:y1, x0:x1].astype(np.float32) / 255.0
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2Lab)
    a = A[y0:y1, x0:x1]
    px_mm = S / D.IPD_MM
    k = max(3, int(1.5 * px_mm)) | 1
    core = cv2.erode((a > 0.9).astype(np.uint8), np.ones((k, k), np.uint8)) & (features[y0:y1, x0:x1] < 0.2)
    samples = lab[core > 0]
    if len(samples) < 300:
        return keep
    if len(samples) > 200000:  # plenty for a 3-parameter colour model
        samples = samples[np.random.default_rng(0).choice(len(samples), 200000, replace=False)]
    mu, cov = fit_colour(samples)
    flat = lab.reshape(-1, 3)
    diff = flat - mu
    diff[:, 0] = 0.0  # judge by chroma only: shadowed skin keeps its chroma, ink and clothing do not
    d = np.sqrt(np.einsum("ij,jk,ik->i", diff, np.linalg.inv(cov), diff))
    d[flat[:, 0] >= mu[0]] = 0.0           # brighter than average skin (highlights) is never removed
    d = d.reshape(lab.shape[:2])
    kp = 1.0 - D.smoothstep(lo, hi, d)
    kp = cv2.medianBlur((kp * 255).astype(np.uint8), 5).astype(np.float32) / 255.0  # drop single-pixel speckles
    kp = cv2.GaussianBlur(kp, (0, 0), max(0.8, 0.5 * px_mm))
    keep[y0:y1, x0:x1] = kp
    return keep


def skin_mask(img_u8, face_info, box, S, use_features=True, use_colour=True, colour_lo=3.5, colour_hi=6.0, use_nostrils=True):
    A, rect = mask_A(img_u8, box)
    feats = feature_mask(img_u8.shape, face_info, S) if use_features else np.zeros_like(A)
    if use_nostrils:
        feats = np.maximum(feats, nostril_mask(img_u8, face_info, S))
    keep = colour_keep(img_u8, A, feats, S, colour_lo, colour_hi) if use_colour else np.ones_like(A)
    skin = np.clip(A * (1 - feats) * keep, 0, 1).astype(np.float32)
    return {"skin": skin, "A": A, "features": feats, "colour": keep, "rect": rect}
