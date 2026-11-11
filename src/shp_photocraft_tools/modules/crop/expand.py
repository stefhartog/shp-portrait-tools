"""Expand a picture to a preset aspect ratio and let a ComfyUI server fill the new, empty area.

Geometry (pure): `frame_margins` turns the frame the user drew (a rectangle in picture pixels that, per direction, either
contains the picture or lies inside it) into the margins on each side: positive = generated, negative = cropped.

Generation: `expand_variations()` uploads the picture to ComfyUI (http://127.0.0.1:8188 or SHPC_COMFY), queues the graph
from `build_graph()` (FLUX.2 [dev] GGUF, reference-latent outpaint, colour match, upscale, seam correction, feathered paste
of the original), or native FLUX.1 Fill with Lanczos final resizing, and reads the finished picture back.
No external workflow JSON or other backend is involved.
"""
import base64
import io
import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms, ImageOps

COMFY_URL = os.environ.get("SHPC_COMFY", "http://127.0.0.1:8188")
# Base ratios (long side : short side); the orientation flips them to portrait. 5:4 flipped is the 4:5 Instagram ratio.
RATIOS = [(1, 1), (4, 3), (3, 2), (16, 9), (5, 4)]
EXPAND_LIMIT = min(999, int(os.environ.get("SHPC_LIMIT", "999")))  # source pixels per side
MIN_SIDE = 64
MAX_VARIATIONS = 4

# Files the graph loads (override with the environment); the diffusion and upscale models are settings.
CLIP_NAME = os.environ.get("SHPC_CLIP", "mistral_3_small_flux2_fp8.safetensors")
VAE_NAME = os.environ.get("SHPC_VAE", "flux2-vae.safetensors")
LORA_NAME = os.environ.get("SHPC_LORA", "Flux_2-Turbo-LoRA_comfyui.safetensors")
FILL_CLIP_L = os.environ.get("SHPC_FILL_CLIP_L", "clip_l.safetensors")
FILL_T5 = os.environ.get("SHPC_FILL_T5", "t5xxl_fp8_e4m3fn_scaled.safetensors")
FILL_VAE = os.environ.get("SHPC_FILL_VAE", "ae.safetensors")
FILL_PROMPT = ("A realistic photograph, a natural continuation of the existing scene into the surrounding empty canvas, "
               "consistent lighting, colors, perspective, depth of field and detail. Seamless scene continuation, "
               "no frame, no border, no flat gray margins.")
BASE_PROMPT = ("Outpaint this image. The flat gray margin around the photo is empty canvas and must NOT remain: fill the "
               "entire gray margin with new realistic scene content that naturally continues the photograph in every "
               "direction, with matching lighting, colors, perspective, depth of field and detail. No gray, no border, "
               "no frame. Keep the central photo identical.")

# name -> (type, min, max, default). Strings have no range; they are checked against the server when a run starts.
SETTINGS = {
    "guidance": (float, 1, 10, 3.5), "steps": (int, 1, 50, 8), "megapixels": (float, 0.25, 2, 0.5),
    "feathering": (int, 0, 512, 128), "lora": (bool, 0, 1, True), "seam_correction": (bool, 0, 1, True),
    "unet": (str, 0, 0, "flux2-dev-q3_k_m.gguf"), "upscale": (str, 0, 0, "4xNomos8k_atd_jpg.safetensors"),
    "method": (str, 0, 0, "flux2"),
    "fill_guidance": (float, 1, 100, 30.0), "fill_steps": (int, 1, 50, 28),
    "fill_megapixels": (float, 0.25, 2, 0.5), "fill_feathering": (int, 0, 512, 64),
    "fill_unet": (str, 0, 0, "flux1-fill-dev.safetensors"),
    "fill_clip_device": (str, 0, 0, "cpu"),
    "fill_upscale": (str, 0, 0, "lanczos"),
    "prompt": (str, 0, 0, ""),
}
DEFAULTS = {k: v[3] for k, v in SETTINGS.items()}
PATCH_DEFAULTS = {"patch_context": 100}
SETTINGS["patch_context"] = (int, 0, 100, 100)
REQUIRED_NODES = ["UnetLoaderGGUF", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly", "ImagePadForOutpaint",
                  "ImageScaleToTotalPixels", "ReferenceLatent", "EmptyFlux2LatentImage", "Flux2Scheduler",
                  "SamplerCustomAdvanced", "INPAINT_ColorMatch", "UpscaleModelLoader", "ImageUpscaleWithModel",
                  "SeamColorCorrect", "ImageCompositeMasked"]
