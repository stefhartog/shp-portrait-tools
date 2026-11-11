"""SHP Portrait Tools Skin Tools: local skin-mask, FS/tone, dodge & burn and Combine UI.

Run: uv run shp-photocraft-dnb [--port 8765] [--dir pictures ...]
Then open http://127.0.0.1:8765 in a browser. The server only listens on 127.0.0.1.

The page lets you pick a photo, set bands (feature-size range in mm, gain, taper) and global settings, press Apply to
see Original | Result | Soft-light layer side by side, and Save to write, at full resolution, 1_result (picture with
the settings), 2_skinmask and 3_softlight_layer (no mask applied) plus settings.json to results/<name>_<time>/.
The skin mask (segmenter minus eyes/brows/lips/nostrils minus off-colour pixels) is computed once at Load.
Presets (named settings, plus one default loaded at start) are stored in $DNB_DATA/presets.json.
Global result/transfer file options live in the app config settings.json, outside module presets.
Previews are computed on a downscaled copy (max ~1600 px) of the subject crop or the whole image; Save always
works at full resolution.
"""
import argparse
import datetime
import io
import hashlib
import json
import math
import threading
import tempfile
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import nullcontext
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms
from shp_photocraft_tools.shared.ui import stylesheet, theme_script
from shp_photocraft_tools.shared.health import identity
from shp_photocraft_tools.shared.lifecycle import BrowserHandler, BrowserServer, script
from shp_photocraft_tools.shared.local_output import write_local_result, batch_output

from . import dodge_burn as D
from . import skin_mask as K
from . import skin_mask_compare as M
from . import photocraft_bridge as PCB
from . import frequency_separation as FS
from . import skintone as ST
from . import directional_blur as DB
from . import combine_scale as C
from . import estimate_body_scale as B
from . import progress
from .mask_precache import MaskPrecache, Superseded
from shp_photocraft_tools.shared.watch_folder import WatchFolder, capture_target
from .paths import DATA, MODELS

