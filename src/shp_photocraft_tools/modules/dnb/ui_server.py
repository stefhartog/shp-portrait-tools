"""SHP PhotoCraft D&B: local web UI for the dodge & burn prototype.

Run: uv run shp-photocraft-dnb [--port 8765] [--dir pictures ...]
Then open http://127.0.0.1:8765 in a browser. The server only listens on 127.0.0.1.

The page lets you pick a photo, set bands (feature-size range in mm, gain, taper) and global settings, press Apply to
see Original | Result | Soft-light layer side by side, and Save to write, at full resolution, 1_result (picture with
the settings), 2_skinmask and 3_softlight_layer (no mask applied) plus settings.json to results/<name>_<time>/.
The skin mask (segmenter minus eyes/brows/lips/nostrils minus off-colour pixels) is computed once at Load.
Presets (named settings, plus one default loaded at start) are stored in $DNB_DATA/presets.json.
Previews are computed on a downscaled copy (max ~1600 px) of the subject crop or the whole image; Save always
works at full resolution.
"""
import argparse
import datetime
import io
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms
from shp_photocraft_tools.shared.ui import stylesheet
from shp_photocraft_tools.shared.health import identity
from shp_photocraft_tools.shared.lifecycle import BrowserHandler, BrowserServer, script

from . import dodge_burn as D
from . import skin_mask as K
from . import skin_mask_compare as M
from . import photocraft_bridge as PCB
from . import frequency_separation as FS
from .paths import DATA, MODELS

ROOT = DATA
HTML = Path(__file__).resolve().parent / "ui" / "index.html"
PRESETS = ROOT / "presets.json"
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
LOCK = threading.Lock()
STATE = {"img": None, "path": None, "S": None, "centre": None, "imgs": {}, "version": 0, "region_key": None, "region": None,
         "masks": None, "icc": None, "source_img": None, "source_icc": None, "source_opaque": True,
         "seen": None, "pbox": None, "last": None, "struct": None, "avers": 0, "region_off": (0, 0), "region_box": None, "subject": None}
DIRS = []


def u8(x):
    return np.round(np.clip(x, 0, 1) * 255).astype(np.uint8)


def encode(bgr_or_gray, fmt=".jpg", q=90, icc=None):
    """Encode for the browser / file. A JPEG carries the picture's ICC profile when there is one, so the browser (and
    Photoshop / PhotoCraft) show the colours as in the original instead of reading them as sRGB."""
    if icc and fmt in (".jpg", ".png") and bgr_or_gray.ndim == 3:
        buf = io.BytesIO()
        Image.fromarray(cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2RGB)).save(
            buf, "JPEG" if fmt == ".jpg" else "PNG", quality=q, icc_profile=icc)
        return buf.getvalue()
    ok, buf = cv2.imencode(fmt, bgr_or_gray, [cv2.IMWRITE_JPEG_QUALITY, q] if fmt == ".jpg" else [])
    return buf.tobytes()


SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def colour_plan(path, raw8):
    """Decide how to treat the picture's colours. Returns (mode, icc_for_outputs, converted_raw8_or_None, note).
    sRGB or no profile: used as is. Adobe RGB: kept as is, analysed with its own gamma/luminance weights, and the same
    profile is embedded in every JPEG we write. Any other profile: converted to sRGB (and tagged sRGB)."""
    try:
        icc = Image.open(path).info.get("icc_profile")
    except Exception:
        icc = None
    if not icc:
        return "srgb", None, None, "no embedded profile, treated as sRGB"
    try:
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        desc = ImageCms.getProfileDescription(prof).strip()
    except Exception:
        return "srgb", None, None, "unreadable embedded profile, treated as sRGB"
    low = desc.lower()
    if "adobe rgb" in low or "adobergb" in low:
        return "adobe", icc, None, f"{desc}: kept; outputs carry the same profile"
    if low.startswith("srgb") or "srgb" in low.split():
        return "srgb", icc, None, f"{desc}: kept; outputs carry the same profile"
    rgb = Image.fromarray(cv2.cvtColor(raw8, cv2.COLOR_BGR2RGB))
    conv = ImageCms.profileToProfile(rgb, prof, ImageCms.createProfile("sRGB"), outputMode="RGB")
    return "srgb", SRGB_ICC, cv2.cvtColor(np.array(conv), cv2.COLOR_RGB2BGR), f"{desc}: converted to sRGB (outputs tagged sRGB)"


def list_images():
    out = []
    for d in DIRS:
        for p in sorted(Path(d).glob("*")):
            if p.suffix.lower() in IMG_EXT:
                out.append({"name": p.name, "path": str(p)})
    return out


