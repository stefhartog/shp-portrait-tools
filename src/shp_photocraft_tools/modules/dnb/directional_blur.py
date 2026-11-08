"""Pose-segment motion filtering on FS's low-frequency base, with one editable mask."""
import math

import cv2
import numpy as np

DEFAULTS = {"on": False, "left": True, "right": True, "distance": 30,
            "strength": 100, "width": 100, "start": 0, "end": 100, "taper": 0, "feather": 3,
            "left_lower_leg": False, "right_lower_leg": False,
            "left_upper_arm": False, "right_upper_arm": False,
            "left_forearm": False, "right_forearm": False, "torso": False}
BOUNDS = {"distance": (0, 300), "strength": (0, 100), "width": (1, 1000),
          "start": (0, 99), "end": (1, 100), "taper": (0, 90), "feather": (0, 50)}
# COCO endpoints are ordered proximal -> distal so taper follows limb orientation.
SEGMENTS = {"left": ("Left upper leg", 11, 13), "right": ("Right upper leg", 12, 14),
            "left_lower_leg": ("Left lower leg", 13, 15), "right_lower_leg": ("Right lower leg", 14, 16),
            "left_upper_arm": ("Left upper arm", 5, 7), "right_upper_arm": ("Right upper arm", 6, 8),
            "left_forearm": ("Left forearm", 7, 9), "right_forearm": ("Right forearm", 8, 10)}
PAIRS = {"upper_arm": ("left_upper_arm", "right_upper_arm"),
         "forearm": ("left_forearm", "right_forearm"),
         "upper_leg": ("left", "right"),
         "lower_leg": ("left_lower_leg", "right_lower_leg")}
PAIR_FIELDS = ("distance", "width", "taper", "strength", "feather", "start", "end")


def pair_settings(p, pair):
    return {**p, **p.get("pairs", {}).get(pair, {})}


def output_strength(p):
    values = [pair_settings(p, pair)["strength"] for pair, keys in PAIRS.items()
              if any(p.get(key, DEFAULTS[key]) for key in keys)]
    if p.get("torso", False):
        values.append(p.get("torso_controls", {}).get("strength", p["strength"]))
    return values[0] if values and all(value == values[0] for value in values) else 100


def settings(req):
    raw = req.get("directional", {})
    if not isinstance(raw, dict):
        raise ValueError("Directional Blur settings must be an object")
    raw = dict(raw)
    if "length" in raw and "end" not in raw:
        legacy = raw["length"]
        if isinstance(legacy, bool) or not math.isfinite(float(legacy)) or not 10 <= float(legacy) <= 100:
            raise ValueError("Directional Blur legacy length must be 10-100; extensions beyond the knee are no longer supported")
        raw["end"] = legacy
    p = {}
    for key in ("on", *SEGMENTS, "torso"):
        p[key] = raw.get(key, DEFAULTS[key])
        if type(p[key]) is not bool:
            raise ValueError(f"Directional Blur {key} must be a boolean")
    for key, (lo, hi) in BOUNDS.items():
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool):
            raise ValueError(f"Directional Blur {key} must be {lo}-{hi}")
        value = float(value)
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"Directional Blur {key} must be {lo}-{hi}")
        p[key] = value
    pairs = raw.get("pairs", {})
    if not isinstance(pairs, dict) or any(key not in PAIRS for key in pairs):
        raise ValueError("Directional Blur pairs must contain only recognized limb pairs")
    p["pairs"] = {}
    for pair in PAIRS:
        values = pairs.get(pair, {})
        if not isinstance(values, dict) or any(key not in PAIR_FIELDS for key in values):
            raise ValueError(f"Directional Blur {pair} controls must contain distance/width/taper")
        p["pairs"][pair] = {}
        for key in PAIR_FIELDS:
            value = values.get(key, p[key])
            lo, hi = BOUNDS[key]
            if isinstance(value, bool):
                raise ValueError(f"Directional Blur {pair} {key} must be {lo}-{hi}")
            value = float(value)
            if not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"Directional Blur {pair} {key} must be {lo}-{hi}")
            p["pairs"][pair][key] = value
        if p["pairs"][pair]["end"] - p["pairs"][pair]["start"] < 1:
            raise ValueError(f"Directional Blur {pair} endpoints need at least 1% separation")
    torso = raw.get("torso_controls", {})
    if not isinstance(torso, dict) or any(key not in PAIR_FIELDS for key in torso):
        raise ValueError("Directional Blur torso controls must contain distance/width/taper")
    p["torso_controls"] = {}
    for key, default, lo, hi in (("distance", p["distance"], 0, 300),
                                ("width", 100, 1, 200), ("taper", 0, 0, 90),
                                *((key, p[key], *BOUNDS[key]) for key in ("strength", "feather", "start", "end"))):
        value = torso.get(key, default)
        if isinstance(value, bool):
            raise ValueError(f"Directional Blur torso {key} must be {lo}-{hi}")
        value = float(value)
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"Directional Blur torso {key} must be {lo}-{hi}")
        p["torso_controls"][key] = value
    if p["torso_controls"]["end"] - p["torso_controls"]["start"] < 1:
        raise ValueError("Directional Blur torso endpoints need at least 1% separation")
    if p["end"] - p["start"] < 1:
        raise ValueError("Directional Blur region endpoints need at least 1% separation")
    if p["on"] and not (p["torso"] or any(p[key] for key in SEGMENTS)):
        raise ValueError("Directional Blur needs at least one selected limb segment")
    return p