FILL_REQUIRED_NODES = ["LoadImage", "ImagePadForOutpaint", "ImageScaleToTotalPixels", "GetImageSize",
                       "MaskToImage", "ImageScale", "ImageToMask", "UNETLoader", "DualCLIPLoader", "VAELoader",
                       "CLIPTextEncode", "FluxGuidance", "ConditioningZeroOut", "InpaintModelConditioning",
                       "KSampler", "VAEDecode", "INPAINT_ColorMatch", "SeamColorCorrect", "InvertMask",
                       "ImageCompositeMasked", "PreviewImage"]


class ExpandError(RuntimeError):
    """Something the user can act on (bad frame, ComfyUI not available, generation failed)."""


class Cancelled(Exception):
    pass


RATIO_SNAP = 2


def frame_margins(width, height, ratio, x, y, w, h=None):
    """Margins (left, top, right, bottom) of the frame {x, y, w} of aspect `ratio` (rw, rh) around a width x height
    picture. x, y is the frame's top-left in picture pixels; h follows from w, or, with ratio None (free frame), is
    given. A negative margin crops that side. Returns (margins, (w, h))."""
    w, x, y = int(w), int(x), int(y)
    if ratio is None:
        if h is None:
            raise ExpandError("a free frame needs a height")
        h = int(h)
    else:
        rw, rh = int(ratio[0]), int(ratio[1])
        if rw < 1 or rh < 1:
            raise ExpandError("invalid ratio")
        h0 = (2 * w * rh + rw) // (2 * rw)  # half up, like the page
        # the page snaps a side within RATIO_SNAP px to the picture's side, so accept its height if that close
        h = int(h) if h is not None and abs(int(h) - h0) <= RATIO_SNAP else h0
    if min(w, h) < MIN_SIDE:
        raise ExpandError("the frame is too small")
    for v, size, extent in ((x, w, width), (y, h, height)):  # each side crops or adds on its own; enough picture must stay
        if min(v + size, extent) - max(v, 0) < MIN_SIDE:
            raise ExpandError("the frame must keep a part of the picture")
    left, top, right, bottom = -x, -y, x + w - width, y + h - height
    if max(left, top, right, bottom) > EXPAND_LIMIT:
        raise ExpandError(f"cannot add more than {EXPAND_LIMIT} px on one side; choose a smaller frame")
    if max(left, top, right, bottom) <= 0:
        raise ExpandError("nothing to generate: the frame does not extend beyond the picture")
    return (left, top, right, bottom), (w, h)