def parse_params(p):
    mode = p.get("mode", "dnb")
    if mode not in ("dnb", "mask", "frequency"):
        raise ValueError("mode must be dnb, mask or frequency")
    bands = [(float(b[0]), float(b[1]), float(b[2]), max(float(b[3]), 1e-3)) for b in p.get("bands", []) if float(b[1]) > float(b[0])]
    if not bands and mode == "dnb":
        raise ValueError("no valid band (HI must be larger than LO)")
    S = float(p["S"])
    if not math.isfinite(S) or S <= 0:
        raise ValueError("S must be a positive finite scale (pixels per 63 mm)")
    tone = p.get("tone", {})
    on = bool(tone.get("on", True))
    tone_pairs = D.tone_from_mid(float(tone.get("mid", 0.52)), float(tone.get("range", 0.87)), float(tone.get("taper", 0.16)))
    mk = p.get("mask", {})
    mask_flags = {"on": bool(mk.get("on", True)), "features": bool(mk.get("features", True)), "colour": bool(mk.get("colour", True)),
                  "feather": max(0.0, float(mk.get("feather", 1.0))), "inward": bool(mk.get("inward", True))}
    st = p.get("structure", {})
    colour = p.get("color", {})
    color_on = bool(colour.get("on", False))
    if color_on and (colour.get("hue") is None or colour.get("saturation") is None):
        raise ValueError("pick a color before enabling the color range")
    color_hue = float(colour["hue"]) if colour.get("hue") is not None else 0.0
    color_saturation = float(colour["saturation"]) if colour.get("saturation") is not None else 0.0
    color_hue_range = float(colour.get("hue_range", 30.0))
    color_saturation_range = float(colour.get("saturation_range", 0.25))
    color_blur_mm = min(10.0, max(0.0, float(colour.get("blur_mm", 1.0))))
    color_smooth_mm = min(5.0, max(0.0, float(colour.get("smooth_mm", 0.8))))
    color_strength = min(1.0, max(0.0, float(colour.get("strength", 1.0))))
    color_filter = {"on": color_on, "hue": color_hue, "saturation": color_saturation,
                    "hue_range": min(180.0, max(1.0, color_hue_range)),
                    "saturation_range": min(1.0, max(0.01, color_saturation_range)),
                    "blur_mm": color_blur_mm, "smooth_mm": color_smooth_mm, "strength": color_strength}
    return dict(
        mode=mode, frequency=FS.settings(p) if mode == "frequency" else None,
        protect=min(1.0, max(0.0, float(st.get("protect", 0.0)))), protect_mm=max(0.5, float(st.get("mm", 4.0))),
        protect_thr=max(0.005, float(st.get("thr", 0.1))),
        tone_strength=min(1.0, max(0.0, float(tone.get("strength", 1.0)))), color_filter=color_filter, analysis=p.get("analysis", {}),
        mask_flags=mask_flags, S=S, bands=bands, strength=float(p.get("strength", 1.0)), edge=max(float(p.get("edge", 0.12)), 1e-3),
        tau=float(p.get("tau", 1.0)), max_ev=float(p.get("max_ev", 1.0)),
        shadows=tone_pairs[0] if on else None, highlights=tone_pairs[1] if on else None,
    )


def combine_mask(comps, flags, px_per_mm):
    """Skin mask from the stored components: segmenter, minus eyes/brows/lips/nostrils, times the colour check, then
    feathered. Feather = Gaussian of sigma feather_mm; 'inward only' keeps the outer boundary where it is and fades the
    mask inside it, so the correction does not spill onto hair or background."""
    m = comps["A"]
    if flags["features"]:
        m = m * (1 - comps["features"])
    if flags["colour"]:
        m = m * comps["colour"]
    m = np.clip(m, 0, 1).astype(np.float32)
    sig = flags.get("feather", 0.0) * px_per_mm
    if sig >= 0.5:
        b = cv2.GaussianBlur(m, (0, 0), sig)
        m = np.minimum(m, np.clip(2 * b - 1, 0, 1)) if flags.get("inward", True) else b
    return m


def add_color_range(img, mask, color_filter, px_per_mm):
    if not color_filter["on"]:
        return mask
    c = color_filter
    cm = D.color_range_mask(img, c["hue"], c["saturation"], c["hue_range"], c["saturation_range"],
                           c["blur_mm"] * px_per_mm, c["strength"], c["smooth_mm"] * px_per_mm)
    return cm if mask is None else mask * cm