def torso_geometry(p, pose, shape, *, origin=(0, 0), scale=1):
    if pose is None or pose.get("kp") is None:
        raise ValueError("Directional Blur unavailable: no torso pose. Get Image again.")
    kp = np.asarray(pose["kp"])
    if kp.ndim != 2 or kp.shape[1] != 2 or len(kp) <= 12:
        raise ValueError("Directional Blur unavailable: invalid torso pose")
    quad = kp[[5, 6, 12, 11]].astype(np.float64)
    h, w = shape[:2]
    if not np.isfinite(quad).all() or np.any(quad < 0) or np.any(quad[:, 0] >= w) or np.any(quad[:, 1] >= h):
        raise ValueError("Directional Blur unavailable: torso coordinates non-finite or outside the image")
    edges = np.roll(quad, -1, axis=0) - quad
    following = np.roll(edges, -1, axis=0)
    cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    if (not (np.all(cross > 0) or np.all(cross < 0))
            or np.min(np.linalg.norm(edges, axis=1)) < 1):
        raise ValueError("Directional Blur unavailable: torso region is degenerate or crossed")
    shoulder = (quad[0] + quad[1]) / 2
    hip = (quad[2] + quad[3]) / 2
    axis = hip - shoulder
    if np.linalg.norm(axis) < 1:
        raise ValueError("Directional Blur unavailable: torso axis is degenerate")
    controls = p.get("torso_controls", {"width": 100, "taper": 0})
    start = controls.get("start", p.get("start", 0)) / 100
    end = controls.get("end", p.get("end", 100)) / 100
    corners = []
    for t in (start, end):
        left = quad[0] * (1 - t) + quad[3] * t
        right = quad[1] * (1 - t) + quad[2] * t
        centre = (left + right) / 2
        half = (right - left) / 2 * controls["width"] / 100 * (1 - t * controls["taper"] / 100)
        corners.extend((centre - half, centre + half))
    corners = np.asarray(corners)[[0, 1, 3, 2]]
    if np.linalg.norm(axis) * (end - start) * scale > 16000:
        raise ValueError("Directional Blur scaled torso axis exceeds safe processing limit (16000 pixels)")
    if max(np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3])) * scale > 8000:
        raise ValueError("Directional Blur scaled torso width exceeds safe processing limit (8000 pixels)")
    offset = np.asarray(origin)
    return ((corners - offset) * scale,
            (shoulder + axis * start - offset) * scale,
            (shoulder + axis * end - offset) * scale)


def segments(p, pose, shape, *, origin=(0, 0), scale=1):
    if not p["on"]:
        return []
    if pose is None or pose.get("kp") is None:
        raise ValueError("Directional Blur unavailable: no pose for the selected subject. Get Image again.")
    kp = np.asarray(pose["kp"])
    h, w = shape[:2]
    result = []
    for key, (label, ia, ib) in SEGMENTS.items():
        if not p.get(key, DEFAULTS[key]):
            continue
        if kp.ndim != 2 or kp.shape[1] != 2 or len(kp) <= max(ia, ib):
            raise ValueError(f"Directional Blur unavailable: invalid {label.lower()} pose")
        points = kp[[ia, ib]].astype(np.float64)
        if not np.isfinite(points).all():
            raise ValueError(f"Directional Blur unavailable: {label.lower()} non-finite joint coordinates")
        if np.any(points < 0) or np.any(points[:, 0] >= w) or np.any(points[:, 1] >= h):
            raise ValueError(f"Directional Blur unavailable: {label.lower()} joints outside the image")
        if np.linalg.norm(points[1] - points[0]) < 1:
            raise ValueError(f"Directional Blur unavailable: {label.lower()} segment is degenerate")
        config = pair_settings(p, next(pair for pair, keys in PAIRS.items() if key in keys))
        start, end = config.get("start", 0), config.get("end", config.get("length", 100))
        if start != 0 or end != 100:
            axis = points[1] - points[0]
            points = np.stack((points[0] + axis * start / 100, points[0] + axis * end / 100))
        if np.linalg.norm(points[1] - points[0]) * scale > 16000:
            raise ValueError("Directional Blur scaled region length exceeds safe processing limit (16000 pixels)")
        a, b = (points - np.asarray(origin)) * scale
        result.append((label, a, b))
    if p.get("torso", False):
        _, a, b = torso_geometry({**p, **p.get("torso_controls", {})}, pose, shape, origin=origin, scale=scale)
        result.append(("Torso", a, b))
    return result


