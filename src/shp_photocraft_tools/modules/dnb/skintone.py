"""Hue on Color plus optional ordinary/directional Blur, then unmasked Detail restoration."""

from functools import lru_cache
import math

import cv2
import numpy as np

from . import frequency_separation as FS
from . import progress

# 33^3 with six decimal places fits PhotoCraft's 1 MiB control-request limit.
LUT_SIZE = 33
DEFAULTS = {"hue_center": 30, "hue_width": 30, "hue_softness": 5, "hue_amount": 100}
BOUNDS = {"hue_center": (0, 360), "hue_width": (0, 180), "hue_softness": (0, 30),
          "hue_amount": (0, 100)}


def settings(req):
    p = req.get("skintone", {})
    if not isinstance(p, dict):
        raise ValueError("skintone settings must be an object")
    on = p.get("on", True)
    if type(on) is not bool:
        raise ValueError("skintone on must be a boolean")
    result = {"on": on}
    for key, default in DEFAULTS.items():
        value = float(p.get(key, default))
        lo, hi = BOUNDS[key]
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{key} must be {lo}-{hi}")
        result[key] = value
    return result


def soft_limit(x, lo, hi, shoulder):
    """Identity in the core; exponential shoulders approach the allowed bounds."""
    shoulder = min(shoulder, (hi - lo) / 2)
    if shoulder <= 0:
        return np.clip(x, lo, hi)
    low, high = lo + shoulder, hi - shoulder
    return np.where(x < low, lo + shoulder * np.exp(np.minimum(0, (x - low) / shoulder)),
                    np.where(x > high, hi - shoulder * np.exp(np.minimum(0, (high - x) / shoulder)), x))


def transform(rgb, p, channel):
    hsv = cv2.cvtColor(np.ascontiguousarray(rgb, dtype=np.float32), cv2.COLOR_RGB2HSV)
    hue, sat, value = cv2.split(hsv)
    # Hue is undefined on gray. Avoid inventing a colour or magnifying near-gray noise.
    protection = np.clip(sat / 0.05, 0, 1)
    protection = protection * protection * (3 - 2 * protection)
    # Keep every corner of a cube intersecting the gray diagonal unchanged.
    # Otherwise trilinear mixing of coloured neighbours can tint neutral pixels.
    chroma = np.ptp(rgb, axis=-1)
    grid_protection = np.clip((chroma * (LUT_SIZE - 1) - 1) / 2, 0, 1)
    protection *= grid_protection * grid_protection * (3 - 2 * grid_protection)
    if channel == "Hue":
        if p["hue_width"] == 180 or p["hue_amount"] == 0:
            return rgb.copy()
        delta = (hue - p["hue_center"] + 180) % 360 - 180
        mapped = soft_limit(delta, -p["hue_width"], p["hue_width"], p["hue_softness"])
        hue = (hue + (mapped - delta) * protection * p["hue_amount"] / 100) % 360
    else:
        raise ValueError("unknown Skintone channel")
    return cv2.cvtColor(cv2.merge((hue, sat, value)), cv2.COLOR_HSV2RGB)


@lru_cache(maxsize=4)
def _tables(key):
    p = dict(key)
    axis = np.linspace(0, 1, LUT_SIZE, dtype=np.float32)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    rgb = np.stack((r, g, b), axis=-1).reshape(-1, LUT_SIZE, 3)
    return tuple(np.round(np.clip(transform(rgb, p, channel), 0, 1).reshape(LUT_SIZE, LUT_SIZE, LUT_SIZE, 3), 6)
                 for channel in ("Hue",))


def tables(p):
    return _tables(tuple(sorted(p.items())))


def cube_text(table, name):
    lines = "\n".join("%.6f %.6f %.6f" % tuple(row) for row in table.reshape(-1, 3))
    return f'TITLE "SHP Skintone {name}"\nLUT_3D_SIZE {LUT_SIZE}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n{lines}\n'