def tool_mask(img, masks, p, S):
    flags = p["mask_flags"]
    mask = combine_mask(masks, flags, S / D.IPD_MM) if flags["on"] and masks else None
    mask = add_color_range(img, mask, p["color_filter"], S / D.IPD_MM)
    total, structure = D.combined_mask(img, S, mask, p["shadows"], p["highlights"],
                                       p["tone_strength"], p["protect"], p["protect_mm"], p["protect_thr"])
    return mask, total, structure


CROP_SIDES = {"detail": 2.5, "crop": 6.0}  # side of the preview crop in units of S (63 mm); anything else = whole image


def get_region(view, S):
    """Preview region (image and mask components, downscaled together). Cached per (view, S)."""
    cx, cy = STATE["centre"]
    key = (view, round(S), round(cx), round(cy))
    if STATE["region_key"] == key:
        return STATE["region"]
    img, masks = STATE["img"], STATE["masks"]
    h, w = img.shape[:2]
    if view in CROP_SIDES:
        side = int(min(CROP_SIDES[view] * S, min(w, h)))
        x0 = int(min(max(0, cx - side / 2), w - side))
        y0 = int(min(max(0, cy - side / 2), h - side))
        sl, max_side = (slice(y0, y0 + side), slice(x0, x0 + side)), 1600
    else:
        sl, max_side = (slice(0, h), slice(0, w)), 1400
    k = min(1.0, max_side / max(img[sl].shape[:2]))

    def rs(a):
        a = a[sl]
        if k < 1:
            return cv2.resize(a, (round(a.shape[1] * k), round(a.shape[0] * k)), interpolation=cv2.INTER_AREA)
        return np.ascontiguousarray(a)

    STATE["region_off"] = (sl[1].start, sl[0].start)
    STATE["region_box"] = (sl[1].start, sl[0].start, sl[1].stop - sl[1].start, sl[0].stop - sl[0].start)
    reg = rs(img)
    mreg = {n: rs(a) for n, a in masks.items()} if masks else None
    STATE["region_key"], STATE["region"] = key, (reg, k, mreg)
    return STATE["region"]


def api_load(req):
    path = Path(req["path"]).expanduser()
    if path.suffix.lower() not in IMG_EXT or not path.is_file():
        raise ValueError(f"not an image file: {path}")
    raw = cv2.imread(str(path), cv2.IMREAD_COLOR_BGR | cv2.IMREAD_ANYDEPTH)
    if raw is None:
        raise ValueError(f"cannot read {path}")
    img = raw.astype(np.float32) / (65535.0 if raw.dtype == np.uint16 else 255.0)
    source_img = img
    with Image.open(path) as source:
        source_icc = source.info.get("icc_profile")
    original = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    opaque = not (original.ndim == 3 and original.shape[2] == 4 and
                  np.any(original[..., 3] != np.iinfo(original.dtype).max))
    h, w = img.shape[:2]
    raw8 = raw if raw.dtype == np.uint8 else (raw / 257.0).astype(np.uint8)
    mode, icc_out, converted, colour_note = colour_plan(path, raw8)
    if converted is not None:
        raw8, img = converted, converted.astype(np.float32) / 255.0
    D.set_colour_mode(mode)
    t0 = time.time()
    t = M.pick_target(path, raw8)
    S = centre = masks = seen = pbox = None
    conf, desc, mask_pct = "none", "no usable scale cue", None
    skin_colour = None
    if t is not None:
        S, conf, desc, centre = t["S"], t["conf"], t["desc"], t["centre"]
        res = K.skin_mask(raw8, t["face_info"], M.person_box(t, raw8.shape), S)
        masks = {"A": res["A"], "features": res["features"], "colour": res["colour"]}
        seen, pbox = t["seen"], M.person_box(t, raw8.shape)
        mask_pct = round(100 * float((res["skin"] > 0.5).mean()), 1)
        sel = res["skin"] > 0.5
        if sel.any():
            bgr = img[sel].mean(axis=0)
            hsv = cv2.cvtColor(np.asarray([[bgr]], dtype=np.float32), cv2.COLOR_BGR2HSV)[0, 0]
            skin_light = float(D.to_srgb(float(D.luminance_linear(img[sel]).mean())))
            skin_colour = {"hue": float(hsv[0]), "saturation": float(hsv[1]), "light": skin_light,
                           "rgb": np.round(np.clip(bgr[::-1], 0, 1) * 255).astype(int).tolist()}
    STATE.update(img=img, path=str(path), S=S, centre=centre or (w / 2, h / 2), subject=centre or (w / 2, h / 2), imgs={}, region_key=None, region=None, masks=masks, icc=icc_out,
                 seen=seen, pbox=pbox, last=None, struct=None, avers=0,
                 source_img=source_img, source_icc=source_icc, source_opaque=opaque)
    BRIDGE["doc"] = None
    return {"name": path.name, "width": w, "height": h, "S": S, "confidence": conf, "desc": desc,
            "seconds": round(time.time() - t0, 1), "has_mask": masks is not None, "mask_pct": mask_pct, "colour": colour_note, "skin_colour": skin_colour}