def to_srgb(im):
    """RGB copy of `im` in sRGB (a picture with another embedded profile, e.g. Adobe RGB, is converted)."""
    icc = im.info.get("icc_profile")
    rgb = ImageOps.exif_transpose(im).convert("RGB")
    if not icc:
        return rgb
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if "srgb" in ImageCms.getProfileDescription(src).lower():
            return rgb
        return ImageCms.profileToProfile(rgb, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
    except (ImageCms.PyCMSError, OSError, ValueError):
        return rgb


class Job:
    """Progress of the running expansion, polled by the page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state, self.fraction, self.text, self.message = "idle", 0.0, "", ""
        self.cancel = False
        self.index, self.count = 0, 1  # variation being generated (0-based) of count
        self.results = []  # finished variations, each like result
        self.selected = 0
        self.result = None  # the selected variation: {"canvas": RGB image, "layer": RGBA image, "margins": (l, t, r, b)}

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "fraction": round(self.fraction, 4), "text": self.text, "message": self.message,
                    "count": len(self.results), "selected": self.selected}

    def progress(self, fraction, text):
        with self.lock:
            f = max(0.0, min(1.0, float(fraction)))
            self.fraction = (self.index + f) / self.count
            self.text = (f"Variation {self.index + 1} of {self.count}: " if self.count > 1 else "") + text


# ---------------------------------------------------------------- settings

def load_settings(path, defaults=None):
    """Saved settings merged over the defaults."""
    try:
        saved = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        saved = {}
    defaults = DEFAULTS if defaults is None else defaults
    cur = {k: saved[k] if isinstance(saved.get(k), type(d)) else d for k, d in defaults.items()}
    if cur["method"] not in ("flux2", "flux1_fill"):
        raise ExpandError(f"unknown generation method: {cur['method']}")
    if cur["fill_clip_device"] not in ("cpu", "default"):
        raise ExpandError(f"unknown Fill text encoding device: {cur['fill_clip_device']}")
    return cur


def fill_settings(path, values=None, reset=False, defaults=None):
    """Read (and, given {name: value} or reset=True, validate and save) the settings."""
    defaults = DEFAULTS if defaults is None else defaults
    cur = dict(defaults) if reset else load_settings(path, defaults)
    for k, v in (values or {}).items():
        if k not in SETTINGS:
            raise ExpandError(f"unknown setting: {k}")
        kind, lo, hi, _ = SETTINGS[k]
        try:
            v = bool(v) if kind is bool else kind(v)
        except (TypeError, ValueError) as e:
            raise ExpandError(f"bad value for {k}") from e
        if kind in (int, float) and not lo <= v <= hi:
            raise ExpandError(f"{k} must be between {lo} and {hi}")
        if kind is str and k != "prompt" and not v.strip():
            raise ExpandError(f"{k} must not be empty")
        if k == "method" and v not in ("flux2", "flux1_fill"):
            raise ExpandError(f"unknown generation method: {v}")
        if k == "fill_clip_device" and v not in ("cpu", "default"):
            raise ExpandError(f"unknown Fill text encoding device: {v}")
        cur[k] = v
    if reset or values:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, indent=2))
    return cur


# ---------------------------------------------------------------- graph

def build_graph(image_name, margins, prompt, seed, s):
    """The API-format ComfyUI graph for one expansion. `s` is the settings dict. Outputs: node "out" (the picture, original
    pasted back through a feathered mask) and node "mask" (1 where the picture is new, soft inside the original)."""
    method = s.get("method", "flux2")
    if method == "flux1_fill":
        return build_fill_graph(image_name, margins, prompt, seed, s)
    if method != "flux2":
        raise ExpandError(f"unknown generation method: {method}")
    left, top, right, bottom = margins
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "ImagePadForOutpaint", "inputs": {"image": ["1", 0], "left": left, "top": top, "right": right,
                                                              "bottom": bottom, "feathering": int(s["feathering"])}},
        "3": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["2", 0], "upscale_method": "lanczos",
                                                                  "megapixels": float(s["megapixels"]), "resolution_steps": 16}},
        "4": {"class_type": "GetImageSize", "inputs": {"image": ["3", 0]}},
        "5": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": s["unet"]}},
        "6": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "flux2", "device": "default"}},
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {
            "text": BASE_PROMPT + (f" The new area should show: {prompt.strip()}" if prompt.strip() else ""), "clip": ["6", 0]}},
        "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["3", 0], "vae": ["7", 0]}},
        "10": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["8", 0], "latent": ["9", 0]}},
        "11": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["10", 0], "guidance": float(s["guidance"])}},
        "12": {"class_type": "BasicGuider", "inputs": {"model": ["5", 0], "conditioning": ["11", 0]}},
        "13": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": ["4", 0], "height": ["4", 1], "batch_size": 1}},
        "14": {"class_type": "Flux2Scheduler", "inputs": {"steps": int(s["steps"]), "width": ["4", 0], "height": ["4", 1]}},
        "15": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "16": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "17": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["16", 0], "guider": ["12", 0], "sampler": ["15", 0],
                                                                 "sigmas": ["14", 0], "latent_image": ["13", 0]}},
        "18": {"class_type": "VAEDecode", "inputs": {"samples": ["17", 0], "vae": ["7", 0]}},
        "21": {"class_type": "INPAINT_ColorMatch", "inputs": {"target": ["18", 0], "reference": ["3", 0],
                                                              "exclude_mask": ["2", 1], "strength": 1.0}},
        "22": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": s["upscale"]}},
        "23": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["22", 0], "image": ["21", 0]}},
        "24": {"class_type": "GetImageSize", "inputs": {"image": ["2", 0]}},
        "25": {"class_type": "ImageScale", "inputs": {"image": ["23", 0], "upscale_method": "lanczos", "width": ["24", 0],
                                                      "height": ["24", 1], "crop": "disabled"}},
        "28": {"class_type": "InvertMask", "inputs": {"mask": ["2", 1]}},
        "26": {"class_type": "ImageCompositeMasked", "inputs": {"destination": ["25", 0], "source": ["2", 0], "x": 0, "y": 0,
                                                                "resize_source": False, "mask": ["28", 0]}},
        "out": {"class_type": "PreviewImage", "inputs": {"images": ["26", 0]}},
        "29": {"class_type": "MaskToImage", "inputs": {"mask": ["2", 1]}},
        "mask": {"class_type": "PreviewImage", "inputs": {"images": ["29", 0]}},
    }
    if s["lora"]:
        g["20"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["5", 0], "lora_name": LORA_NAME, "strength_model": 1.0}}
        g["12"]["inputs"]["model"] = ["20", 0]
    if s["seam_correction"]:
        g["27"] = {"class_type": "SeamColorCorrect", "inputs": {"original": ["2", 0], "generated": ["25", 0], "new_area": ["2", 1],
                                                                "band": 128, "strength": 1.0, "max_offset": 24,
                                                                "min_offset": 1.5, "max_spread": 10}}
        g["26"]["inputs"]["destination"] = ["27", 0]
    if s["upscale"] == "lanczos":
        del g["22"], g["23"]
        g["25"]["inputs"]["image"] = ["21", 0]
    return g


def build_fill_graph(image_name, margins, prompt, seed, s):
    """Native Fill conditioning and only the fully corrected comparison-workflow pathway."""
    left, top, right, bottom = margins
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "ImagePadForOutpaint", "inputs": {
            "image": ["1", 0], "left": left, "top": top, "right": right, "bottom": bottom,
            "feathering": int(s["fill_feathering"])}},
        "3": {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["2", 0], "upscale_method": "lanczos", "megapixels": float(s["fill_megapixels"]),
            "resolution_steps": 16}},
        "4": {"class_type": "GetImageSize", "inputs": {"image": ["3", 0]}},
        "5": {"class_type": "MaskToImage", "inputs": {"mask": ["2", 1]}},
        "6": {"class_type": "ImageScale", "inputs": {
            "image": ["5", 0], "upscale_method": "bilinear", "width": ["4", 0], "height": ["4", 1], "crop": "disabled"}},
        "7": {"class_type": "ImageToMask", "inputs": {"image": ["6", 0], "channel": "red"}},
        "8": {"class_type": "UNETLoader", "inputs": {
            "unet_name": s["fill_unet"], "weight_dtype": "fp8_e4m3fn"}},
        "9": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": FILL_CLIP_L, "clip_name2": FILL_T5, "type": "flux",
            "device": s.get("fill_clip_device", "cpu")}},
        "10": {"class_type": "VAELoader", "inputs": {"vae_name": FILL_VAE}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["9", 0], "text": FILL_PROMPT + (f" The new area should show: {prompt.strip()}" if prompt.strip() else "")}},
        "12": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["11", 0], "guidance": float(s["fill_guidance"])}},
        "13": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["11", 0]}},
        "14": {"class_type": "InpaintModelConditioning", "inputs": {
            "positive": ["12", 0], "negative": ["13", 0], "vae": ["10", 0], "pixels": ["3", 0],
            "mask": ["7", 0], "noise_mask": False}},
        "16": {"class_type": "KSampler", "inputs": {
            "model": ["8", 0], "seed": int(seed), "steps": int(s["fill_steps"]), "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "positive": ["14", 0], "negative": ["14", 1],
            "latent_image": ["14", 2], "denoise": 1.0}},
        "18": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["10", 0]}},
        "21": {"class_type": "INPAINT_ColorMatch", "inputs": {
            "target": ["18", 0], "reference": ["3", 0], "exclude_mask": ["7", 0], "strength": 1.0}},
        "24": {"class_type": "GetImageSize", "inputs": {"image": ["2", 0]}},
        "25": {"class_type": "ImageScale", "inputs": {
            "image": ["21", 0], "upscale_method": "lanczos", "width": ["24", 0], "height": ["24", 1], "crop": "disabled"}},
        "27": {"class_type": "SeamColorCorrect", "inputs": {
            "original": ["2", 0], "generated": ["25", 0], "new_area": ["2", 1],
            "band": 128, "strength": 1.0, "max_offset": 24, "min_offset": 1.5, "max_spread": 10}},
        "28": {"class_type": "InvertMask", "inputs": {"mask": ["2", 1]}},
        "26": {"class_type": "ImageCompositeMasked", "inputs": {
            "destination": ["27", 0], "source": ["2", 0], "x": 0, "y": 0, "resize_source": False, "mask": ["28", 0]}},
        "out": {"class_type": "PreviewImage", "inputs": {"images": ["26", 0]}},
        "29": {"class_type": "MaskToImage", "inputs": {"mask": ["2", 1]}},
        "mask": {"class_type": "PreviewImage", "inputs": {"images": ["29", 0]}},
    }
    if s.get("fill_upscale", "lanczos") != "lanczos":
        g["22"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": s["fill_upscale"]}}
        g["23"] = {"class_type": "ImageUpscaleWithModel", "inputs": {
            "upscale_model": ["22", 0], "image": ["21", 0]}}
        g["25"]["inputs"]["image"] = ["23", 0]
    return g


def build_patch_graph(image_name, mask_name, prompt, seed, settings):
    """Reuse native Fill conditioning/correction; final masked paste is enforced locally."""
    graph = build_fill_graph(image_name, (0, 0, 0, 0), prompt, seed, settings)
    del graph["2"]
    graph["patch_image"] = {"class_type": "LoadImage", "inputs": {"image": mask_name}}
    graph["patch_mask"] = {"class_type": "ImageToMask", "inputs": {"image": ["patch_image", 0], "channel": "red"}}
    for node in graph.values():
        for key, value in node["inputs"].items():
            if value == ["2", 0]:
                node["inputs"][key] = ["1", 0]
            elif value == ["2", 1]:
                node["inputs"][key] = ["patch_mask", 0]
    graph["11"]["inputs"]["text"] = (
        "Fill the masked patch in this photograph with realistic content matching its surroundings, "
        "lighting, perspective and detail." + (f" {prompt.strip()}" if prompt.strip() else ""))
    graph["out"]["inputs"]["images"] = ["27", 0]
    # Do not blend twice: the local full-resolution paste below owns feathering.
    for key in ("26", "28"):
        del graph[key]
    return graph


def patch_mask(data, size):
    """Validate a browser PNG mask in source pixels before starting any job."""
    if not isinstance(data, str) or not data.startswith("data:image/png;base64,") or len(data) > 32 * 1024 * 1024:
        raise ExpandError("paint a PNG patch mask first (maximum encoded size 32 MiB)")
    try:
        raw = base64.b64decode(data.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != size:
                raise ExpandError("patch mask must match the full-resolution loaded image")
            mask = image.getchannel("A") if image.mode == "RGBA" else image.convert("L")
    except (ValueError, OSError) as error:
        raise ExpandError("invalid patch mask PNG") from error
    if mask.getextrema()[1] == 0:
        raise ExpandError("paint a patch before generating")
    return mask


def patch_alpha(mask, feather):
    """Feather inward only: zero mask pixels always remain exactly untouched."""
    values = np.asarray(mask, np.uint8)
    if not values.any():
        raise ExpandError("paint a patch before generating")
    if feather:
        support = np.pad((values > 0).astype(np.uint8), 1)
        distance = cv2.distanceTransform(support, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
        values = np.round(values.astype(np.float32) * np.minimum(distance / feather, 1)).astype(np.uint8)
    return Image.fromarray(values)


def patch_bounds(mask, context=100):
    """Interpolate from a padded all-stroke bounding box to the exact full source."""
    if isinstance(context, bool) or not isinstance(context, int) or not 0 <= context <= 100:
        raise ExpandError("patch context must be between 0 and 100")
    bounds = mask.getbbox()
    if bounds is None:
        raise ExpandError("paint a patch before generating")
    left, top, right, bottom = bounds
    pad_x = max(64, (right - left + 3) // 4)
    pad_y = max(64, (bottom - top + 3) // 4)
    left, top = max(0, left - pad_x), max(0, top - pad_y)
    right, bottom = min(mask.width, right + pad_x), min(mask.height, bottom + pad_y)
    return (left * (100 - context) // 100, top * (100 - context) // 100,
            mask.width - (mask.width - right) * (100 - context) // 100,
            mask.height - (mask.height - bottom) * (100 - context) // 100)


def patch_variations(picture, mask, prompt, job, seed=-1, settings=None, variations=1, url=None, on_result=None):
    """Generate same-size editable patches using the existing Fill settings and Comfy client."""
    if mask.size != picture.size or mask.mode != "L" or not mask.getextrema()[1]:
        raise ExpandError("paint a full-resolution patch mask before generating")
    n = int(variations)
    if not 1 <= n <= MAX_VARIATIONS:
        raise ExpandError(f"variations must be between 1 and {MAX_VARIATIONS}")
    settings = {**DEFAULTS, **(settings or {}), "method": "flux1_fill"}
    alpha = patch_alpha(mask, settings["fill_feathering"])
    if not alpha.getextrema()[1]:
        raise ExpandError("patch is too faint for this feathering; paint a larger patch or reduce feather px")
    bounds = patch_bounds(mask, settings.get("patch_context", 100))
    region, region_mask, region_alpha = picture.crop(bounds), mask.crop(bounds), alpha.crop(bounds)
    if job.cancel:
        raise Cancelled()
    readiness = status(settings, url)
    if not readiness["ok"]:
        raise ExpandError(readiness["message"])
    comfy = Comfy(url)
    job.count = n
    job.progress(0, f"Sending {region.width} x {region.height} context and patch mask to ComfyUI")
    identifier = uuid.uuid4().hex
    image_name = comfy.upload(_png(region), f"shppatch_{identifier}.png")
    mask_name = comfy.upload(_png(region_mask), f"shppatch_mask_{identifier}.png")
    base = int(seed) if int(seed) >= 0 else random.randrange(2 ** 31 - MAX_VARIATIONS)
    results = []
    for index in range(n):
        if job.cancel:
            raise Cancelled()
        job.index = index
        graph = build_patch_graph(image_name, mask_name, prompt, base + index, settings)
        client = uuid.uuid4().hex
        prompt_id = comfy.queue(graph, client)
        outputs = comfy.wait(prompt_id, client, graph, job)
        if job.cancel:
            raise Cancelled()
        try:
            with Image.open(io.BytesIO(comfy.view(outputs["out"]["images"][0]))) as output:
                if output.size != region.size:
                    raise ExpandError("ComfyUI patch result dimensions differ from the context region")
                generated = output.convert("RGB")
        except (KeyError, IndexError, OSError) as error:
            raise ExpandError("ComfyUI returned no valid patch picture") from error
        patch_layer = generated.convert("RGBA")
        patch_layer.putalpha(region_alpha)
        layer = picture.convert("RGBA")
        layer.putalpha(0)
        layer.paste(patch_layer, bounds[:2])
        canvas = picture.copy()
        canvas.paste(Image.composite(generated, region, region_alpha), bounds[:2])
        result = {"canvas": canvas, "layer": layer,
                  "margins": (0, 0, 0, 0), "mode": "patch"}
        if job.cancel:
            raise Cancelled()
        if on_result is not None:
            on_result(index, result)
        with job.lock:
            job.results.append(result)
            if job.result is None:
                job.selected, job.result = 0, result
        results.append(result)
    return results


STAGES = {"UnetLoaderGGUF": "Loading the model", "CLIPLoader": "Loading the text encoder", "CLIPTextEncode": "Reading the prompt",
          "VAEEncode": "Encoding the picture", "SamplerCustomAdvanced": "Generating", "VAEDecode": "Decoding",
          "INPAINT_ColorMatch": "Matching colours", "ImageUpscaleWithModel": "Upscaling", "SeamColorCorrect": "Correcting the seam",
          "ImageCompositeMasked": "Pasting the original back"}
STAGES.update(UNETLoader="Loading the Fill model", DualCLIPLoader="Loading the text encoders", KSampler="Generating")


# ---------------------------------------------------------------- ComfyUI client

class _WS:
    """Just enough of a WebSocket client to read ComfyUI's progress messages (stdlib only)."""

    def __init__(self, host, port, client_id):
        self.sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /ws?clientId={client_id} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("websocket closed")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError("websocket refused")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        op, n, i = b[0] & 0x0F, b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = int.from_bytes(b[2:4], "big"), 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = int.from_bytes(b[2:10], "big"), 10
        if len(b) < i + n:
            return None
        payload, self.buf = b[i:i + n], b[i + n:]
        return op, payload

    def poll(self, timeout):
        """Messages (dicts) that arrived within `timeout` seconds."""
        out, end = [], time.time() + timeout
        while True:
            while True:
                f = self._frame()
                if f is None:
                    break
                op, payload = f
                if op == 1:
                    try:
                        out.append(json.loads(payload.decode()))
                    except ValueError:
                        pass
                elif op == 8:
                    raise OSError("websocket closed")
                elif op == 9:
                    self._send(10, payload)
            left = end - time.time()
            if left <= 0 or out:
                return out
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return out
            if not chunk:
                raise OSError("websocket closed")
            self.buf += chunk

    def _send(self, op, payload):
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | op, 0x80 | len(payload)]) + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))