def scaled(p, S, scale=1):
    if not math.isfinite(S) or S <= 0 or not math.isfinite(scale) or scale <= 0:
        raise ValueError("Directional Blur requires a valid source scale S")
    values = {key: p[key] * S / 181 * scale for key in ("distance", "width", "feather")}
    if values["distance"] > 2000 or values["width"] > 8000 or values["feather"] > 500:
        raise ValueError("Directional Blur scaled distance/width/feather exceeds safe processing limits")
    return values


def region(shape, a, b, width, taper=0):
    """Rounded segment region: proximal width, linearly narrowed toward distal end."""
    h, w = shape[:2]
    delta = b - a
    out = np.empty((h, w), np.float32)
    x = np.arange(w)[None, :]
    for row in range(0, h, 128):
        y = np.arange(row, min(h, row + 128))[:, None]
        t = np.clip(((x - a[0]) * delta[0] + (y - a[1]) * delta[1]) / np.dot(delta, delta), 0, 1)
        distance = np.sqrt((x - a[0] - t * delta[0]) ** 2 + (y - a[1] - t * delta[1]) ** 2)
        radius = width / 2 if taper == 0 else width / 2 * (1 - t * taper / 100)
        out[row:row + 128] = np.clip(radius + .5 - distance, 0, 1)
    return out


def torso_region(shape, quad):
    """Convex shoulder/hip box coverage, without the limb capsule's rounded caps."""
    h, w = shape[:2]
    out = np.empty((h, w), np.float32)
    edges = np.roll(quad, -1, axis=0) - quad
    area = np.sum(quad[:, 0] * np.roll(quad[:, 1], -1) - quad[:, 1] * np.roll(quad[:, 0], -1))
    orientation = 1 if area > 0 else -1
    x = np.arange(w)[None, :]
    for row in range(0, h, 128):
        y = np.arange(row, min(h, row + 128))[:, None]
        distance = np.full((y.shape[0], w), np.inf)
        for point, edge in zip(quad, edges):
            signed = orientation * (edge[0] * (y - point[1]) - edge[1] * (x - point[0])) / np.linalg.norm(edge)
            np.minimum(distance, signed, out=distance)
        out[row:row + 128] = np.clip(distance + .5, 0, 1)
    return out