COCO_BONES = [(5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
              (0, 1), (0, 2), (1, 3), (2, 4)]
ANALYSIS_KEYS = ("detect", "skin", "structure")


def render_analysis(flags):
    """Original with the analysis drawn on it, in the preview region's pixels: detected faces and bodies (target
    bright), skin mask (green), parts left out of the skin (magenta features, orange off-colour), narrow structure (red)."""
    L = STATE["last"]
    if L is None:
        return
    flags = {k: bool(flags.get(k, True)) for k in ANALYSIS_KEYS}
    reg, k, (ox, oy), S = L["reg"], L["k"], L["off"], L["S"]
    out = reg.copy()

    def blend(m, bgr, a):
        m = (m * a)[..., None]
        out[:] = out * (1 - m) + np.array(bgr, np.float32) * m

    mreg = L["mreg"]
    if flags["skin"] and mreg:
        blend(np.clip(mreg["A"], 0, 1) * (L["skin"] if L["skin"] is not None else 1.0), (0, 1, 0), 0.45)
        blend(np.clip(mreg["features"], 0, 1) * (mreg["A"] > 0.5), (1, 0, 1), 0.7)
        blend((1 - np.clip(mreg["colour"], 0, 1)) * (mreg["A"] > 0.5), (0, 0.55, 1), 0.7)
    if flags["structure"]:
        key = (S, L["mm"], L["thr"])
        if STATE["struct"] is None or STATE["struct"][0] != key:
            STATE["struct"] = (key, D.structure_map(D.luminance_linear(reg).astype(np.float32), S, L["mm"], L["thr"]))
        blend(STATE["struct"][1], (0, 0, 1), 0.6)
    o = u8(out).copy()
    if flags["detect"] and STATE["seen"]:
        th = max(1, round(max(o.shape[:2]) / 700))

        def pt(x, y):
            return (round((x - ox) * k), round((y - oy) * k))

        for sub in STATE["seen"]:
            tgt = sub["target"]
            col, dim = ((0, 255, 255), (0, 200, 255)) if tgt else ((200, 200, 200), (150, 150, 150))
            f = sub["face"]
            if f is not None:
                x0, y0 = pt(f[0], f[1])
                x1, y1 = pt(f[0] + f[2], f[1] + f[3])
                cv2.rectangle(o, (x0, y0), (x1, y1), col, th)
                for lx, ly in f[4:14].reshape(-1, 2):
                    cv2.circle(o, pt(lx, ly), th + 1, col, -1)
            fi = sub["face_info"]
            if fi is not None and fi[2] >= 0.5:
                P = fi[0][:, :2]
                for ids in (K.L_EYE, K.R_EYE, K.LIPS, K.L_BROW, K.R_BROW):
                    cv2.polylines(o, [np.array([pt(*P[i]) for i in ids], np.int32)], True, (255, 255, 0), max(1, th - 1))
            kp, sc = sub["kp"], sub["sc"]
            if kp is not None:
                for a, b in COCO_BONES:
                    if sc[a] >= 0.3 and sc[b] >= 0.3:
                        cv2.line(o, pt(*kp[a]), pt(*kp[b]), dim, th)
                for i in range(17):
                    if sc[i] >= 0.3:
                        cv2.circle(o, pt(*kp[i]), th + 2, col, -1)
        if STATE["pbox"] is not None:
            x0, y0, x1, y1 = STATE["pbox"]
            cv2.rectangle(o, pt(x0, y0), pt(x1, y1), (255, 128, 0), max(1, th - 1))
    STATE["avers"] += 1
    STATE["imgs"]["analysis"] = encode(o, ".jpg", 92, STATE["icc"])


def api_analysis(req):
    if STATE["last"] is None:
        raise ValueError("no result yet")
    render_analysis(req.get("analysis", {}))
    return {"avers": STATE["avers"]}


def api_centre(req):
    """Move the preview crop: to the clicked spot (u, v = fractions of the shown preview) or back to the subject."""
    if STATE["img"] is None:
        raise ValueError("load an image first")
    if req.get("reset"):
        STATE["centre"] = STATE["subject"]
    else:
        bx, by, bw, bh = STATE["region_box"]
        h, w = STATE["img"].shape[:2]
        STATE["centre"] = (min(max(bx + float(req["u"]) * bw, 0), w), min(max(by + float(req["v"]) * bh, 0), h))
    return {}


D_PICK_MM = 1.0  # radius of the patch averaged for a pick


def api_color_pick(req):
    if STATE["last"] is None or int(req.get("version", -1)) != STATE["version"]:
        raise ValueError("wait for the preview to finish updating before picking a color")
    u, v = float(req["u"]), float(req["v"])
    if not math.isfinite(u) or not math.isfinite(v) or not 0 <= u <= 1 or not 0 <= v <= 1:
        raise ValueError("pick a point inside the image")
    reg = STATE["last"]["reg"]
    h, w = reg.shape[:2]
    bgr = reg[min(h - 1, int(v * h)), min(w - 1, int(u * w))]
    hsv = cv2.cvtColor(np.asarray([[bgr]], dtype=np.float32), cv2.COLOR_BGR2HSV)[0, 0]
    rgb = np.round(np.clip(bgr[::-1], 0, 1) * 255).astype(int).tolist()
    r = max(1, int(round(D_PICK_MM * STATE["last"]["S"] / D.IPD_MM)))
    y0, y1, x0, x1 = max(0, int(v * h) - r), min(h, int(v * h) + r + 1), max(0, int(u * w) - r), min(w, int(u * w) + r + 1)
    light = float(D.to_srgb(float(D.luminance_linear(reg[y0:y1, x0:x1]).mean())))
    return {"hue": float(hsv[0]), "saturation": float(hsv[1]), "value": float(hsv[2]), "rgb": rgb, "light": light}


def api_run(req):
    if STATE["img"] is None:
        raise ValueError("load an image first")
    p = parse_params(req)
    view = req.get("view", "crop")
    reg, k, mreg = get_region(view, p["S"])
    if p["mode"] != "dnb":
        return run_skin_preview(p, reg, k, mreg)
    flags = p["mask_flags"]
    mask = combine_mask(mreg, flags, p["S"] * k / D.IPD_MM) if (flags["on"] and mreg) else None
    mask = add_color_range(reg, mask, p["color_filter"], p["S"] * k / D.IPD_MM)
    t = time.time()
    cs, after, change, diag, total = D.process(reg, p["S"] * k, p["bands"], p["strength"], p["edge"], p["tau"], p["max_ev"],
                                               p["shadows"], p["highlights"], mask=mask, tone_strength=p["tone_strength"],
                                               protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"])
    d = (after - reg).mean(axis=2)
    stats = {"p95": float(np.percentile(np.abs(d), 95) * 255), "max": float(np.abs(d).max() * 255),
             "levels": [[round(c, 2), round(e, 4), round(g, 2)] for c, e, g in diag["levels"]],
             "preview_px": [reg.shape[1], reg.shape[0]], "seconds": round(time.time() - t, 2)}
    stats["mask_used"] = total is not None
    stats["mask_pct"] = round(100 * float((total > 0.5).mean()), 1) if total is not None else None
    show = total if total is not None else np.ones(reg.shape[:2], np.float32)
    STATE["last"] = {"reg": reg, "k": k, "skin": mask, "mreg": mreg, "S": p["S"] * k, "off": STATE["region_off"],
                     "mm": p["protect_mm"], "thr": p["protect_thr"]}
    STATE["struct"] = None
    if diag.get("structure") is not None:
        STATE["struct"] = ((p["S"] * k, p["protect_mm"], p["protect_thr"]), diag["structure"])
    lm = cs if total is None else 0.5 + (cs - 0.5) * total
    STATE["version"] += 1
    STATE["imgs"] = {
        "mask": encode(u8(show), ".png"),
        "orig": encode(u8(reg), ".jpg", 92, STATE["icc"]),
        "result": encode(u8(after), ".jpg", 92, STATE["icc"]),
        "layer": encode(u8(cs), ".png"),
        "layerx": encode(u8(0.5 + (cs - 0.5) * 8), ".png"),
        # the layer as it acts through the mask: 50% gray (no change) where the mask is 0
        "layerm": encode(u8(lm), ".png"),
        "layermx": encode(u8(0.5 + (lm - 0.5) * 8), ".png"),
    }
    render_analysis(p["analysis"])
    return {"version": STATE["version"], "avers": STATE["avers"], "stats": stats}


def source_region(shape):
    x, y, w, h = STATE["region_box"]
    src = STATE["source_img"][y:y + h, x:x + w]
    return cv2.resize(src, (shape[1], shape[0]), interpolation=cv2.INTER_AREA) if src.shape != shape else src


def frequency_layers(img, mask, p, scale=1):
    if not STATE["source_opaque"]:
        raise ValueError("frequency separation currently needs an opaque merged image; flatten transparency before Get")
    f = p["frequency"]
    return FS.separate(img, mask, f["method"], f["radius"] * scale, f["blur_radius"] * scale)


def run_skin_preview(p, reg, k, mreg):
    t = time.time()
    mask, total, structure = tool_mask(reg, mreg, p, p["S"] * k)
    show = np.ones(reg.shape[:2], np.float32) if total is None else total
    STATE["last"] = {"reg": reg, "k": k, "skin": mask, "mreg": mreg, "S": p["S"] * k,
                     "off": STATE["region_off"], "mm": p["protect_mm"], "thr": p["protect_thr"]}
    STATE["struct"] = ((p["S"] * k, p["protect_mm"], p["protect_thr"]), structure) if structure is not None else None
    imgs = {"orig": encode(u8(reg), ".jpg", 92, STATE["icc"]), "mask": encode(u8(show), ".png")}
    if p["mode"] == "mask":
        imgs["result"] = encode(u8(show), ".jpg", 92)
    else:
        src = source_region(reg.shape)
        layers = frequency_layers(src, show, p, k)
        icc = STATE["source_icc"]
        imgs["orig"] = encode(u8(src), ".jpg", 92, icc)
        imgs.update({key: encode(u8(a), ".png", icc=icc) for key, a in layers.items()})
        quantized = {key: FS.rgb16(a).astype(np.float32) / 65535 for key, a in layers.items()}
        opacity = u8(show).astype(np.float32) / 255
        masked_blur = quantized["color"] + (quantized["blur"] - quantized["color"]) * opacity[..., None]
        imgs["blur"] = encode(u8(masked_blur), ".png", icc=icc)
        result = FS.composite(quantized, opacity)
        imgs["result"] = encode(u8(result), ".jpg", 92, icc)
    STATE["imgs"] = imgs
    STATE["version"] += 1
    render_analysis(p["analysis"])
    return {"version": STATE["version"], "avers": STATE["avers"], "stats": {
        "preview_px": [reg.shape[1], reg.shape[0]], "seconds": round(time.time() - t, 2),
        "mask_used": total is not None, "mask_pct": round(100 * float((show > 0.5).mean()), 1)}}


def api_save(req):
    """Writes the three files for a layer-based workflow (Photoshop / PhotoCraft):
    1_result: the picture with the settings applied, 2_skinmask: the combined mask to apply to layer 3, and
    3_softlight_layer: 16-bit gray layer for Soft Light mode with no mask of any kind applied."""
    if STATE["img"] is None:
        raise ValueError("load an image first")
    p = parse_params(req)
    if p["mode"] != "dnb":
        return save_skin(req, p)
    t = time.time()
    img = STATE["img"]
    flags = p["mask_flags"]
    full_mask = combine_mask(STATE["masks"], flags, p["S"] / D.IPD_MM) if STATE["masks"] else None
    mask = full_mask if flags["on"] else None
    mask = add_color_range(img, mask, p["color_filter"], p["S"] / D.IPD_MM)
    cs, after, change, diag, total = D.process(img, p["S"], p["bands"], p["strength"], p["edge"], p["tau"], p["max_ev"],
                                               p["shadows"], p["highlights"], mask=mask, tone_strength=p["tone_strength"],
                                               protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"])
    stem = Path(STATE["path"]).stem
    out = ROOT / "results" / f"{stem}_{datetime.datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}_1_result.jpg").write_bytes(encode(u8(after), ".jpg", 95, STATE["icc"]))
    mask_out = total if total is not None else np.ones(img.shape[:2], np.float32)
    cv2.imwrite(str(out / f"{stem}_2_skinmask.png"), u8(mask_out))
    # Three identical channels, not a Gray image: PhotoCraft (and Photoshop) convert a Gray image to RGB with a gray
    # profile, which shifts 50% gray and made the layer brighten the picture by about 8 levels in a test.
    g16 = np.round(cs * 65535).astype(np.uint16)
    cv2.imwrite(str(out / f"{stem}_3_softlight_layer.png"), np.dstack([g16, g16, g16]))
    d = (after - img).mean(axis=2)
    settings = {k: v for k, v in req.items() if k != "view"}
    settings.update(image=STATE["path"], stats={"p95": float(np.percentile(np.abs(d), 95) * 255), "max": float(np.abs(d).max() * 255)},
                    files={"1_result": "picture with the settings applied (combined mask applied)",
                           "2_skinmask": "ONE mask = skin x color range x tone range x structure protection (8-bit gray; all white if none are used); use as the layer mask of file 3",
                           "3_softlight_layer": "16-bit RGB PNG (three identical gray channels, so no gray-profile conversion on import), Soft Light blend mode, 50% gray = no change, NO mask applied"},
                    soft_light="W3C soft-light formula; Photoshop's soft light differs slightly",
                    colour=("embedded ICC profile written into the result JPEG" if STATE["icc"] else "no profile written (source had none)"))
    (out / "settings.json").write_text(json.dumps(settings, indent=2))
    return {"dir": str(out), "files": sorted(f.name for f in out.iterdir()), "seconds": round(time.time() - t, 1)}


def save_skin(req, p):
    t = time.time()
    _, total, _ = tool_mask(STATE["img"], STATE["masks"], p, p["S"])
    mask = np.ones(STATE["img"].shape[:2], np.float32) if total is None else total
    layers = frequency_layers(STATE["source_img"], mask, p) if p["mode"] == "frequency" else {}
    stem = Path(STATE["path"]).stem
    out = ROOT / "results" / f"{stem}_{p['mode']}_{datetime.datetime.now():%Y%m%d-%H%M%S-%f}"
    out.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out / "skinmask.png"), u8(mask)):
        raise ValueError("could not write skin mask")
    for key, pixels in layers.items():
        (out / f"{key}.png").write_bytes(PCB.tagged_rgb16(FS.rgb16(pixels), STATE["source_icc"] or SRGB_ICC))
    (out / "settings.json").write_text(json.dumps({**req, "image": STATE["path"],
        "layer_recipe": ("Detail: Linear Light, unmasked; Blur: Normal with skinmask.png; Color: Normal, unmasked. "
                         "Group: Normal, unmasked. Top-down: Detail / Blur / Color."
                         if layers else "skinmask.png: reusable grayscale layer in Normal mode."),
        "pixels": "Separation radius: Gaussian sigma or median square radius, full-image pixels; Blur: Gaussian sigma. "
                  "Median uses equal votes among positive-mask samples. RGB16, source numeric space."}, indent=2))
    return {"dir": str(out), "files": sorted(f.name for f in out.iterdir()), "seconds": round(time.time() - t, 1)}