class Comfy:
    def __init__(self, url=None):
        self.url = (url or COMFY_URL).rstrip("/")

    def _req(self, method, path, data=None, headers=None, timeout=60):
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise ExpandError(self._http_error(e)) from e
        except (urllib.error.URLError, OSError) as e:
            raise ExpandError(f"ComfyUI is not reachable at {self.url}. Start it, then try again.") from e

    @staticmethod
    def _http_error(e):
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
        except ValueError:
            return f"ComfyUI answered {e.code}: {body[:300]}"
        parts = []
        err = j.get("error")
        if isinstance(err, dict):
            parts.append(err.get("message") or err.get("type") or "")
        elif err:
            parts.append(str(err))
        for nid, ne in (j.get("node_errors") or {}).items():
            for x in ne.get("errors", []):
                parts.append(f"{ne.get('class_type', nid)}: {x.get('message', '')} {x.get('details', '')}".strip())
        return "ComfyUI refused the graph: " + "; ".join(p for p in parts if p)[:600]

    def json(self, path):
        return json.loads(self._req("GET", path))

    def object_info(self):
        return self.json("/object_info")

    def upload(self, png, name):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("overwrite", "true"), ("type", "input")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                      "Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--\r\n".encode())
        res = json.loads(self._req("POST", "/upload/image", b"".join(parts),
                                   {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300))
        return (res.get("subfolder") + "/" if res.get("subfolder") else "") + res["name"]

    def queue(self, graph, client_id):
        body = json.dumps({"prompt": graph, "client_id": client_id}).encode()
        return json.loads(self._req("POST", "/prompt", body, {"Content-Type": "application/json"}))["prompt_id"]

    def interrupt(self, prompt_id):
        try:
            self._req("POST", "/queue", json.dumps({"delete": [prompt_id]}).encode(), {"Content-Type": "application/json"}, 10)
            self._req("POST", "/interrupt", b"{}", {"Content-Type": "application/json"}, 10)
        except ExpandError:
            pass

    def view(self, info):
        q = urllib.parse.urlencode({"filename": info["filename"], "subfolder": info.get("subfolder", ""), "type": info.get("type", "temp")})
        return self._req("GET", "/view?" + q, timeout=300)

    def wait(self, prompt_id, client_id, graph, job):
        """Block until the prompt finished; returns its outputs. Progress goes to `job`; Cancelled when job.cancel is set."""
        host = urllib.parse.urlparse(self.url)
        try:
            ws = _WS(host.hostname, host.port or 80, client_id)
        except OSError:
            ws = None
        seen, total, last_poll = set(), max(1, len(graph)), 0.0
        try:
            while True:
                if job.cancel:
                    self.interrupt(prompt_id)
                    raise Cancelled()
                try:
                    msgs = ws.poll(0.4) if ws else (time.sleep(0.4) or [])
                except OSError:
                    ws, msgs = None, []
                for m in msgs:
                    d = m.get("data") or {}
                    if d.get("prompt_id") not in (None, prompt_id):
                        continue
                    if m.get("type") == "execution_cached":
                        seen.update(d.get("nodes", []))
                    elif m.get("type") == "executing" and d.get("node"):
                        seen.add(d["node"])
                        job.progress(len(seen) / total, STAGES.get(graph.get(d["node"], {}).get("class_type"), "Working"))
                    elif m.get("type") == "progress" and d.get("max"):
                        job.progress((len(seen) - 1 + d["value"] / d["max"]) / total, f"Generating: step {d['value']} of {d['max']}")
                if time.time() - last_poll >= 1.0:
                    last_poll = time.time()
                    h = self.json(f"/history/{prompt_id}").get(prompt_id)
                    if h:
                        st = h.get("status") or {}
                        if st.get("status_str") == "error":
                            raise ExpandError(self._exec_error(st))
                        if h.get("outputs") or st.get("completed"):
                            return h.get("outputs", {})
        finally:
            if ws:
                ws.close()

    @staticmethod
    def _exec_error(status):
        for kind, data in status.get("messages", []):
            if kind == "execution_error":
                return f"ComfyUI failed in {data.get('node_type', 'a node')}: {data.get('exception_message', '').strip()}"
            if kind == "execution_interrupted":
                return "ComfyUI interrupted the run"
        return "ComfyUI reported an error"


def status(settings, url=None):
    """What the page needs to know: is ComfyUI up with everything the graph uses. {ok, reachable, message, unets, upscalers}."""
    try:
        info = Comfy(url).object_info()
    except ExpandError as e:
        return {"ok": False, "reachable": False, "message": str(e), "unets": [], "upscalers": []}

    def options(node, field):
        inputs = info.get(node, {}).get("input", {})
        spec = ((inputs.get("required", {}) or {}).get(field)
                or (inputs.get("optional", {}) or {}).get(field))
        if not spec:
            return []
        first = spec[0]
        return list(first) if isinstance(first, list) else list(((spec[1] if len(spec) > 1 else {}) or {}).get("options", []))

    method = settings.get("method", "flux2")
    if method not in ("flux2", "flux1_fill"):
        raise ExpandError(f"unknown generation method: {method}")
    fill = method == "flux1_fill"
    unets = options("UNETLoader" if fill else "UnetLoaderGGUF", "unet_name")
    upscalers = options("UpscaleModelLoader", "model_name")
    required = list(FILL_REQUIRED_NODES if fill else REQUIRED_NODES)
    if not fill and settings["upscale"] == "lanczos":
        required = [node for node in required if node not in ("UpscaleModelLoader", "ImageUpscaleWithModel")]
    if fill and settings.get("fill_upscale", "lanczos") != "lanczos":
        required += ["UpscaleModelLoader", "ImageUpscaleWithModel"]
    missing = [n for n in required if n not in info]
    problems = ["ComfyUI lacks the nodes: " + ", ".join(missing)] if missing else []
    if fill:
        wanted = [("FLUX.1 Fill model", unets, settings["fill_unet"]),
                  ("CLIP-L", options("DualCLIPLoader", "clip_name1"), FILL_CLIP_L),
                  ("T5 encoder", options("DualCLIPLoader", "clip_name2"), FILL_T5),
                  ("Fill VAE", options("VAELoader", "vae_name"), FILL_VAE)]
        if settings.get("fill_upscale", "lanczos") != "lanczos":
            wanted.append(("Fill upscale model", upscalers, settings["fill_upscale"]))
        for node, field, value in (("UNETLoader", "weight_dtype", "fp8_e4m3fn"),
                                   ("DualCLIPLoader", "type", "flux"),
                                   ("DualCLIPLoader", "device", settings.get("fill_clip_device", "cpu"))):
            if value not in options(node, field):
                problems.append(f"ComfyUI {node} lacks {field} '{value}'; update the node/runtime")
    else:
        wanted = [("diffusion model", unets, settings["unet"]),
                  ("text encoder", options("CLIPLoader", "clip_name"), CLIP_NAME),
                  ("VAE", options("VAELoader", "vae_name"), VAE_NAME)]
        if settings["upscale"] != "lanczos":
            wanted.append(("upscale model", upscalers, settings["upscale"]))
    problems += [f"{label} '{want}' is not installed" for label, have, want in wanted if want not in have]
    if not fill and settings["lora"] and LORA_NAME not in options("LoraLoaderModelOnly", "lora_name"):
        problems.append(f"LoRA '{LORA_NAME}' is not installed (untick the LoRA setting to run without it)")
    return {"ok": not problems, "reachable": True, "message": "; ".join(problems), "unets": unets, "upscalers": upscalers}


# ---------------------------------------------------------------- generation

def _png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def expand_variations(picture, margins, prompt, job, seed=-1, settings=None, variations=1, url=None,
                      on_result=None):
    """Run `variations` (1-4) generations with different seeds one after another. Fills job.results; job.result is
    the selected one (the first until the user picks). Completed results survive cancellation."""
    n = int(variations)
    if not 1 <= n <= MAX_VARIATIONS:
        raise ExpandError(f"variations must be between 1 and {MAX_VARIATIONS}")
    s = dict(DEFAULTS, **(settings or {}))
    st = status(s, url)
    if not st["ok"]:
        raise ExpandError(st["message"])
    base = int(seed) if int(seed) >= 0 else random.randrange(2 ** 31 - MAX_VARIATIONS)
    comfy = Comfy(url)
    job.count = n
    job.progress(0, "Sending the picture to ComfyUI")
    cl, ct, cr, cb = (max(0, -m) for m in margins)  # negative margins crop the picture before the rest is generated
    kept = picture.crop((cl, ct, picture.width - cr, picture.height - cb)) if (cl or ct or cr or cb) else picture
    pad = tuple(max(0, m) for m in margins)
    name = comfy.upload(_png(kept), f"shpcrop_{os.getpid()}.png")
    results = []
    for i in range(n):
        if job.cancel:
            raise Cancelled()
        job.index = i
        r = expand(comfy, name, kept.size, pad, prompt, job, base + i, s)
        r["margins"] = tuple(margins)  # the signed margins relative to the original picture, for Send to PhotoCraft
        results.append(r)
        if on_result is not None:
            on_result(i, r)
        with job.lock:
            job.results.append(r)
            if job.result is None:
                job.selected, job.result = 0, r
    return results


def expand(comfy, image_name, size, margins, prompt, job, seed, s):
    """One generation. Returns {"canvas": RGB, "layer": RGBA (opaque over the new area, soft inside the original), "margins"}."""
    graph = build_graph(image_name, margins, prompt, seed, s)
    client_id = uuid.uuid4().hex
    pid = comfy.queue(graph, client_id)
    outputs = comfy.wait(pid, client_id, graph, job)
    try:
        canvas = Image.open(io.BytesIO(comfy.view(outputs["out"]["images"][0]))).convert("RGB")
        mask = Image.open(io.BytesIO(comfy.view(outputs["mask"]["images"][0]))).convert("L")
    except (KeyError, IndexError) as e:
        raise ExpandError("ComfyUI returned no picture") from e
    left, top, right, bottom = margins
    want = (size[0] + left + right, size[1] + top + bottom)
    if canvas.size != want or mask.size != want:
        raise ExpandError(f"ComfyUI returned {canvas.size[0]}x{canvas.size[1]}, expected {want[0]}x{want[1]}")
    layer = canvas.convert("RGBA")
    layer.putalpha(mask)
    return {"canvas": canvas, "layer": layer, "margins": tuple(margins)}