def sample(rgb, table):
    """Trilinear .cube sampling, red varying fastest; bounded temporary memory."""
    out = np.empty_like(rgb, dtype=np.float32)
    for y in range(0, rgb.shape[0], 64):
        src = np.clip(rgb[y:y + 64], 0, 1) * (LUT_SIZE - 1)
        idx = np.minimum(src.astype(np.int32), LUT_SIZE - 2)
        fraction = src - idx
        r, g, b = np.moveaxis(idx, -1, 0)
        fr, fg, fb = [a[..., None] for a in np.moveaxis(fraction, -1, 0)]
        lower = ((table[b, g, r] * (1 - fr) + table[b, g, r + 1] * fr) * (1 - fg)
                 + (table[b, g + 1, r] * (1 - fr) + table[b, g + 1, r + 1] * fr) * fg)
        upper = ((table[b + 1, g, r] * (1 - fr) + table[b + 1, g, r + 1] * fr) * (1 - fg)
                 + (table[b + 1, g + 1, r] * (1 - fr) + table[b + 1, g + 1, r + 1] * fr) * fg)
        out[y:y + 64] = lower * (1 - fb) + upper * fb
    return out


def source_error(bgr):
    if not np.isfinite(bgr).all():
        return "Skintone needs finite RGB samples; the source contains NaN or infinity."
    lo, hi = float(bgr.min()), float(bgr.max())
    if lo < 0 or hi > 1:
        return (f"Skintone needs normalized RGB in 0-1; this source ranges from {lo:.7g} to {hi:.7g}. "
                "Extended-range/HDR correction is not supported. Use an explicitly tone-mapped SDR copy "
                "for Skintone; other skin modes remain available. No source values have been clipped.")
    return None


def process(bgr, mask, p):
    if bgr.ndim != 3 or bgr.shape[2] != 3 or mask.shape != bgr.shape[:2]:
        raise ValueError("Skintone needs matching RGB image and mask dimensions")
    error = source_error(bgr)
    if error:
        raise ValueError(error)
    if not np.isfinite(mask).all():
        raise ValueError("Skintone mask must be finite")
    if not p.get("on", True):
        return bgr
    hue, = tables(dict(p, hue_amount=100))
    rgb = np.ascontiguousarray(bgr[..., ::-1])
    corrected = sample(rgb, hue)[..., ::-1]
    opacity = np.round(np.clip(mask, 0, 1) * 255) / 255 * p["hue_amount"] / 100
    return (bgr + (corrected - bgr) * opacity[..., None]).astype(np.float32)


def separate(img, mask, method, radius, blur_radius, *, native=False):
    """Use exactly the RGB16 Color/Blur/Detail data sent to the native stack."""
    error = source_error(img)
    if error:
        raise ValueError(error)
    layers = FS.separate(img, mask, method, radius, blur_radius)
    for key in layers:
        pixels = progress.call("Preparing RGB16 " + key.title(), FS.rgb16, layers[key])
        layers[key] = pixels if native else pixels.astype(np.float32) / 65535
    return layers


def base(layers, mask, frequency=None):
    frequency = frequency or {}
    result = layers["color"]
    if "blur" in layers:
        opacity = (np.round(np.clip(mask, 0, 1) * 255) / 255
                   if frequency.get("blur_mask", True) else np.ones(mask.shape, np.float32))
        opacity = opacity * frequency.get("blur_strength", 100) / 100
        result = result + (layers["blur"] - result) * opacity[..., None]
    if "directional" in layers:
        opacity = layers["directional_mask"] * layers["directional_strength"] / 100
        result = result + (layers["directional"] - result) * opacity[..., None]
    return result


def composite(layers, mask, p, frequency=None):
    color = process(base(layers, mask, frequency), mask, p)
    return color, np.clip(color + 2 * layers["detail"] - 1, 0, 1)


