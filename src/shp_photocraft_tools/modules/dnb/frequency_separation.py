"""Unmasked frequency separation with a skin-only extra Blur (BGR, 0..1)."""

import math
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from . import progress


def settings(req):
    p = req.get("frequency", {})
    if not isinstance(p, dict):
        raise ValueError("frequency settings must be an object")
    on = p.get("on", True)
    if type(on) is not bool:
        raise ValueError("frequency on must be a boolean")
    method = p.get("method", "gaussian")
    if method not in ("gaussian", "median", "median_fast"):
        raise ValueError("frequency method must be gaussian, median or median_fast")
    radius, blur = float(p.get("radius", 8)), float(p.get("blur_radius", 12))
    calibration = float(p.get("radius_at_s181", 6))
    if not math.isfinite(calibration) or not 0.5 <= calibration <= 30:
        raise ValueError("radius calibration at S181 must be 0.5-30 pixels")
    percent = p.get("blur_percent")
    if percent is not None:
        percent = float(percent)
        if not math.isfinite(percent) or not 100 <= percent <= 500:
            raise ValueError("Blur percent must be 100-500")
        S = float(req["S"])
        if not math.isfinite(S) or S <= 0:
            raise ValueError("enter a valid S for scaled frequency separation")
        radius = min(100, max(1, math.floor(calibration * S / 181 + 0.5)))
        blur = radius * percent / 100
    if not math.isfinite(radius) or not 1 <= radius <= 100:
        raise ValueError("separation radius must be 1-100 full-image pixels")
    if not math.isfinite(blur) or not 0 <= blur <= 500:
        raise ValueError("Blur radius must be 0-500 full-image pixels")
    result = {"on": on, "method": method, "radius": radius, "blur_radius": blur, "radius_at_s181": calibration}
    strength = float(p.get("blur_strength", 100))
    masked = p.get("blur_mask", True)
    if not math.isfinite(strength) or not 0 <= strength <= 100:
        raise ValueError("Blur strength must be 0-100 percent")
    if type(masked) is not bool:
        raise ValueError("Blur skin mask must be a boolean")
    result.update(blur_strength=strength, blur_mask=masked)
    if percent is not None:
        result["blur_percent"] = percent
    return result


def skin_filter(img, mask, radius, method="gaussian"):
    """Do not attenuate filtered pixels by opacity: Blur's layer mask does that once.

    Gaussian uses soft-mask normalized convolution; median uses only positive-mask
    samples, with equal votes. Outside support (or with no skin) keep the source.
    """
    img = np.ascontiguousarray(img, dtype=np.float32)
    m = np.clip(np.asarray(mask, np.float32), 0, 1)
    if m.shape != img.shape[:2] or not np.isfinite(m).all() or not np.isfinite(img).all():
        raise ValueError("image and finite skin mask must have matching dimensions")
    if radius <= 0 or not np.any(m > 0):
        return img.copy()
    if method == "gaussian":
        weights = cv2.GaussianBlur(m, (0, 0), radius, borderType=cv2.BORDER_REPLICATE)
        numerator = cv2.GaussianBlur(img * m[..., None], (0, 0), radius, borderType=cv2.BORDER_REPLICATE)
        filtered = np.divide(numerator, weights[..., None], out=img.copy(),
                             where=weights[..., None] > 1e-8)
    elif method == "median":
        # WMF_OFF has no guidance/color weighting. Its mask excludes samples from
        # the moving histogram instead of replacing them with black.
        r = max(1, round(radius))
        support = (m > 0).astype(np.uint8)
        filtered = cv2.ximgproc.weightedMedianFilter(
            np.zeros(m.shape, np.uint8), img, r,
            weightType=cv2.ximgproc.WMF_OFF, mask=support)
        counts = cv2.boxFilter(support.astype(np.float32), -1, (2 * r + 1, 2 * r + 1),
                               normalize=False, borderType=cv2.BORDER_CONSTANT)
        # No median quantization for an isolated skin pixel: it has only itself.
        filtered[counts <= 1] = img[counts <= 1]
    else:
        raise ValueError("unknown skin filter")
    return np.where((m > 0)[..., None], filtered, img).astype(np.float32)


def median_fast(img, radius):
    """Square median on uniformly quantized RGB; exact stripe halos, no resizing."""
    img = np.ascontiguousarray(img, dtype=np.float32)
    if (img.ndim != 3 or img.shape[2] != 3 or not np.isfinite(img).all()
            or img.min() < 0 or img.max() > 1):
        raise ValueError("Median Fast requires finite RGB samples in 0..1")
    if not math.isfinite(radius) or not 0 < radius <= 100:
        raise ValueError("Median Fast radius must be positive and at most 100 pixels")
    r = max(1, round(radius))
    h, w = img.shape[:2]
    quantized = np.empty(img.shape, np.uint8)
    for y in range(0, h, 128):
        quantized[y:y+128] = np.round(img[y:y+128] * 255).astype(np.uint8)
    workers = min(8, max(1, h*w // 4_000_000), max(1, h // (2*r+1)))
    if workers == 1:
        filtered = cv2.medianBlur(quantized, 2*r+1)
    else:
        filtered = np.empty_like(quantized)
        edges = np.linspace(0, h, workers+1, dtype=int)
        def stripe(i):
            y0, y1 = edges[i:i+2]
            lo, hi = max(0, y0-r), min(h, y1+r)
            tile = cv2.medianBlur(quantized[lo:hi], 2*r+1)
            filtered[y0:y1] = tile[y0-lo:y1-lo]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fs-median") as pool:
            list(pool.map(stripe, range(workers)))
    del quantized
    color = np.empty(img.shape, np.float32)
    for y in range(0, h, 128):
        color[y:y+128] = filtered[y:y+128].astype(np.float32)/255
    return color


def split(img, method="gaussian", radius=8):
    color = median_fast(img, radius) if method == "median_fast" else \
        skin_filter(img, np.ones(img.shape[:2], np.float32), radius, method)
    detail = np.clip(0.5 + (img - color) * 0.5, 0, 1).astype(np.float32)
    return {"color": color, "detail": detail}


def separate(img, mask, method="gaussian", radius=8, blur_radius=12):
    layers = progress.call("Separating Color and Detail", split, img, method, radius)
    color, detail = layers["color"], layers["detail"]
    blur = progress.call("Computing skin-aware Blur", skin_filter, color, mask, blur_radius, "gaussian")
    return {"color": color, "blur": blur, "detail": detail}


def composite(layers, mask):
    """Native stack: Color, masked Normal Blur, then unmasked Linear Light Detail."""
    base = layers["color"] + (layers["blur"] - layers["color"]) * mask[..., None]
    return np.clip(base + 2 * layers["detail"] - 1, 0, 1)


def rgb16(img):
    out = np.empty(img.shape, np.uint16)
    for y in range(0, img.shape[0], 64):
        out[y:y + 64] = np.round(np.clip(img[y:y + 64], 0, 1) * 65535).astype(np.uint16)
    return out