ROOT = DATA
HTML = Path(__file__).resolve().parent / "ui" / "index.html"
PRESETS = ROOT / "presets.json"
GLOBAL_SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shp-photocraft-tools" / "settings.json"
GLOBAL_SETTINGS_LOCK = threading.Lock()
WATCH_DIRECTORY = Path("/home/stef/shp-photocraft-tools/watchfolder")
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
LOCK = threading.Lock()
FULL_MASK_LOCK = threading.Lock()
FULL_MASK_CACHE_LIMIT = 256 * 1024 * 1024
PREVIEW_CACHE_LIMIT = 192 * 1024 * 1024
STATE = {"img": None, "path": None, "S": None, "centre": None, "imgs": {}, "version": 0, "region_key": None, "region": None,
         "masks": None, "icc": None, "source_img": None, "source_icc": None, "source_opaque": True, "skintone_error": None,
         "seen": None, "pbox": None, "last": None, "struct": None, "avers": 0, "region_off": (0, 0), "region_box": None, "subject": None,
         "source_kind": None}
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
        with Image.open(path) as image:
            icc = image.info.get("icc_profile")
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
    if mode not in ("dnb", "mask", "frequency", "skintone", "combine"):
        raise ValueError("mode must be dnb, mask, frequency or skintone")
    if mode == "skintone":
        mode = "frequency"
    layer_blur = float(p.get("layer_blur_at_s181", 0)) if mode in ("dnb", "combine") else 0
    if not math.isfinite(layer_blur) or not 0 <= layer_blur <= 20:
        raise ValueError("Layer blur at S181 must be 0-20 pixels")
    bands = [(float(b[0]), float(b[1]), float(b[2]), max(float(b[3]), 1e-3)) for b in p.get("bands", []) if float(b[1]) > float(b[0])]
    if not bands and mode in ("dnb", "combine"):
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
    if type(p.get("band1_on", True)) is not bool:
        raise ValueError("band1_on must be a boolean")
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
    combine=p.get("combine", {})
    if not isinstance(combine, dict):
        raise ValueError("Combine settings must be an object")
    mixes={}
    for key in ("fs_mix", "dnb_mix"):
        value=combine.get(key,100)
        if isinstance(value,bool):
            raise ValueError(f"Combine {key} must be 0-100")
        value=float(value)
        if not math.isfinite(value) or not 0<=value<=100:
            raise ValueError(f"Combine {key} must be 0-100")
        mixes[key]=value
    return dict(
        combine=mixes,
        band1_on=p.get("band1_on", True), second_band=D.second_band_settings(p.get("second_band")) if mode in ("dnb", "combine") else D.second_band_settings(),
        mode=mode, layer_blur_at_s181=layer_blur, frequency=FS.settings(p) if mode in ("frequency", "combine") else None,
        skintone=ST.settings(p) if mode in ("frequency", "combine") else None,
        directional=DB.settings(p) if mode in ("frequency", "combine") else None,
        frequency_transfer=PCB.transfer_settings(p.get("frequency_transfer")) if mode in ("frequency", "combine") else None,
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


def base_skin_mask(img, masks, p, S):
    flags = p["mask_flags"]
    mask = combine_mask(masks, flags, S / D.IPD_MM) if flags["on"] and masks else None
    return add_color_range(img, mask, p["color_filter"], S / D.IPD_MM)


def tool_mask(img, masks, p, S):
    mask = base_skin_mask(img, masks, p, S)
    total, structure = D.combined_mask(img, S, mask, p["shadows"], p["highlights"],
                                       p["tone_strength"], p["protect"], p["protect_mm"], p["protect_thr"])
    return mask, total, structure


def full_skin_mask(p):
    total, _ = cached_final_skin_mask(p)
    return np.ones(STATE["img"].shape[:2], np.float32) if total is None else total


def mask_settings_key(p, masks):
    flags = p["mask_flags"] if p["mask_flags"]["on"] and masks else {"on": False}
    effective = {"S": p["S"], "mask_flags": flags,
                 "color_filter": p["color_filter"] if p["color_filter"]["on"] else {"on": False},
                 "shadows": p["shadows"], "highlights": p["highlights"],
                 "tone_strength": p["tone_strength"] if p["shadows"] is not None or p["highlights"] is not None else None,
                 "protect": p["protect"], "protect_mm": p["protect_mm"] if p["protect"] else None,
                 "protect_thr": p["protect_thr"] if p["protect"] else None}
    return json.dumps(effective, sort_keys=True, allow_nan=False)


def full_mask_key(p, img, masks, *, state=None):
    state = STATE if state is None else state
    def digest(array):
        if array is None:
            return None
        return (array.shape, array.dtype.str, hashlib.sha256(memoryview(np.ascontiguousarray(array))).digest())
    flags = p["mask_flags"]
    components = None
    if flags["on"] and masks:
        used = ["A"] + (["features"] if flags["features"] else []) + (["colour"] if flags["colour"] else [])
        components = tuple((key, digest(masks[key])) for key in used)
    source = state.get("source_img")
    return (state.get("landmark_generation"), state.get("path"), id(img), id(masks),
            id(source), digest(img), digest(source) if source is not img else None, components,
            state.get("icc"), state.get("source_icc"), mask_settings_key(p, masks))


def cached_final_skin_mask(p, *, base_mask=None, state=None, commit_guard=None):
    """One content-keyed, read-only final mask/structure entry shared by normal modes."""
    with FULL_MASK_LOCK:
        state = STATE if state is None else state
        guard = commit_guard if commit_guard is not None else lambda: nullcontext(True)
        with guard() as current:
            if not current:
                raise Superseded()
        img, masks = state["img"], state["masks"]
        key = full_mask_key(p, img, masks, state=state)
        saved = STATE.get("full_mask_cache")
        if saved is not None and saved[0] == key:
            with guard() as current:
                if not current:
                    raise Superseded()
                progress.call("Reusing cached full-resolution skin mask", lambda: None)
                STATE["full_mask_status_entry"] = (saved[0], mask_request_key(p))
                return tuple(None if a is None else a.view() for a in saved[1:])
        STATE["full_mask_cache"] = None
        def prepare():
            base = base_mask if base_mask is not None else base_skin_mask(img, masks, p, p["S"])
            return D.combined_mask(img, p["S"], base, p["shadows"], p["highlights"],
                                   p["tone_strength"], p["protect"], p["protect_mm"], p["protect_thr"])
        result = progress.call("Recomputing full-resolution skin mask", prepare)
        with guard() as current:
            if not current:
                raise Superseded()
        if (STATE["img"] is not img or STATE["masks"] is not masks
                or full_mask_key(p, img, masks) != key):
            with guard() as current:
                if not current:
                    raise Superseded()
            raise ValueError("source or mask components changed during mask preparation; load/Get again")
        arrays = tuple(None if a is None else np.ascontiguousarray(a, dtype=np.float32) for a in result)
        if sum(a.nbytes for a in arrays if a is not None) <= FULL_MASK_CACHE_LIMIT:
            with guard() as current:
                if not current:
                    raise Superseded()
                for a in arrays:
                    if a is not None:
                        a.flags.writeable = False
                STATE["full_mask_cache"] = (key, *arrays)
                STATE["full_mask_status_entry"] = (key, mask_request_key(p))
                return tuple(None if a is None else a.view() for a in arrays)
        progress.call("Full-resolution mask exceeds cache budget; not retained", lambda: None)
        return arrays


def mask_request_key(p):
    return (STATE.get("landmark_generation"), STATE.get("path"), STATE.get("icc"), STATE.get("source_icc"),
            id(STATE["img"]), id(STATE["source_img"]),
            id(STATE["masks"]), tuple((key, id(a)) for key, a in (STATE["masks"] or {}).items()),
            mask_settings_key(p, STATE["masks"]))


def precache_mask(job, guard):
    p, state = job
    # Background progress owns its record; never append to a completed preview.
    token = f"mask-precache-{time.monotonic_ns()}"
    try:
        with progress.operation(token):
            cached_final_skin_mask(p, state=state, commit_guard=guard)
    finally:
        progress.snapshot(token, consume=True)


MASK_PRECACHE = MaskPrecache(precache_mask)


def mask_cache_status():
    status = MASK_PRECACHE.status()
    saved = STATE.get("full_mask_cache")
    entry = STATE.get("full_mask_status_entry")
    status["cached"] = bool(saved is not None and entry is not None
                            and entry[0] == saved[0] and entry[1] == STATE.get("mask_status_key"))
    status["enabled"] = STATE.get("mask_precache_enabled", True)
    return status


def queue_mask_precache(p):
    if p["mode"] not in ("mask", "dnb", "frequency", "combine"):
        return
    STATE["mask_status_key"] = mask_request_key(p)
    count = STATE["img"].shape[0] * STATE["img"].shape[1] * 4 * (2 if p["protect"] else 1)
    if count > FULL_MASK_CACHE_LIMIT:
        MASK_PRECACHE.skip(mask_request_key(p), "Background mask skipped: exceeds the 256 MiB cache budget.")
        return
    state = {key: STATE.get(key) for key in ("img", "source_img", "masks", "path",
                                             "landmark_generation", "icc", "source_icc")}
    MASK_PRECACHE.request(mask_request_key(p), (deepcopy(p), state))


CROP_SIDES = {"detail": 2.5, "crop": 6.0}  # side of the preview crop in units of S (63 mm); anything else = whole image


def get_region(view, S):
    """Preview region (image and mask components, downscaled together). Cached per (view, S)."""
    cx, cy = STATE["centre"]
    key = (view, S, cx, cy)
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
    started = time.perf_counter()
    path, img, source_img, raw8, mode, icc_out, source_icc, opaque, skintone_error, colour_note = \
        progress.call("Loading image / preparing profile", prepare_loaded_image, req)
    h, w = img.shape[:2]
    return finish_loaded_image(path, img, source_img, raw8, mode, icc_out, source_icc,
                               opaque, skintone_error, colour_note, h, w, started)


def prepare_loaded_image(req, *, set_colour=True):
    path = Path(req["path"]).expanduser().absolute()
    if path.suffix.lower() not in IMG_EXT or not path.is_file():
        raise ValueError(f"not an image file: {path}")
    raw = cv2.imread(str(path), cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
    if raw is None:
        raise ValueError(f"cannot read {path}")
    if raw.ndim == 2:
        raw = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
    if raw.dtype in (np.uint8, np.uint16):
        img = raw.astype(np.float32)
        img /= np.iinfo(raw.dtype).max
    elif np.issubdtype(raw.dtype, np.floating):
        img = raw.astype(np.float32, copy=False)
    else:
        raise ValueError(f"unsupported image sample type: {raw.dtype}")
    if not np.isfinite(img).all():
        raise ValueError("image contains non-finite RGB samples")
    source_img = img
    skintone_error = ST.source_error(source_img)
    with Image.open(path) as source:
        source_icc = source.info.get("icc_profile")
    original = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    alpha_max = np.iinfo(original.dtype).max if np.issubdtype(original.dtype, np.integer) else 1.0
    opaque = not (original.ndim == 3 and original.shape[2] == 4 and
                  np.any(original[..., 3] != alpha_max))
    del original
    h, w = img.shape[:2]
    raw8 = (raw if raw.dtype == np.uint8 else
            (raw / 257.0).astype(np.uint8) if raw.dtype == np.uint16 else u8(img))
    del raw
    mode, icc_out, converted, colour_note = colour_plan(path, raw8)
    if converted is not None:
        raw8, img = converted, converted.astype(np.float32) / 255.0
    if set_colour:
        D.set_colour_mode(mode)
    return path, img, source_img, raw8, mode, icc_out, source_icc, opaque, skintone_error, colour_note


def finish_loaded_image(path, img, source_img, raw8, mode, icc_out, source_icc,
                        opaque, skintone_error, colour_note, h, w, started, *, publish=True):
    prepared = time.perf_counter()
    t = progress.call("Analyzing image / estimating S", M.pick_target, path, raw8, include_unscaled=True)
    analyzed = time.perf_counter()
    mask_started = None
    S = centre = masks = seen = pbox = None
    conf, desc, mask_pct = "none", "no usable scale cue", None
    skin_colour = None
    if t is not None and t["S"] is not None:
        S, conf, desc, centre = t["S"], t["conf"], t["desc"], t["centre"]
        mask_started = time.perf_counter()
        res = progress.call("Generating shared skin mask", K.skin_mask,
                            raw8, t["face_info"], M.person_box(t, raw8.shape), S)
        masks = {"A": res["A"], "features": res["features"], "colour": res["colour"]}
        seen, pbox = t["seen"], M.person_box(t, raw8.shape)
        mask_pct = round(100 * float((res["skin"] > 0.5).mean()), 1)
        sel = res["skin"] > 0.5
        if sel.any():
            skin_colour = progress.call("Measuring masked skin color", initial_skin_colour, img, sel)
    mask_finished = time.perf_counter()
    if t is not None:
        seen = t["seen"]
    if masks is None:
        progress.call("Mask not generated: no usable detected scale", lambda: None)
    directory = path.parent.stat()
    loaded = dict(img=img, path=str(path), S=S, centre=centre or (w / 2, h / 2), subject=centre or (w / 2, h / 2), imgs={}, region_key=None, region=None, masks=masks, icc=icc_out,
                 seen=seen, pbox=pbox, last=None, struct=None, avers=0,
                 source_img=source_img, source_icc=source_icc, source_opaque=opaque, skintone_error=skintone_error,
                 landmark_generation=STATE.get("landmark_generation", 0) + 1, landmark_overrides={}, preview_cache={},
                 full_mask_cache=None,
                 source_kind="local", source_directory=(directory.st_dev, directory.st_ino))
    loaded["pose"] = ({"kp": np.array(t["kp"], copy=True),
                      **({"sc": np.array(t["sc"], copy=True)} if t.get("sc") is not None else {})}
                     if t is not None and t.get("kp") is not None else None)
    info = {"name": path.name, "width": w, "height": h, "S": S, "confidence": conf, "desc": desc,
            "seconds": time.perf_counter() - started,
            "timings": {"loaded": prepared - started, "analyzed": analyzed - prepared,
                        "mask": mask_finished - mask_started if mask_started is not None else None},
            "has_mask": masks is not None, "mask_pct": mask_pct, "colour": colour_note, "skin_colour": skin_colour,
            "skintone_error": skintone_error, "source_kind": "local"}
    if not publish:
        return loaded, info
    MASK_PRECACHE.reset()
    STATE.update(loaded)
    BRIDGE["doc"] = None
    return {**info, "generation": STATE["landmark_generation"], "directional_pose": directional_availability()}


def batch_sources(req):
    folder, paths = req.get("folder"), req.get("paths")
    if (folder is None) == (paths is None):
        raise ValueError("select one input folder or image files from one folder")
    if folder is not None:
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("input folder must be a nonempty path")
        directory = Path(folder).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("input folder is not a directory")
        sources = sorted((p for p in directory.iterdir() if p.suffix.lower() in IMG_EXT and p.is_file()),
                         key=lambda p: p.name)
    else:
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise ValueError("select at least one image file")
        sources = [Path(p).expanduser().absolute() for p in paths]
        directory = sources[0].parent.resolve(strict=True)
        sources = [p.parent.resolve(strict=True) / p.name for p in sources]
        if any(p.parent != directory for p in sources):
            raise ValueError("all batch images must come from the same folder")
        if any(p.suffix.lower() not in IMG_EXT or not p.is_file() for p in sources):
            raise ValueError("select supported local image files only")
        if len(set(sources)) != len(sources):
            raise ValueError("the same image was selected more than once")
    if not sources:
        raise ValueError("the folder has no supported images in its direct children")
    return directory, sources


def api_batch_images(req):
    directory, sources = batch_sources(req)
    return {"folder": str(directory), "items": [{"name": p.name, "path": str(p)} for p in sources]}


def api_batch_cancel(req):
    return progress.cancel_batch(req.get("token"))


def api_batch(req):
    """A local-only Combine run; no editor state, PhotoCraft document or Crop work is replaced."""
    directory, sources = batch_sources(req)
    settings = deepcopy(req.get("settings"))
    if not isinstance(settings, dict) or settings.get("mode") != "combine":
        raise ValueError("batch processing supports Combine settings only")
    settings.pop("generation", None)
    settings.pop("view", None)
    settings.pop("S", None)
    settings.pop("frequency_transfer", None)
    transfer = read_global_settings()["layer_transfer"]
    parse_params({**settings, "S": 181})
    started = time.perf_counter()
    items = [{"name": path.name, "path": str(path), "status": "pending"} for path in sources]
    with batch_output(directory) as (folder, write, write_preset):
        preset = {**settings, "batch": {"input_folder": str(directory),
                  "scale": "independently detected for each image", "output_settings": transfer, "items": items}}
        preset_path = write_preset(preset)
        progress.batch_status(folder, items)
        for index, (path, item) in enumerate(zip(sources, items), 1):
            item["status"] = "running"
            progress.batch_status(folder, items)
            try:
                progress.check_batch_cancel()
                image_started = time.perf_counter()
                if path.is_symlink():
                    raise ValueError("batch inputs must be direct files, not symbolic links")
                loaded = progress.call(f"{index}/{len(items)}: Loading {path.name}",
                                       prepare_loaded_image, {"path": str(path)}, set_colour=False)
                progress.check_batch_cancel()
                state, analysis = finish_loaded_image(*loaded, *loaded[1].shape[:2], image_started, publish=False)
                progress.check_batch_cancel()
                if state["S"] is None:
                    raise ValueError("no usable detected scale; batch needs an independently estimated S")
                validate_skintone_source(state)
                effective = {**deepcopy(settings), "S": state["S"]}
                p = parse_params(effective)
                with D.colour_mode(loaded[4]):
                    _, mask, _ = progress.call("Calculating batch skin mask", tool_mask,
                                               state["img"], state["masks"], p, p["S"])
                    if mask is None:
                        mask = np.ones(state["img"].shape[:2], np.float32)
                    _, _, result = progress.call("Combining batch image", combined_result,
                                                 state["source_img"], mask, p, state=state)
                progress.check_batch_cancel()
                data = progress.call("Encoding batch result", PCB.tagged_rgb16,
                                     FS.rgb16(result), state["source_icc"] or SRGB_ICC, transfer)
                progress.check_batch_cancel()
                output_path = progress.call("Saving batch result", write, index, path, data,
                                            extension="." + transfer["format"])
                item.update(status="success", S=p["S"], output=Path(output_path).name,
                            source_analysis=analysis, effective_frequency=p["frequency"])
            except progress.BatchCancelled:
                for remaining in items[index - 1:]:
                    remaining["status"] = "cancelled"
                progress.batch_status(folder, items)
                progress.call("Saving cancelled batch mapping", write_preset, preset)
                break
            except Exception as error:  # Each image is an independent job; report failures and continue.
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            progress.batch_status(folder, items)
            progress.call("Updating batch preset and output mapping", write_preset, preset)
        return {"dir": str(folder), "preset": preset_path, "items": items, "seconds": time.perf_counter() - started,
                "succeeded": sum(item["status"] == "success" for item in items),
                "failed": sum(item["status"] == "failed" for item in items),
                "cancelled": sum(item["status"] == "cancelled" for item in items)}


def directional_availability():
    status = {}
    for side in (*DB.SEGMENTS, "torso"):
        p = DB.settings({"directional": {"on": True, "torso": side == "torso",
                                        **{key: key == side for key in DB.SEGMENTS}}})
        try:
            DB.segments(p, STATE.get("pose"), STATE["img"].shape)
            status[side] = "Available"
        except DB.GeometryUnavailable as error:
            status[side] = str(error)
    return status


def initial_skin_colour(img, sel):
    bgr = img[sel].mean(axis=0)
    hsv = cv2.cvtColor(np.asarray([[bgr]], dtype=np.float32), cv2.COLOR_BGR2HSV)[0, 0]
    skin_light = float(D.to_srgb(float(D.luminance_linear(img[sel]).mean())))
    return {"hue": float(hsv[0]), "saturation": float(hsv[1]), "light": skin_light,
            "rgb": np.round(np.clip(bgr[::-1], 0, 1) * 255).astype(int).tolist()}


def api_hue_center(req):
    generation = req.get("generation")
    if STATE["img"] is None or generation != STATE.get("landmark_generation"):
        raise ValueError("the loaded image changed; Hue Center estimate discarded")
    note = None
    if STATE["masks"] is None:
        note = "no detected skin mask"
    elif STATE["S"] is None:
        note = "no automatic scale for the Color split"
    elif STATE.get("skintone_error"):
        note = STATE["skintone_error"]
    elif not STATE["source_opaque"]:
        note = "the merged source is not opaque"
    if note:
        return {"generation": generation, "center": None,
                "note": f"Hue Center not estimated: {note}; previous Center retained. Set Center manually."}
    p = parse_params({**req, "mode": "frequency", "S": STATE["S"]})
    if not p["skintone"]["on"]:
        return {"generation": generation, "center": None,
                "note": "Skintone correction is off; Hue Center estimation skipped, previous Center retained."}
    native, analysis = STATE["source_img"], STATE["img"]
    h, w = native.shape[:2]
    scale = min(1.0, 1400 / max(h, w))

    def reduced(a):
        return cv2.resize(a, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else a

    # Native values remain untouched; validated SDR area averages can overshoot by ULPs.
    color_source = np.clip(reduced(native), 0, 1)
    components = {key: reduced(a) for key, a in STATE["masks"].items()}
    _, mask, _ = tool_mask(reduced(analysis), components, p, p["S"] * scale)
    if mask is None:
        mask = np.ones(color_source.shape[:2], np.float32)
    color = FS.split(color_source, p["frequency"]["method"], p["frequency"]["radius"] * scale)["color"]
    result = ST.estimate_center(color, mask)
    if generation != STATE.get("landmark_generation"):
        raise ValueError("the loaded image changed; Hue Center estimate discarded")
    return {"generation": generation, **result}


LANDMARK_NAMES = B.KP[1:3] + B.KP[5:17]


def landmark_subjects():
    return STATE["seen"] or [{"face": None, "face_info": None, "kp": None, "sc": None, "cues": [], "target": True}]


def landmark_points(subject):
    points = {}
    kp, sc = subject["kp"], subject["sc"]
    if kp is not None:
        for name in LANDMARK_NAMES:
            i = B.I[name]
            if sc[i] >= B.MIN_SCORE:
                points[name] = {"x": float(kp[i][0]), "y": float(kp[i][1]), "visible": True, "source": "detected"}
    fi = subject["face_info"]
    if fi is not None and fi[2] >= 0.5:
        for name, i in (("l_eye", 468), ("r_eye", 473)):
            points[name] = {"x": float(fi[0][i][0]), "y": float(fi[0][i][1]), "visible": True, "source": "detected"}
    return points


def api_landmarks(req):
    if STATE["img"] is None:
        raise ValueError("load an image first")
    subjects = landmark_subjects()
    h, w = STATE["img"].shape[:2]
    return {"generation": STATE["landmark_generation"], "width": w, "height": h,
            "subjects": [{"label": f"Subject #{i + 1}" + (" (analysis target)" if s["target"] else ""),
                          "points": landmark_points(s), "overrides": STATE["landmark_overrides"].get(i)}
                         for i, s in enumerate(subjects)]}


def api_landmark_estimate(req):
    if STATE["img"] is None or req.get("generation") != STATE.get("landmark_generation"):
        raise ValueError("image changed; reopen scale landmarks")
    index = req.get("subject")
    subjects = landmark_subjects()
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(subjects):
        raise ValueError("select a valid subject")
    overrides = req.get("points", {})
    excluded = req.get("excluded", [])
    if not isinstance(overrides, dict) or not isinstance(excluded, list) or any(n not in LANDMARK_NAMES for n in overrides):
        raise ValueError("invalid landmarks")
    if any(n not in [s[0] for s in B.SEGMENTS] for n in excluded):
        raise ValueError("invalid excluded segment")
    h, w = STATE["img"].shape[:2]
    for p in overrides.values():
        if not isinstance(p, dict) or not isinstance(p.get("visible"), bool):
            raise ValueError("invalid landmark visibility")
        if p["visible"]:
            x, y = p.get("x"), p.get("y")
            if (not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                    or not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= w or not 0 <= y <= h):
                raise ValueError("landmark outside image")
    sub = subjects[index]
    points = landmark_points(sub)
    points.update({n: dict(p, source="manual") for n, p in overrides.items()})
    kp = np.zeros((17, 2), dtype=float)
    sc = np.zeros(17, dtype=float)
    for name, p in points.items():
        if p["visible"]:
            kp[B.I[name]] = (p["x"], p["y"])
            sc[B.I[name]] = 1
    body = B.estimate(kp, sc, excluded)
    unavailable = []
    for name, a, b, _ in B.SEGMENTS:
        if name in excluded:
            continue
        pa, _ = B.point(kp, sc, a)
        pb, _ = B.point(kp, sc, b)
        if pa is None or pb is None or np.linalg.norm(pa - pb) <= 1e-6:
            unavailable.append(name)
    cues = [tuple(c) for c in sub.get("cues", []) if c[0] != "body"]
    eyes_changed = any(n in overrides for n in ("l_eye", "r_eye"))
    if eyes_changed:
        cues = [c for c in cues if c[0] not in ("face_ipd", "yunet_ipd", "eye_corners")]
        a, b = points.get("l_eye"), points.get("r_eye")
        if a and b and a["visible"] and b["visible"]:
            d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            if d > 1e-6:
                cues.append(("manual_eye_centres_2d", d, 0.20))
    if body:
        cues.append(("body", body["S"], 0.20))
    result = C.combine(cues)
    if overrides or excluded:
        if result["confidence"] == "high":
            result["confidence"] = "medium"
        if result["sigma"] is not None:
            result["sigma"] = max(result["sigma"], 0.15)
    if req.get("apply"):
        if result["S"] is None:
            raise ValueError("no usable scale cue")
        STATE["landmark_overrides"][index] = {"points": overrides, "excluded": excluded}
        STATE["preview_cache"] = {}
    return {"S": result["S"], "confidence": result["confidence"], "sigma": result["sigma"],
            "used": result["used"], "dropped": result["dropped"],
            "segments": body["rows"] if body else [], "excluded": excluded, "unavailable": unavailable,
            "body_spread": body["spread"] if body and math.isfinite(body["spread"]) else None}


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
    if "generation" in req and req["generation"] != STATE.get("landmark_generation"):
        raise ValueError("the loaded image changed; preview discarded")
    p = parse_params(req)
    if p["mode"] in ("frequency", "combine"):
        validate_skintone_source()
    view = req.get("view", "crop")
    reg, k, mreg = get_region(view, p["S"])
    key = preview_key(p, view)
    cache = STATE.setdefault("preview_cache", {})
    saved = cache.get(p["mode"])
    if saved and saved["key"] == key:
        STATE.update(saved["state"])
        STATE["imgs"] = dict(saved["state"]["imgs"])
        STATE["version"] += 1
        STATE["avers"] += 1
        if saved["analysis"] != analysis_flags(p["analysis"]):
            render_analysis(p["analysis"])
        return {"version": STATE["version"], "avers": STATE["avers"], "stats": saved["stats"], "cached": True}
    if p["mode"] != "dnb":
        result = run_skin_preview(p, reg, k, mreg)
        remember_preview(p, key, result)
        return result
    flags = p["mask_flags"]
    def preview_mask():
        mask = combine_mask(mreg, flags, p["S"] * k / D.IPD_MM) if (flags["on"] and mreg) else None
        return add_color_range(reg, mask, p["color_filter"], p["S"] * k / D.IPD_MM)
    mask = progress.call("Calculating preview skin mask", preview_mask)
    t = time.time()
    layers, after, diag, total = D.band_stack(reg, p["S"] * k, p["bands"], p["strength"], p["second_band"],
                                             first_on=p["band1_on"], sigma=p["layer_blur_at_s181"] * p["S"] * k / 181,
                                             edge=p["edge"], tau=p["tau"], max_ev=p["max_ev"],
                                             shadows=p["shadows"], highlights=p["highlights"], mask=mask,
                                             tone_strength=p["tone_strength"], protect=p["protect"],
                                             protect_mm=p["protect_mm"], protect_thr=p["protect_thr"])
    cs = next((a for index, a in layers if index == 1), np.full(reg.shape[:2], .5, np.float32))
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
    for index, pixels in layers:
        if index == 2:
            STATE["imgs"]["layer2"] = encode(u8(pixels), ".png")
            STATE["imgs"]["layer2x"] = encode(u8(.5+(pixels-.5)*8), ".png")
            masked = pixels if total is None else .5 + (pixels - .5) * total
            STATE["imgs"]["layer2m"] = encode(u8(masked), ".png")
            STATE["imgs"]["layer2mx"] = encode(u8(.5+(masked-.5)*8), ".png")
    render_analysis(p["analysis"])
    result = {"version": STATE["version"], "avers": STATE["avers"], "stats": stats}
    remember_preview(p, key, result)
    return result


def analysis_flags(flags):
    return {k: bool(flags.get(k, True)) for k in ANALYSIS_KEYS}


def preview_key(p, view):
    shared = {k: p[k] for k in ("S", "mask_flags", "shadows", "highlights", "tone_strength",
                               "protect", "protect_mm", "protect_thr", "color_filter")}
    if not shared["mask_flags"]["on"] or STATE["masks"] is None:
        shared["mask_flags"] = {"on": False}
    if not shared["color_filter"]["on"]:
        shared["color_filter"] = {"on": False}
    if shared["shadows"] is None and shared["highlights"] is None:
        shared["tone_strength"] = None
    if p["mode"] in ("dnb", "combine"):
        shared.update({k: p[k] for k in ("bands", "strength", "edge", "tau", "max_ev", "layer_blur_at_s181", "band1_on")})
        shared["second_band"] = p["second_band"] if p["second_band"]["on"] else {"on": False}
    if p["mode"] in ("frequency", "combine"):
        shared.update(frequency={k: p["frequency"][k] for k in
                                 (("on", "method", "radius", "blur_radius", "blur_strength", "blur_mask") if p["frequency"]["on"]
                                  else ("on", "method", "radius"))}, skintone=p["skintone"])
        directional = p.get("directional", DB.DEFAULTS)
        shared["directional"] = directional if directional["on"] else {"on": False}
        if directional["on"]:
            pose = STATE.get("pose")
            if pose is None or pose.get("kp") is None:
                shared["pose"] = None
            else:
                coordinates=np.asarray(pose["kp"])
                shared["pose"]=np.where(np.isfinite(coordinates),coordinates,None).tolist()
    if p["mode"] == "combine":
        shared["combine"]=p["combine"]
    return (STATE.get("landmark_generation"), id(STATE["masks"]), view, tuple(STATE["centre"]), tuple(STATE["region_box"]),
            json.dumps(shared, sort_keys=True, allow_nan=False))


def remember_preview(p, key, result):
    # Copy only region-sized state: contiguous crops can otherwise retain a full source array.
    def snapshot(value):
        if isinstance(value, np.ndarray):
            return value.copy()
        if isinstance(value, dict):
            return {k: snapshot(v) for k, v in value.items()}
        if isinstance(value, tuple):
            return tuple(snapshot(v) for v in value)
        return value

    def size(value):
        if isinstance(value, np.ndarray):
            return value.nbytes
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, dict):
            return sum(size(v) for v in value.values())
        if isinstance(value, (tuple, list)):
            return sum(size(v) for v in value)
        return 0

    cache = STATE.setdefault("preview_cache", {})
    cache.pop(p["mode"], None)
    state = {k: STATE[k] for k in ("imgs", "last", "struct", "region_off", "region_box")}
    required = size(state)
    while cache and sum(entry["bytes"] for entry in cache.values()) + required > PREVIEW_CACHE_LIMIT:
        del cache[next(iter(cache))]
    if required <= PREVIEW_CACHE_LIMIT:
        cache[p["mode"]] = {"key": key, "state": snapshot(state), "stats": result["stats"],
                            "analysis": analysis_flags(p["analysis"]), "bytes": required}


def source_region(shape, normalized=False):
    if normalized:
        validate_skintone_source()
    x, y, w, h = STATE["region_box"]
    src = STATE["source_img"][y:y + h, x:x + w]
    if src.shape == shape:
        return src
    resized = cv2.resize(src, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
    if normalized:
        # Area averages of validated SDR samples cannot exceed 0-1 mathematically.
        # OpenCV float32 accumulation can overshoot white by one or more ULPs.
        np.clip(resized, 0, 1, out=resized)
    return resized


def frequency_layers(img, mask, p, scale=1):
    if not STATE["source_opaque"]:
        raise ValueError("frequency separation currently needs an opaque merged image; flatten transparency before Get")
    f = p["frequency"]
    if not f["on"]:
        return FS.split(img, f["method"], f["radius"] * scale)
    return FS.separate(img, mask, f["method"], f["radius"] * scale, f["blur_radius"] * scale)


def validate_skintone_source(state=None):
    state = STATE if state is None else state
    if state.get("skintone_error"):
        raise ValueError(state["skintone_error"])
    if not state["source_opaque"]:
        raise ValueError("Skintone currently needs an opaque merged image; flatten transparency before Get")


def skintone_layers(img, mask, p, scale=1, *, native=False, state=None):
    state = STATE if state is None else state
    validate_skintone_source(state)
    directional = p.get("directional", DB.DEFAULTS)
    if directional["on"]:
        directional, _ = DB.effective_settings(directional, state.get("pose"), state["img"].shape)
    if directional["on"]:
        if state["masks"] is None:
            raise ValueError("Directional Blur unavailable: no shared skin mask. Get Image with usable skin detection.")
        DB.segments(directional, state.get("pose"), state["img"].shape)
        for pair, keys in DB.PAIRS.items():
            if any(directional.get(key, DB.DEFAULTS[key]) for key in keys):
                DB.scaled(DB.pair_settings(directional, pair), p["S"], scale)
        if directional.get("torso", False):
            DB.scaled({**directional, **directional["torso_controls"], "width": 1}, p["S"], scale)
    f = p["frequency"]
    if not f["on"]:
        layers = FS.split(img, f["method"], f["radius"] * scale)
        for key, value in layers.items():
            pixels = progress.call("Preparing RGB16 " + key.title(), FS.rgb16, value)
            layers[key] = pixels if native else pixels.astype(np.float32)/65535
    else:
        blur_mask = mask if f["blur_mask"] else np.ones(mask.shape, np.float32)
        layers = ST.separate(img, blur_mask, f["method"], f["radius"] * scale, f["blur_radius"] * scale,
                             native=native)
    if directional["on"]:
        if native:
            base = np.empty_like(img, dtype=np.float32)
            for y in range(0, img.shape[0], 128):
                part = {key: layers[key][y:y + 128].astype(np.float32) / 65535
                        for key in ("color", "blur") if key in layers}
                base[y:y + 128] = ST.base(part, mask[y:y + 128], f)
        else:
            base = ST.base(layers, mask, f)
        origin = state["region_box"][:2] if img.shape[:2] != state["img"].shape[:2] else (0, 0)
        raster, region_mask = progress.call("Calculating Directional Blur", DB.apply,
                                            base, mask, directional, state.get("pose"), p["S"],
                                            source_shape=state["img"].shape, origin=origin, scale=scale)
        del base
        pixels = FS.rgb16(raster)
        layers["directional"] = pixels if native else pixels.astype(np.float32) / 65535
        layers["directional_mask"] = u8(region_mask).astype(np.float32) / 255
        layers["directional_strength"] = DB.output_strength(directional)
    return layers


def combined_result(source, mask, p, scale=1, *, previews=None, state=None):
    """FS mix against original, followed by original-derived masked Soft Light bands."""
    fs_mix=p["combine"]["fs_mix"]/100
    dnb_mix=p["combine"]["dnb_mix"]/100
    fs_layers={}
    base=source.copy()
    if fs_mix>0 or previews is not None:
        fs_layers=skintone_layers(source,mask,p,scale,state=state)
        _,fs_result=ST.composite(fs_layers,mask,p["skintone"],p["frequency"])
        if previews is not None:
            previews["fs_result"]=fs_result
        base=np.clip(source+(fs_result-source)*fs_mix,0,1).astype(np.float32)
    bands=[]
    if (dnb_mix>0 or previews is not None) and (p["band1_on"] or p["second_band"]["on"]):
        bands,dnb_result,_,_=D.band_stack(source,p["S"]*scale,p["bands"],p["strength"],p["second_band"],
            first_on=p["band1_on"],sigma=p["layer_blur_at_s181"]*p["S"]*scale/181,
            edge=p["edge"],tau=p["tau"],max_ev=p["max_ev"],mask_result=(mask,None))
    else:
        dnb_result=source
    if previews is not None:
        previews["dnb_result"]=dnb_result
    for _,gray in bands:
        blended=D.softlight(base,gray[...,None])
        base=np.clip(base+(blended-base)*mask[...,None]*dnb_mix,0,1).astype(np.float32)
    return fs_layers,bands,base


def run_skin_preview(p, reg, k, mreg):
    t = time.time()
    mask, total, structure = progress.call("Calculating preview skin mask", tool_mask, reg, mreg, p, p["S"] * k)
    show = np.ones(reg.shape[:2], np.float32) if total is None else total
    STATE["last"] = {"reg": reg, "k": k, "skin": mask, "mreg": mreg, "S": p["S"] * k,
                     "off": STATE["region_off"], "mm": p["protect_mm"], "thr": p["protect_thr"]}
    STATE["struct"] = ((p["S"] * k, p["protect_mm"], p["protect_thr"]), structure) if structure is not None else None
    imgs = {"orig": encode(u8(reg), ".jpg", 92, STATE["icc"]), "mask": encode(u8(show), ".png")}
    distribution = None
    if p["mode"] == "mask":
        imgs["result"] = encode(u8(show), ".jpg", 92)
    else:
        src = source_region(reg.shape, normalized=True)
        if p["mode"]=="combine":
            previews={}
            layers,_,result=combined_result(src,show,p,k,previews=previews)
            imgs.update({key:encode(u8(value),".png",icc=STATE["source_icc"])
                         for key,value in previews.items()})
            corrected=None
        else:
            layers = skintone_layers(src, show, p, k)
            corrected, result = ST.composite(layers, show, p["skintone"], p["frequency"])
        imgs["orig"] = encode(u8(src), ".jpg", 92, STATE["source_icc"])
        imgs["result"] = encode(u8(result), ".png", icc=STATE["source_icc"])
        imgs.update({key: encode(u8(layers[key]), ".png", icc=STATE["source_icc"])
                     for key in ("color", "blur", "detail") if key in layers})
        if "directional" in layers:
            coverage = layers["directional_mask"][..., None]
            overlay = src * (1 - .45 * coverage) + np.array([.78, .46, .2], np.float32) * .45 * coverage
            imgs["directional_region"] = encode(u8(overlay), ".png", icc=STATE["source_icc"])
            imgs["directional_mask"] = encode(u8(layers["directional_mask"]), ".png")
        if p["frequency"]["on"] and layers:
            blur_layers = {key: layers[key] for key in ("color", "blur")}
            imgs["blur"] = encode(u8(ST.base(blur_layers, show, p["frequency"])), ".png", icc=STATE["source_icc"])
        if p["mode"]!="combine":
            distribution = ST.distribution(layers["color"], corrected, show, p["skintone"])
        if distribution is not None and "directional" in layers and not p["skintone"]["on"]:
            distribution["effect"]["note"] = (f"Skintone correction is off; Directional Blur uses "
                                               f"{layers['directional_strength']}% layer opacity. Color/Detail remain active.")
        elif distribution is not None and not p["frequency"]["on"] and not p["skintone"]["on"]:
            distribution["effect"]["note"] = "Skintone correction and added Blur are off; Color/Detail reconstruct the source."
    STATE["imgs"] = imgs
    STATE["version"] += 1
    render_analysis(p["analysis"])
    return {"version": STATE["version"], "avers": STATE["avers"], "stats": {
        "preview_px": [reg.shape[1], reg.shape[0]], "seconds": round(time.time() - t, 2),
        "mask_used": total is not None, "mask_pct": round(100 * float((show > 0.5).mean()), 1),
        "skintone": distribution}}


def dnb_full_result(p, layered=False):
    img = STATE["img"]
    mask = progress.call("Preparing D&B base skin/color mask", base_skin_mask,
                         img, STATE["masks"], p, p["S"])
    mask_result = cached_final_skin_mask(p, base_mask=mask)
    layers, after, diag, total = progress.call("Calculating D&B layers and final mask", D.band_stack, img, p["S"], p["bands"], p["strength"], p["second_band"],
                                               first_on=p["band1_on"], sigma=p["layer_blur_at_s181"] * p["S"] / 181,
                                               edge=p["edge"], tau=p["tau"], max_ev=p["max_ev"],
                                               shadows=p["shadows"], highlights=p["highlights"], mask=mask, tone_strength=p["tone_strength"],
                                               protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"],
                                               mask_result=mask_result)
    first = next((a for index, a in layers if index == 1), np.full(img.shape[:2], .5, np.float32))
    return (layers if layered else first), after, total


def api_save(req):
    """Writes the three files for a layer-based workflow (Photoshop / PhotoCraft):
    1_result: the picture with the settings applied, 2_skinmask: the combined mask to apply to layer 3, and
    3_softlight_layer: 16-bit gray layer for Soft Light mode with no mask of any kind applied."""
    if STATE["img"] is None:
        raise ValueError("load an image first")
    p = parse_params(req)
    if p["mode"]=="combine":
        return save_combined(req,p)
    if p["mode"] != "dnb":
        return save_skin(req, p)
    t = time.time()
    img = STATE["img"]
    layers, after, total = dnb_full_result(p, layered=True)
    cs = next((a for index, a in layers if index == 1), np.full(img.shape[:2], .5, np.float32))
    stem = Path(STATE["path"]).stem
    out = ROOT / "results" / f"{stem}_{datetime.datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    transfer = read_global_settings()["layer_transfer"]
    (out / f"{stem}_1_result.{transfer['format']}").write_bytes(
        PCB.tagged_rgb16(FS.rgb16(after), STATE["icc"] or SRGB_ICC, transfer))
    mask_out = total if total is not None else np.ones(img.shape[:2], np.float32)
    cv2.imwrite(str(out / f"{stem}_2_skinmask.png"), u8(mask_out))
    # Three identical channels, not a Gray image: PhotoCraft (and Photoshop) convert a Gray image to RGB with a gray
    # profile, which shifts 50% gray and made the layer brighten the picture by about 8 levels in a test.
    g16 = np.round(cs * 65535).astype(np.uint16)
    if p["band1_on"]:
        cv2.imwrite(str(out / f"{stem}_3_softlight_layer.png"), np.dstack([g16, g16, g16]))
    for index, pixels in layers:
        if index == 2:
            gray = np.round(pixels * 65535).astype(np.uint16)
            if not cv2.imwrite(str(out / f"{stem}_4_softlight_band2.png"), np.repeat(gray[..., None], 3, axis=2)):
                raise OSError("could not write the second D&B band layer")
    d = (after - img).mean(axis=2)
    settings = {k: v for k, v in module_settings(req).items() if k != "view"}
    settings["output_settings"] = transfer
    settings.update(image=STATE["path"], stats={"p95": float(np.percentile(np.abs(d), 95) * 255), "max": float(np.abs(d).max() * 255)},
                    layer_order=[f"band {index}" for index, _ in reversed(layers)],
                    band1_on=p["band1_on"], second_band=p["second_band"],
                    files={"1_result": "picture with the settings applied (combined mask applied)",
                           "2_skinmask": "ONE mask = skin x color range x tone range x structure protection (8-bit gray; all white if none are used); use as the layer mask of file 3",
                           "3_softlight_layer": "16-bit RGB PNG (three identical gray channels, so no gray-profile conversion on import), Soft Light blend mode, 50% gray = no change, NO mask applied"},
                    soft_light="W3C soft-light formula; Photoshop's soft light differs slightly",
                    colour="embedded source ICC profile, or sRGB when source had none; flattened result is RGB16")
    (out / "settings.json").write_text(json.dumps(settings, indent=2))
    return {"dir": str(out), "files": sorted(f.name for f in out.iterdir()), "seconds": round(time.time() - t, 1)}


def save_combined(req,p):
    validate_skintone_source()
    mask=full_skin_mask(p)
    _,_,result=combined_result(STATE["source_img"],mask,p)
    stem=Path(STATE["path"]).stem
    out=ROOT/"results"/f"{stem}_combine_{datetime.datetime.now():%Y%m%d-%H%M%S-%f}"
    out.mkdir(parents=True,exist_ok=False)
    transfer = read_global_settings()["layer_transfer"]
    filename = "result." + transfer["format"]
    (out/filename).write_bytes(PCB.tagged_rgb16(FS.rgb16(result),STATE["source_icc"] or SRGB_ICC,transfer))
    (out/"settings.json").write_text(json.dumps({**module_settings(req),"image":STATE["path"],
                                               "output_settings":transfer},indent=2))
    return {"dir":str(out),"files":[filename,"settings.json"]}


def save_skin(req, p):
    if p["mode"] == "frequency":
        validate_skintone_source()
    t = time.time()
    mask = full_skin_mask(p)
    layers = {}
    result = None
    if p["mode"] == "frequency":
        layers = skintone_layers(STATE["source_img"], mask, p)
        _, result = ST.composite(layers, mask, p["skintone"], p["frequency"])
    stem = Path(STATE["path"]).stem
    out = ROOT / "results" / f"{stem}_{p['mode']}_{datetime.datetime.now():%Y%m%d-%H%M%S-%f}"
    out.mkdir(parents=True, exist_ok=True)
    mask_name = "skinmask.png"
    if not cv2.imwrite(str(out / mask_name), u8(mask)):
        raise ValueError("could not write skin mask")
    for key in ("color", "blur", "detail", "directional"):
        if key not in layers:
            continue
        pixels = layers[key]
        (out / f"{key}.png").write_bytes(PCB.tagged_rgb16(FS.rgb16(pixels), STATE["source_icc"] or SRGB_ICC))
    if "directional_mask" in layers:
        if not cv2.imwrite(str(out / "directional_mask.png"), u8(layers["directional_mask"])):
            raise ValueError("could not write Directional Blur mask")
    if result is not None:
        transfer = read_global_settings()["layer_transfer"]
        (out / ("result." + transfer["format"])).write_bytes(
            PCB.tagged_rgb16(FS.rgb16(result), STATE["source_icc"] or SRGB_ICC, transfer))
        if p["skintone"]["on"]:
            for name, table in zip(("Hue",), ST.tables(dict(p["skintone"], hue_amount=100))):
                (out / f"{name}.cube").write_text(ST.cube_text(table, name))
    recipe = ("SHP FS + Skintone: Normal unmasked outer group. Top-down: Detail (Linear Light, unmasked), "
                         + (f"Hue (opacity {p['skintone']['hue_amount']}%; with skinmask.png; Hue.cube, native Color Lookup, Normal, "
                            "trilinear, dither off), " if p["skintone"]["on"] else "")
                         + (f"Directional Blur (Normal, opacity {layers['directional_strength']}%, with directional_mask.png), "
                            if "directional" in layers else "")
                         + (f"Blur (Normal, opacity {p['frequency']['blur_strength']}%, "
                            + ("with skinmask.png" if p["frequency"]["blur_mask"] else "unmasked") + "), " if p["frequency"]["on"] else "")
                         + "Color (Normal, unmasked)."
                         if result is not None else mask_name + ": reusable grayscale layer in Normal mode; no healing.")
    (out / "settings.json").write_text(json.dumps({**module_settings(req), "image": STATE["path"],
        **({"output_settings": transfer} if result is not None else {}),
        "layer_recipe": recipe,
        "pixels": ("HSV of native document RGB, 33-cube sampled hue soft limits on Color plus masked Blur. "
                   "No saturation correction. Neutrals protected. Gaussian/median Color/Detail separation and "
                   "skin-aware Gaussian Blur use full-image radii. RGB16, source numeric space. Hue LUT is "
                   "embedded by Send only while Skintone correction is on; off bypasses hue calculations."
                   if result is not None else
                   "Advisory signed luminance-anomaly candidates, grayscale8; no correction or healing."
                   "Separation radius: Gaussian sigma or median square radius, full-image pixels; Blur: Gaussian sigma. "
                   "Median uses equal votes among positive-mask samples. RGB16, source numeric space.")}, indent=2))
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
        return {"connected": False, "message": str(e), "code": e.code, "fetched": BRIDGE["doc"]}


def api_pc_fetch(req):
    started = time.perf_counter()
    path, doc = progress.call("Getting merged image from PhotoCraft", bridge().fetch, ROOT / "inbox")
    fetched = time.perf_counter()
    res = api_load({"path": str(path)})
    res["timings"]["loaded"] += fetched - started
    res["seconds"] = time.perf_counter() - started
    BRIDGE["doc"] = doc
    STATE["source_kind"] = res["source_kind"] = "photocraft"
    res["pc_doc"] = doc
    return res


def watch_target():
    return capture_target(bridge())


def load_watch_image(path, target):
    if watch_target() != target:
        raise ValueError("PhotoCraft document changed; export again from the intended document")
    with Image.open(path) as image:
        if (target["identity"]["width"], target["identity"]["height"]) != image.size:
            raise ValueError("export size does not match the active PhotoCraft document")
    started = time.perf_counter()
    loaded = progress.call("Loading image / preparing profile", prepare_loaded_image,
                           {"path": str(path)}, set_colour=False)
    state, info = finish_loaded_image(*loaded, *loaded[1].shape[:2], started, publish=False)
    h, w = state["img"].shape[:2]
    if (target["identity"]["width"], target["identity"]["height"]) != (w, h):
        raise ValueError("export size does not match the confirmed PhotoCraft document")
    if watch_target() != target:
        raise ValueError("PhotoCraft document changed during image analysis; export again")
    D.set_colour_mode(loaded[4])
    MASK_PRECACHE.reset()
    state["source_kind"] = info["source_kind"] = "photocraft"
    STATE.update(state)
    BRIDGE["doc"] = target["document"]
    return {**info, "generation": STATE["landmark_generation"], "pc_doc": BRIDGE["doc"],
            "directional_pose": directional_availability()}


def api_pc_send_layers(req):
    if STATE["img"] is None or BRIDGE["doc"] is None:
        raise ValueError("get a picture from PhotoCraft first")
    if "generation" in req and req["generation"]!=STATE.get("landmark_generation"):
        raise ValueError("the loaded image changed; Get Image again before Send Layers")
    p = parse_params(req)
    img = STATE["img"]
    if p["mode"]=="combine":
        return {"sent":True,**send_combined_layers(p)}
    if p["mode"] != "dnb":
        if p["mode"] == "frequency":
            return {"sent": True, **send_skintone_parallel(p)}
        mask = progress.call("Preparing shared mask", full_skin_mask, p)
        if p["mode"] == "mask":
            mask8 = u8(mask)
            del mask
            r = bridge().send_mask(mask8, BRIDGE["doc"], source_icc=STATE["source_icc"])
        return {"sent": True, **r}
    layers, after, total = dnb_full_result(p, layered=True)
    if not layers:
        raise ValueError("enable at least one D&B band before Send")
    del after
    mask_out = total if total is not None else np.ones(img.shape[:2], np.float32)
    payload = [(index, np.repeat(np.round(pixels * 65535).astype(np.uint16)[..., None], 3, axis=2))
               for index, pixels in layers]
    mask8 = u8(mask_out)
    del mask_out, total
    r = bridge().send_skin_bands(payload,mask8,BRIDGE["doc"],source_icc=STATE["source_icc"],
                                mask_placement=read_global_settings()["mask_placement"])
    return {"sent": True, **r}


def skin_result(p):
    if p["mode"]=="dnb":
        _,result,_=dnb_full_result(p)
        return result,STATE["icc"] or SRGB_ICC
    validate_skintone_source()
    mask=full_skin_mask(p)
    if p["mode"]=="combine":
        _,_,result=combined_result(STATE["source_img"],mask,p)
    else:
        layers=skintone_layers(STATE["source_img"],mask,p)
        _,result=ST.composite(layers,mask,p["skintone"],p["frequency"])
    return result,STATE["source_icc"] or SRGB_ICC


def api_pc_send(req):
    if STATE["img"] is None or BRIDGE["doc"] is None:
        raise ValueError("get a picture from PhotoCraft first")
    p=parse_params(req)
    if p["mode"] == "mask":
        return api_pc_send_layers(req)
    result,icc=skin_result(p)
    return {"sent":True,**bridge().send_result(FS.rgb16(result),BRIDGE["doc"],source_icc=icc)}


def send_combined_layers(p):
    validate_skintone_source()
    source=STATE["source_img"]
    mask=full_skin_mask(p)
    fs_layers,bands,_=combined_result(source,mask,p)
    payload=[(index,np.repeat(np.round(gray*65535).astype(np.uint16)[...,None],3,axis=2))
             for index,gray in bands]
    if not fs_layers:
        if not payload:
            raise ValueError("enable an FS or D&B contribution before Send Layers")
        return bridge().send_skin_bands(payload,u8(mask),BRIDGE["doc"],source_icc=STATE["source_icc"],
                                        band_mix=p["combine"]["dnb_mix"], mask_placement=read_global_settings()["mask_placement"])
    mix=p["combine"]["fs_mix"]/100
    for key in ("color","blur","directional"):
        if key in fs_layers:
            fs_layers[key]=source+(fs_layers[key]-source)*mix
    fs_layers["detail"]=.5+(fs_layers["detail"]-.5)*mix
    directional=None
    if "directional" in fs_layers:
        directional=(FS.rgb16(fs_layers.pop("directional")),u8(fs_layers.pop("directional_mask")),
                     fs_layers.pop("directional_strength"))
    rgb={key:FS.rgb16(value) for key,value in fs_layers.items()}
    settings={**p["skintone"],"hue_amount":p["skintone"]["hue_amount"]*mix}
    return bridge().send_skintone(rgb,settings,u8(mask),BRIDGE["doc"],STATE["source_icc"],
        transfer=p["frequency_transfer"],include_blur=p["frequency"]["on"], mask_placement=read_global_settings()["mask_placement"],
        blur_strength=p["frequency"]["blur_strength"],blur_masked=p["frequency"]["blur_mask"],
        directional=directional,bands=payload,band_mask8=u8(mask) if payload else None,band_mix=p["combine"]["dnb_mix"])


def send_skintone_parallel(p):
    """One compute worker; only this request thread touches PhotoCraft."""
    validate_skintone_source()
    source = STATE["source_img"]
    error = ST.source_error(source)
    if error:
        raise ValueError(error)
    f = p["frequency"]
    effective, _ = DB.effective_settings(p.get("directional", DB.DEFAULTS), STATE.get("pose"), source.shape)
    if effective["on"]:
        # Bounded serial region work avoids nesting another executor into the existing Send workers.
        mask = full_skin_mask(p)
        layers = skintone_layers(source, mask, p, native=True)
        directional = layers.pop("directional")
        directional_mask = u8(layers.pop("directional_mask"))
        strength = layers.pop("directional_strength")
        return bridge().send_skintone(layers, p["skintone"], u8(mask), BRIDGE["doc"], STATE["source_icc"],
                                      transfer=p["frequency_transfer"], include_blur=f["on"], mask_placement=read_global_settings()["mask_placement"],
                                      blur_strength=f["blur_strength"], blur_masked=f["blur_mask"],
                                      directional=(directional, directional_mask, strength))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="skin-fs") as pool:
        mask_job = pool.submit(copy_context().run, progress.call, "Preparing shared mask", full_skin_mask, p)
        try:
            layers = progress.call("Separating Color and Detail", FS.split, source, f["method"], f["radius"])
            mask = mask_job.result()
            if not f["on"]:
                pixels = {key: progress.call("Preparing RGB16 " + key.title(), FS.rgb16, value)
                          for key, value in layers.items()}
                return bridge().send_skintone(pixels, p["skintone"], u8(mask), BRIDGE["doc"],
                                              STATE["source_icc"], transfer=p["frequency_transfer"], include_blur=False,
                                              mask_placement=read_global_settings()["mask_placement"])

            def prepare_blur(color, skin):
                blur = progress.call("Computing skin-aware Blur", FS.skin_filter,
                                     color, skin if f["blur_mask"] else np.ones(skin.shape, np.float32),
                                     f["blur_radius"], "gaussian")
                return progress.call("Preparing RGB16 Blur", FS.rgb16, blur)

            blur_job = pool.submit(copy_context().run, prepare_blur, layers["color"], mask)
            try:
                # Blur uses unquantized Color and the original soft mask, as before.
                for key in ("color", "detail"):
                    layers[key] = progress.call("Preparing RGB16 " + key.title(), FS.rgb16, layers[key])
                mask8 = u8(mask)
                del mask
                return bridge().send_skintone(layers, p["skintone"], mask8, BRIDGE["doc"],
                                              STATE["source_icc"], blur_future=blur_job,
                                              transfer=p["frequency_transfer"], blur_strength=f["blur_strength"],
                                              blur_masked=f["blur_mask"], mask_placement=read_global_settings()["mask_placement"])
            finally:
                blur_job.cancel()
        finally:
            mask_job.cancel()


def api_pc_launch(req):
    return bridge().launch()


def api_send_result(req):
    if STATE["img"] is None:
        raise ValueError("load an image first")
    if req.get("generation", STATE.get("landmark_generation")) != STATE.get("landmark_generation"):
        raise ValueError("the loaded image changed; Get Image again before sending")
    if STATE.get("source_kind") == "photocraft":
        if not api_pc_status({})["connected"]:
            raise ValueError("PhotoCraft is disconnected; reconnect to send this PhotoCraft source")
        return api_pc_send(req)
    if STATE.get("source_kind") != "local" or not STATE.get("path"):
        raise ValueError("Get Image again to establish the output destination")
    p = parse_params(req)
    transfer = read_global_settings()["layer_transfer"] if p["mode"] != "mask" else None
    if p["mode"] == "dnb":
        cs, result, total = dnb_full_result(p)
        del cs, total
        data = progress.call("Encoding full-resolution result", PCB.tagged_rgb16,
                             FS.rgb16(result), STATE["icc"] or SRGB_ICC, transfer)
    else:
        mask = progress.call("Preparing shared mask", full_skin_mask, p)
        if p["mode"] == "mask":
            ok, encoded = cv2.imencode(".png", u8(mask))
            if not ok:
                raise ValueError("could not encode the skin mask")
            data = encoded.tobytes()
        else:
            if p["mode"]=="combine":
                _,_,result=combined_result(STATE["source_img"],mask,p)
            else:
                layers = progress.call("Calculating FS + Skintone result", skintone_layers, STATE["source_img"], mask, p)
                _, result = ST.composite(layers, mask, p["skintone"], p["frequency"])
            data = progress.call("Encoding full-resolution result", PCB.tagged_rgb16,
                                 FS.rgb16(result), STATE["source_icc"] or SRGB_ICC, transfer)
    suffix = {"dnb": "shpdnb", "mask": "shpskinmask", "frequency": "shpfs", "combine":"shpcombine"}[p["mode"]]
    path = progress.call("Saving result beside source", write_local_result, STATE["path"], suffix, data,
                         STATE.get("source_directory"),
                         extension="." + transfer["format"] if transfer else ".png")
    return {"saved": True, "path": path}


def read_presets():
    try:
        d = json.loads(PRESETS.read_text())
    except Exception:
        d = {}
    return {"default": d.get("default"), "presets": d.get("presets", {})}


def read_global_settings():
    with GLOBAL_SETTINGS_LOCK:
        try:
            settings = json.loads(GLOBAL_SETTINGS.read_text())
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError("global settings must be an object")
        migrate = "layer_transfer" not in settings
        if migrate:
            # Only the startup default migrates; choosing a named preset never changes globals.
            try:
                presets = json.loads(PRESETS.read_text())
            except FileNotFoundError:
                presets = {}
            if not isinstance(presets, dict):
                raise ValueError("legacy presets must be an object")
            legacy = presets.get("default") or {}
            if not isinstance(legacy, dict):
                raise ValueError("legacy default settings must be an object")
            settings["layer_transfer"] = PCB.transfer_settings(
                legacy.get("frequency_transfer", {"opaque_alpha": False}))
        else:
            migrate = "exclude_opaque_alpha" not in settings["layer_transfer"] if isinstance(settings["layer_transfer"], dict) else False
        settings["layer_transfer"] = global_transfer_settings(settings["layer_transfer"])
        if "mask_placement" not in settings:
            settings["mask_placement"] = "folder"
            migrate = True
        if settings["mask_placement"] not in ("folder", "layers"):
            raise ValueError("mask placement must be folder or layers")
        if migrate:
            write_global_settings(settings)
        return settings


def global_transfer_settings(value):
    if isinstance(value, dict) and "exclude_opaque_alpha" in value:
        value = dict(value)
        exclude = value.pop("exclude_opaque_alpha")
        if type(exclude) is not bool:
            raise ValueError("exclude opaque alpha must be a boolean")
        if "opaque_alpha" in value and value["opaque_alpha"] != (not exclude):
            raise ValueError("conflicting opaque alpha settings")
        value["opaque_alpha"] = not exclude
    return PCB.transfer_settings(value)


def write_global_settings(settings):
    stored = {**settings, "layer_transfer": dict(settings["layer_transfer"])}
    stored["layer_transfer"]["exclude_opaque_alpha"] = not stored["layer_transfer"].pop("opaque_alpha")
    GLOBAL_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=GLOBAL_SETTINGS.parent,
                                     prefix=".settings-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(stored, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temporary.replace(GLOBAL_SETTINGS)
        finally:
            temporary.unlink(missing_ok=True)


def api_global_settings(req):
    transfer = global_transfer_settings(req["layer_transfer"])
    settings = read_global_settings()
    placement = req.get("mask_placement", settings["mask_placement"])
    if placement not in ("folder", "layers"):
        raise ValueError("mask placement must be folder or layers")
    with GLOBAL_SETTINGS_LOCK:
        settings["layer_transfer"] = transfer
        settings["mask_placement"] = placement
        write_global_settings(settings)
    return settings


def module_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("preset settings must be an object")
    return {key: value for key, value in settings.items() if key != "frequency_transfer"}


def write_presets(d):
    PRESETS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(PRESETS)


def api_preset_save(req):
    name = str(req.get("name", "")).strip()
    if not name or len(name) > 60:
        raise ValueError("give the preset a name (1-60 characters)")
    read_global_settings()
    d = read_presets()
    existed = name in d["presets"]
    d["presets"][name] = module_settings(req["settings"])
    write_presets(d)
    return {"name": name, "updated": existed, **d}


def api_preset_default(req):
    read_global_settings()
    d = read_presets()
    d["default"] = module_settings(req["settings"])
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

    def load_watch_image(self, req):
        watcher = getattr(self.server, "watch_folder", None)
        if watcher is None:
            raise ValueError("watch folder is not available in this service")
        target = watch_target()
        return watcher.load(req.get("token"), lambda path: load_watch_image(path, target),
                            retry_key=target)

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/mask-precache":
            return self.send(200, json.dumps(mask_cache_status()).encode(), "application/json")
        if path == "/api/progress":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = progress.snapshot(query.get("token", [""])[0], query.get("consume") == ["1"])
            return self.send(200, json.dumps(result).encode(), "application/json")
        if path == "/api/watch-folder":
            watcher = getattr(self.server, "watch_folder", None)
            result = watcher.status() if watcher is not None else {"ready": False}
            return self.send(200, json.dumps(result).encode(), "application/json")
        if path == "/api/health":
            return self.send(200, json.dumps({**identity("dnb", ROOT, MODELS),
                "ui_lifecycle": 1 if getattr(self.server, "lifetime", None) else 0}).encode(), "application/json")
        if path == "/static/lifecycle.js":
            return self.send(200, script(), "text/javascript; charset=utf-8")
        if path == "/static/shp-ui.css":
            return self.send(200, stylesheet(), "text/css; charset=utf-8")
        if path == "/static/theme.js":
            return self.send(200, theme_script(), "text/javascript; charset=utf-8")
        if path in ("/", "/index.html"):
            return self.send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/presets":
            return self.send(200, json.dumps(read_presets()).encode(), "application/json")
        if path == "/api/global-settings":
            try:
                return self.send(200, json.dumps(read_global_settings()).encode(), "application/json")
            except (OSError, ValueError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
        if path == "/api/images":
            return self.send(200, json.dumps(list_images()).encode(), "application/json")
        if path == "/img/landmarks":
            if STATE["img"] is None:
                return self.send(404, b"no image yet", "text/plain")
            img = STATE["img"]
            h, w = img.shape[:2]
            k = min(1.0, 1400 / max(h, w))
            preview = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else img
            return self.send(200, encode(u8(preview), ".jpg", 92, STATE["icc"]), "image/jpeg")
        if path.startswith("/img/"):
            kind = path[5:]
            data = STATE["imgs"].get(kind)
            if data is None:
                return self.send(404, b"no image yet", "text/plain")
            return self.send(200, data, "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg")
        self.send(404, b"not found", "text/plain")

    def do_api_POST(self):
        token = None
        try:
            if self.path in ("/api/send-result","/api/pc/send_layers", "/api/batch", "/api/batch/images", "/api/batch/cancel", "/api/global-settings", "/api/watch-folder/load", "/api/watch-folder/prepare") and (
                    self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}"
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                return self.send(403, b'{"error":"Same-origin JSON requests required"}', "application/json")
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/batch/cancel":
                return self.send(200, json.dumps(api_batch_cancel(req)).encode(), "application/json")
            fn = {"/api/load": api_load, "/api/hue-center": api_hue_center, "/api/run": api_run, "/api/centre": api_centre, "/api/color-pick": api_color_pick, "/api/analysis": api_analysis, "/api/save": api_save, "/api/profile": api_profile, "/api/tone": api_tone,
                  "/api/landmarks": api_landmarks, "/api/landmarks/estimate": api_landmark_estimate,
                  "/api/pc/status": api_pc_status, "/api/pc/fetch": api_pc_fetch, "/api/pc/send": api_pc_send, "/api/pc/send_layers":api_pc_send_layers, "/api/pc/launch": api_pc_launch,
                  "/api/send-result": api_send_result,
                  "/api/batch": api_batch, "/api/batch/images": api_batch_images,
                  "/api/watch-folder/load": self.load_watch_image,
                  "/api/global-settings": api_global_settings,
                  "/api/presets/save": api_preset_save, "/api/presets/default": api_preset_default, "/api/presets/delete": api_preset_delete}.get(self.path)
            if fn is None:
                return self.send(404, b"not found", "text/plain")
            token = req.pop("_progress", None)
            with progress.operation(token):
                with LOCK:
                    if self.path in ("/api/pc/send", "/api/pc/send_layers"):
                        req["frequency_transfer"] = read_global_settings()["layer_transfer"]
                    mask_params = None
                    if self.path in ("/api/run", "/api/pc/send", "/api/pc/send_layers", "/api/send-result", "/api/save") and STATE["img"] is not None:
                        if self.path == "/api/run" and type(req.get("precache_mask", True)) is not bool:
                            raise ValueError("precache_mask must be a boolean")
                        if self.path == "/api/run":
                            STATE["mask_precache_enabled"] = req.get("precache_mask", True)
                        mask_params = parse_params(req)
                        STATE["mask_status_key"] = mask_request_key(mask_params)
                        MASK_PRECACHE.select(STATE["mask_status_key"])
                        if self.path == "/api/run" and req.get("precache_mask", True) is False:
                            MASK_PRECACHE.reset()
                    res = fn(req)
                    if self.path == "/api/run" and mask_params is not None:
                        if req.get("precache_mask", True) is not False:
                            queue_mask_precache(mask_params)
                    if mask_params is not None:
                        res["mask_precache"] = mask_cache_status()
            if token is not None:
                res["_progress"] = progress.snapshot(token, consume=True)
            self.send(200, json.dumps(res).encode(), "application/json")
        except Exception as e:  # report to the page instead of crashing the server
            result = {"error": f"{type(e).__name__}: {e}"}
            if self.path.startswith("/api/watch-folder/") and getattr(self.server, "watch_last_error", None) != result["error"]:
                print(f"Watch-folder {self.path.rsplit('/', 1)[-1]} failed: {result['error']}", flush=True)
                self.server.watch_last_error = result["error"]
            if isinstance(token, str):
                result["_progress"] = progress.snapshot(token, consume=True)
            self.send(400, json.dumps(result).encode(), "application/json")


def main():
    ap = argparse.ArgumentParser(description="SHP Portrait Tools — Skin Tools web UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--close-with-ui", action="store_true", help="stop after the last UI tab closes (launcher use)")
    ap.add_argument("--dir", nargs="+", default=[])
    a = ap.parse_args()
    DIRS.extend(a.dir)
    srv = BrowserServer(("127.0.0.1", a.port), Handler, close_with_ui=a.close_with_ui,
                        loaded=lambda: STATE["img"] is not None)
    srv.watch_folder = WatchFolder(WATCH_DIRECTORY, ROOT / "inbox")
    print(f"SHP Portrait Tools — Skin Tools: http://127.0.0.1:{srv.server_port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.watch_folder.close()
        srv.server_close()
        MASK_PRECACHE.close()


if __name__ == "__main__":
    main()
