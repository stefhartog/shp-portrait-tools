"""Unmasked frequency separation with a skin-only extra Blur (BGR, 0..1)."""

import math

import cv2
import numpy as np
from . import progress


def settings(req):
    p = req.get("frequency", {})
    method = p.get("method", "gaussian")
    if method not in ("gaussian", "median"):
        raise ValueError("frequency method must be gaussian or median")
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
    result = {"method": method, "radius": radius, "blur_radius": blur, "radius_at_s181": calibration}
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


def split(img, method="gaussian", radius=8):
    color = skin_filter(img, np.ones(img.shape[:2], np.float32), radius, method)
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