def motion(img, angle, distance, *, origin=(0, 0), support=None):
    """Identical rotated motion operator on weighted pixels and sampling support."""
    if support is not None:
        if support.shape != img.shape[:2] or not np.isfinite(support).all():
            raise ValueError("Directional motion needs matching finite sampling support")
        support = np.clip(support, 0, 1).astype(np.float32, copy=False)
    count = max(1, round(distance) + 1)
    if count == 1:
        return img.copy()
    h, w = img.shape[:2]
    transform = cv2.getRotationMatrix2D((0, 0), angle, 1)
    transform[:, 2] = transform[:, :2] @ np.asarray(origin)
    corners = np.array([[0, 0, 1], [w - 1, 0, 1], [0, h - 1, 1], [w - 1, h - 1, 1]]) @ transform.T
    # Anchor the rotated sampling grid to image coordinates, not the chosen ROI.
    low, high = np.floor(corners.min(0)), np.ceil(corners.max(0))
    transform[:, 2] -= low
    size = tuple((np.ceil(high - low) + 1).astype(int))
    # Odd kernel keeps the centred motion symmetric rather than shifting the image.
    count = count if count % 2 else count + 1
    inverse = cv2.invertAffineTransform(transform)

    def filter_plane(plane):
        rotated = cv2.warpAffine(plane, transform, size, flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
        filtered = cv2.boxFilter(rotated, -1, (count, 1), borderType=cv2.BORDER_REPLICATE)
        return cv2.warpAffine(filtered, inverse, (w, h), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)

    denominator = filter_plane(support) if support is not None else None
    result = img.copy() if support is not None else np.empty_like(img)
    for channel in range(img.shape[2]):
        plane = img[..., channel]
        numerator = filter_plane(plane * support if support is not None else plane)
        if denominator is None:
            result[..., channel] = numerator
        else:
            np.divide(numerator, denominator, out=result[..., channel], where=denominator > 1e-6)
    return result


def apply(img, skin, p, pose, S, *, source_shape=None, origin=(0, 0), scale=1):
    """Return full-strength raster and region union × skin; strength is never baked in."""
    if not p["on"]:
        return None
    if skin.shape != img.shape[:2] or not np.isfinite(skin).all():
        raise ValueError("Directional Blur requires a matching finite shared skin mask")
    lines = segments(p, pose, source_shape or img.shape, origin=origin, scale=scale)
    configurations = {SEGMENTS[key][0]: pair_settings(p, pair)
                      for pair, keys in PAIRS.items() for key in keys}
    torso = None
    if p.get("torso", False):
        torso, _, _ = torso_geometry({**p, **p.get("torso_controls", {})}, pose, source_shape or img.shape, origin=origin, scale=scale)
        controls = p.get("torso_controls", {"distance": p["distance"], "width": 100, "taper": 0})
        # Torso Width is a percentage; geometric extent was already validated above.
        configurations["Torso"] = {**p, **controls, "width": 1}
    sizes_by_label = {label: scaled(configurations[label], S, scale) for label, _, _ in lines}
    h, w = img.shape[:2]
    margin = max(sizes["width"] / 2 + math.ceil(4 * sizes["feather"]) + 2
                 for sizes in sizes_by_label.values())
    points = np.concatenate([np.stack((a, b)) for _, a, b in lines])
    if torso is not None:
        points = np.concatenate((points, torso))
    low, high = points.min(0) - margin, points.max(0) + margin
    x0, y0 = max(0, math.floor(low[0])), max(0, math.floor(low[1]))
    x1, y1 = min(w, math.ceil(high[0]) + 1), min(h, math.ceil(high[1]) + 1)
    raster, mask = img.copy(), np.zeros((h, w), np.float32)
    if x1 <= x0 or y1 <= y0:
        return raster, mask
    shape = (y1 - y0, x1 - x0)
    union = np.zeros(shape, np.float32)
    weight = np.zeros(shape, np.float32)
    mixed = np.zeros((*shape, 3), np.float32)
    offset = np.array([x0, y0])
    sigma = p["feather"] * S / 181 * scale
    same_feather = len({sizes["feather"] for sizes in sizes_by_label.values()}) == 1
    if same_feather:
        sigma = next(iter(sizes_by_label.values()))["feather"]
    opacity = output_strength(p)
    for label, a, b in lines:
        sizes = sizes_by_label[label]
        sigma_i = sizes["feather"]
        kernel = 2 * math.ceil(4 * sigma_i) + 1
        coverage = (torso_region(shape, torso - offset) if label == "Torso"
                    else region(shape, a - offset, b - offset, sizes["width"], configurations[label].get("taper", 0)))
        soft = (cv2.GaussianBlur(coverage, (kernel, kernel), sigma_i, borderType=cv2.BORDER_CONSTANT)
                if sigma_i > 0 else coverage)
        np.maximum(union, coverage if same_feather else soft, out=union)
        # Only sample the surrounding ROI, including the full motion-filter halo.
        pad = math.ceil(sizes["distance"] / 2) + 4
        xa, ya, xb, yb = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
        angle = math.degrees(math.atan2((b - a)[1], (b - a)[0]))
        strength = configurations[label].get("strength", p["strength"])
        if strength == 0:
            candidate = img[y0:y1, x0:x1]
        else:
            filtered = motion(img[ya:yb, xa:xb], angle, sizes["distance"],
                              origin=(xa + origin[0] * scale, ya + origin[1] * scale),
                              support=skin[ya:yb, xa:xb])
            candidate = filtered[y0 - ya:y1 - ya, x0 - xa:x1 - xa]
        if strength == opacity:
            mixed += candidate * soft[..., None]
        else:
            base = img[y0:y1, x0:x1]
            mixed += (base + (candidate - base) * strength / opacity) * soft[..., None]
        weight += soft
    np.divide(mixed, weight[..., None], out=mixed, where=weight[..., None] > 0)
    active = weight > 0
    raster[y0:y1, x0:x1][active] = mixed[active]
    if same_feather and sigma > 0:
        kernel = 2 * math.ceil(4 * sigma) + 1
        union = cv2.GaussianBlur(union, (kernel, kernel), sigma, borderType=cv2.BORDER_CONSTANT)
    mask[y0:y1, x0:x1] = np.clip(union, 0, 1) * np.clip(skin[y0:y1, x0:x1], 0, 1)
    return raster, mask