def estimate_center(color, mask):
    """Mask-weighted circular hue mean; gray/black samples carry no hue evidence."""
    hsv = cv2.cvtColor(np.ascontiguousarray(color, dtype=np.float32), cv2.COLOR_BGR2HSV)
    keep = (mask > 1 / 255) & (hsv[..., 1] >= .05) & (hsv[..., 2] > 1 / 255)
    weights = np.clip(mask[keep], 0, 1).astype(np.float64)
    if not weights.size:
        return {"center": None, "note": "Hue Center not estimated: no usable masked coloured skin samples; previous Center retained. Set Center manually."}
    angles = np.deg2rad(hsv[..., 0][keep].astype(np.float64))
    vector = np.sum(weights * np.exp(1j * angles)) / weights.sum()
    if abs(vector) < .1:
        return {"center": None, "note": "Hue Center not estimated: skin hues have no clear circular direction; previous Center retained. Set Center manually."}
    center = float(np.rad2deg(np.angle(vector)) % 360)
    if center > 360 - 1e-4:
        center = 0.0
    center = round(center, 1) % 360
    return {"center": center, "note": f"Hue Center estimated from original Color and shared skin mask: {center:.1f}°."}


def distribution(before, after, mask, p):
    """Weighted 10/50/90% HSV summaries by brightness, never a new skin detection."""
    if not p.get("on", True):
        return {"bins": [], "hue_center": p["hue_center"], "sampled_weight": 0,
                "effect": {"note": "Skintone correction is off; Color, skin-masked Blur and Detail remain active."}}
    stride = max(1, math.ceil(math.sqrt(mask.size / 100000)))
    delta = np.max(np.abs(after[::stride, ::stride] - before[::stride, ::stride]), axis=-1)
    active = mask[::stride, ::stride] > 1 / 255
    effect = {"max_rgb_change_255": float(delta.max() * 255),
              "changed_pct": float(100 * np.mean(delta[active] > 1 / 255)) if active.any() else 0}
    if not active.any():
        effect["note"] = "No visible correction: the shared mask is empty."
    elif p["hue_amount"] == 0:
        effect["note"] = "Hue Amount is zero; only the skin-masked Blur can change Color."
    elif effect["changed_pct"] == 0:
        effect["note"] = ("No visible correction in this preview: colours are already within the soft limits "
                          "or protected near-neutral, with no visible Blur change. Narrow the hue range to request stronger correction.")
    else:
        effect["note"] = f"Visible correction on {effect['changed_pct']:.1f}% of masked preview samples."
    images = [cv2.cvtColor(np.ascontiguousarray(a[::stride, ::stride]), cv2.COLOR_BGR2HSV)
              for a in (before, after)]
    weights = np.clip(mask[::stride, ::stride], 0, 1).ravel()
    hsv = [a.reshape(-1, 3) for a in images]
    bins = np.minimum((hsv[0][:, 2] * 16).astype(int), 15)
    rows = []
    for i in range(16):
        keep = (bins == i) & (weights > 1 / 255) & (hsv[0][:, 1] >= 0.05)
        if not np.any(keep):
            rows.append(None)
            continue
        w = weights[keep]
        row = {"brightness": (i + 0.5) / 16, "weight": float(w.sum())}
        for label, a in zip(("before", "after"), hsv):
            hue = (a[keep, 0] - p["hue_center"] + 180) % 360 - 180
            summaries = []
            for values in (hue, a[keep, 1] * 100):
                order = np.argsort(values)
                q = np.interp([0.1, 0.5, 0.9], np.cumsum(w[order]) / w.sum(), values[order])
                summaries.append(q.tolist())
            row[label] = {"hue": summaries[0], "saturation": summaries[1]}
        rows.append(row)
    return {"bins": rows, "hue_center": p["hue_center"],
            "sampled_weight": float(sum(row["weight"] for row in rows if row)),
            "space": "HSV of native document RGB; brightness = HSV value", "effect": effect}