BRIDGE = {"b": None, "doc": None}


def bridge():
    if BRIDGE["b"] is None:
        BRIDGE["b"] = PCB.Bridge()
    return BRIDGE["b"]


def api_pc_status(req):
    try:
        st = bridge().status()
        return {"connected": True, "doc": st["doc"], "fetched": BRIDGE["doc"], "headless": st["headless"]}
    except PCB.ControlError as e:
        return {"connected": False, "message": str(e), "fetched": BRIDGE["doc"]}


def api_pc_fetch(req):
    path, doc = bridge().fetch(ROOT / "inbox")
    res = api_load({"path": str(path)})
    BRIDGE["doc"] = doc
    res["pc_doc"] = doc
    return res


def api_pc_send(req):
    if STATE["img"] is None or BRIDGE["doc"] is None:
        raise ValueError("get a picture from PhotoCraft first")
    p = parse_params(req)
    img = STATE["img"]
    if p["mode"] != "dnb":
        _, total, _ = tool_mask(img, STATE["masks"], p, p["S"])
        mask = np.ones(img.shape[:2], np.float32) if total is None else total
        if p["mode"] == "mask":
            r = bridge().send_mask(u8(mask), BRIDGE["doc"])
        else:
            layers = frequency_layers(STATE["source_img"], mask, p)
            r = bridge().send_frequency({key: FS.rgb16(a) for key, a in layers.items()}, u8(mask),
                                        BRIDGE["doc"], STATE["source_icc"])
        return {"sent": True, **r}
    flags = p["mask_flags"]
    full_mask = combine_mask(STATE["masks"], flags, p["S"] / D.IPD_MM) if STATE["masks"] else None
    mask = full_mask if flags["on"] else None
    mask = add_color_range(img, mask, p["color_filter"], p["S"] / D.IPD_MM)
    cs, after, change, diag, total = D.process(img, p["S"], p["bands"], p["strength"], p["edge"], p["tau"], p["max_ev"],
                                               p["shadows"], p["highlights"], mask=mask, tone_strength=p["tone_strength"],
                                               protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"])
    mask_out = total if total is not None else np.ones(img.shape[:2], np.float32)
    g16 = np.round(cs * 65535).astype(np.uint16)
    r = bridge().send(np.dstack([g16, g16, g16]), u8(mask_out), BRIDGE["doc"])
    return {"sent": True, **r}


def api_pc_launch(req):
    return bridge().launch()


def read_presets():
    try:
        d = json.loads(PRESETS.read_text())
    except Exception:
        d = {}
    return {"default": d.get("default"), "presets": d.get("presets", {})}


def write_presets(d):
    PRESETS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(PRESETS)


def api_preset_save(req):
    name = str(req.get("name", "")).strip()
    if not name or len(name) > 60:
        raise ValueError("give the preset a name (1-60 characters)")
    d = read_presets()
    existed = name in d["presets"]
    d["presets"][name] = req["settings"]
    write_presets(d)
    return {"name": name, "updated": existed, **d}


def api_preset_default(req):
    d = read_presets()
    d["default"] = req["settings"]
    write_presets(d)
    return d


def api_preset_delete(req):
    d = read_presets()
    if req.get("name") not in d["presets"]:
        raise ValueError("no such preset")
    del d["presets"][req["name"]]
    write_presets(d)
    return d


def api_tone(req):
    sh, hl = D.tone_from_mid(float(req["mid"]), float(req["range"]), float(req["taper"]))
    v = np.linspace(0, 1, 101)
    return {"v": v.tolist(), "w": D.tone_weight(v, sh, hl).tolist()}


def api_profile(req):
    bands = [(float(b[0]), float(b[1]), float(b[2]), max(float(b[3]), 1e-3)) for b in req["bands"] if float(b[1]) > float(b[0])]
    sizes = [0.5 * (400 ** (i / 119)) for i in range(120)]
    return {"sizes": sizes, "gains": [float(req.get("strength", 1.0)) * (D.band_gain(s, bands) if bands else 0.0) for s in sizes]}


class Handler(BrowserHandler, BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self.send(200, json.dumps({**identity("dnb", ROOT, MODELS),
                "ui_lifecycle": 1 if getattr(self.server, "lifetime", None) else 0}).encode(), "application/json")
        if path == "/static/lifecycle.js":
            return self.send(200, script(), "text/javascript; charset=utf-8")
        if path == "/static/shp-ui.css":
            return self.send(200, stylesheet(), "text/css; charset=utf-8")
        if path in ("/", "/index.html"):
            return self.send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/presets":
            return self.send(200, json.dumps(read_presets()).encode(), "application/json")
        if path == "/api/images":
            return self.send(200, json.dumps(list_images()).encode(), "application/json")
        if path.startswith("/img/"):
            kind = path[5:]
            data = STATE["imgs"].get(kind)
            if data is None:
                return self.send(404, b"no image yet", "text/plain")
            return self.send(200, data, "image/jpeg" if kind in ("orig", "result", "analysis") else "image/png")
        self.send(404, b"not found", "text/plain")

    def do_api_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            fn = {"/api/load": api_load, "/api/run": api_run, "/api/centre": api_centre, "/api/color-pick": api_color_pick, "/api/analysis": api_analysis, "/api/save": api_save, "/api/profile": api_profile, "/api/tone": api_tone,
                  "/api/pc/status": api_pc_status, "/api/pc/fetch": api_pc_fetch, "/api/pc/send": api_pc_send, "/api/pc/launch": api_pc_launch,
                  "/api/presets/save": api_preset_save, "/api/presets/default": api_preset_default, "/api/presets/delete": api_preset_delete}.get(self.path)
            if fn is None:
                return self.send(404, b"not found", "text/plain")
            with LOCK:
                res = fn(req)
            self.send(200, json.dumps(res).encode(), "application/json")
        except Exception as e:  # report to the page instead of crashing the server
            self.send(400, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def main():
    ap = argparse.ArgumentParser(description="SHP PhotoCraft D&B web UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--close-with-ui", action="store_true", help="stop after the last UI tab closes (launcher use)")
    ap.add_argument("--dir", nargs="+", default=[])
    a = ap.parse_args()
    DIRS.extend(a.dir)
    srv = BrowserServer(("127.0.0.1", a.port), Handler, close_with_ui=a.close_with_ui,
                        loaded=lambda: STATE["img"] is not None)
    print(f"SHP PhotoCraft D&B: http://127.0.0.1:{srv.server_port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
