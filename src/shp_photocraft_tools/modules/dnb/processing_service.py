"""Full-resolution result and layer assembly; callers own I/O and source lifetime."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import logging

import numpy as np

from . import cuda_bands
from . import directional_blur as DB
from . import dodge_burn as D
from . import frequency_separation as FS
from . import progress
from . import skintone as ST


logger = logging.getLogger(__name__)


DNB_BACKENDS = ("cpu", "cuda")


def dnb_backend_settings(settings):
    backend = settings.get("dnb_layer_backend", "cpu")
    if backend not in DNB_BACKENDS:
        raise ValueError("D&B layer backend must be cpu or cuda")
    return backend


def cached_dnb_band(p, img, index, mask, mask_result, *, backend="cpu", before_fallback=None):
    """Render a single cache miss; preview and flattened processing stay on CPU."""
    sigma = p["layer_blur_at_s181"] * p["S"] / 181
    second = {**p["second_band"], "on": index == 2}
    if backend == "cuda":
        bands, amount = (p["bands"], p["strength"]) if index == 1 else (
            [(second["lo"], second["hi"], 1, second["taper"])], second["strength"])
        try:
            return progress.cache_step(
                "calculated", "Calculating D&B band on CUDA", cuda_bands.render_band,
                img, p["S"], bands, amount, p["edge"], p["tau"], p["max_ev"], sigma,
                mask=mask, invert=index == 2 and second["invert"])
        except cuda_bands.CudaUnavailable as exc:
            logger.warning("D&B CUDA unavailable; using CPU: %s", exc)
            if before_fallback is not None:
                before_fallback()
        label = "Calculating D&B band on CPU (CUDA fallback)"
    else:
        label = "Calculating D&B band"
    layers, _, _, _ = progress.cache_step(
        "calculated", label, D.band_stack, img, p["S"], p["bands"], p["strength"], second,
        first_on=index == 1, sigma=sigma, edge=p["edge"], tau=p["tau"], max_ev=p["max_ev"],
        mask_result=mask_result, render_result=False, mask=mask)
    return layers[0][1]


def dnb_result(p, img, masks, *, base_skin_mask, final_mask, layered=False, commit_guard=None):
    mask = progress.call("Preparing D&B base skin/color mask", base_skin_mask, img, masks, p, p["S"])
    mask_result = final_mask(p, base_mask=mask, commit_guard=commit_guard)
    layers, after, _, total = progress.call(
        "Calculating D&B layers and final mask", D.band_stack,
        img, p["S"], p["bands"], p["strength"], p["second_band"],
        first_on=p["band1_on"], sigma=p["layer_blur_at_s181"] * p["S"] / 181,
        edge=p["edge"], tau=p["tau"], max_ev=p["max_ev"], shadows=p["shadows"],
        highlights=p["highlights"], mask=mask, tone_strength=p["tone_strength"],
        protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"],
        mask_result=mask_result,
    )
    return (layers if layered else first_band(layers, img.shape[:2])), after, total


def first_band(layers, shape):
    return next((a for index, a in layers if index == 1), np.full(shape, .5, np.float32))


def _rgb_bands(bands):
    return [(index, np.repeat(np.round(gray * 65535).astype(np.uint16)[..., None], 3, axis=2))
            for index, gray in bands]


def dnb_layers(p, shape, *, cached_bands, mask8):
    layers, total, _ = cached_bands(p, "masked")
    if not layers:
        raise ValueError("enable at least one D&B band before Send")
    mask = total if total is not None else np.ones(shape, np.float32)
    return _rgb_bands(layers), mask8(mask)


def shared_mask(p, full_mask, *, report_progress=False):
    if report_progress:
        return progress.call("Preparing shared mask", full_mask, p)
    return full_mask(p)


def skin_result(p, source, *, full_mask, skintone_layers, combined_result, report_progress=False):
    """Prepare one shared mask for flattened FS/Combine or a mask-only export."""
    mask = shared_mask(p, full_mask, report_progress=report_progress)
    if p["mode"] == "mask":
        return mask, {}, None
    if p["mode"] == "combine":
        _, _, result = combined_result(source, mask, p)
        return mask, {}, result
    if report_progress:
        layers = progress.call("Calculating FS + Skintone result", skintone_layers, source, mask, p)
    else:
        layers = skintone_layers(source, mask, p)
    _, result = ST.composite(layers, mask, p["skintone"], p["frequency"])
    return mask, layers, result


def combine_frequency(p):
    f = p["frequency"]
    return {**f, "blur_strength": f["blur_strength"] * p["combine"]["fs_mix"] / 100}


def combine_composite(layers, mask, p, bands, mask_placement):
    """Render the editor stack from Color through Corrections to Linear Light Detail."""
    f = combine_frequency(p)
    if mask_placement == "folder":
        f = {**f, "blur_mask": False}
        correction_mask = np.ones_like(mask)
        corrections = {**layers, "directional_mask": correction_mask} if "directional" in layers else layers
    else:
        corrections = layers
        correction_mask = mask
    color = ST.process(ST.base(corrections, correction_mask, f), correction_mask, p["skintone"])
    band_alpha = correction_mask[..., None] * (p["combine"]["dnb_mix"] / 100)
    if p["combine"]["dnb_mix"] > 0:
        for _, gray in bands:
            color = np.clip(color + (D.softlight(color, gray[..., None]) - color) * band_alpha, 0, 1)
    if mask_placement == "folder":
        color = layers["color"] + (color - layers["color"]) * mask[..., None]
    return np.clip(color + 2 * layers["detail"] - 1, 0, 1)


def combine_layers(p, source, *, full_mask, skintone_layers, cached_bands, mask8, send_fs, mask_placement):
    mask = full_mask(p)
    f = combine_frequency(p)
    fs_layers = skintone_layers(source, mask, {**p, "frequency": f}, cache_full=True, native=True,
                                directional_mask_placement=mask_placement)
    bands = []
    if p["combine"]["dnb_mix"] > 0 and (p["band1_on"] or p["second_band"]["on"]):
        bands, _, _ = cached_bands(p, "unmasked", mask=mask)
    payload = _rgb_bands(bands)
    directional = None
    if "directional" in fs_layers:
        directional = (fs_layers.pop("directional"), mask8(fs_layers.pop("directional_mask")),
                       fs_layers.pop("directional_strength"))
    shared = mask8(mask)
    return send_fs(fs_layers, p["skintone"], shared, include_blur=f["on"],
                   blur_strength=f["blur_strength"], blur_masked=f["blur_mask"],
                   directional=directional, bands=payload, band_mask8=shared if payload else None,
                   band_mix=p["combine"]["dnb_mix"])


def frequency_layers(p, source, pose, *, full_mask, skintone_layers,
                     cached_split, cached_blur, reserve, mask8, send):
    """Keep the one compute worker alive through the caller's synchronous send."""
    error = ST.source_error(source)
    if error:
        raise ValueError(error)
    f = p["frequency"]
    effective, _ = DB.effective_settings(p.get("directional", DB.DEFAULTS), pose, source.shape)
    if effective["on"]:
        # Bounded serial region work avoids nesting another executor.
        mask = full_mask(p)
        layers = skintone_layers(source, mask, p, native=True)
        directional = (layers.pop("directional"), mask8(layers.pop("directional_mask")),
                       layers.pop("directional_strength"))
        return send(layers, p["skintone"], mask8(mask), include_blur=f["on"],
                    blur_strength=f["blur_strength"], blur_masked=f["blur_mask"], directional=directional)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="skin-fs") as pool:
        mask_job = pool.submit(copy_context().run, progress.call, "Preparing shared mask", full_mask, p)
        try:
            split_key, layers = cached_split(source, f)
            mask = mask_job.result()
            if not f["on"]:
                pixels = {key: progress.call("Preparing RGB16 " + key.title(), FS.rgb16, value)
                          for key, value in layers.items()}
                return send(pixels, p["skintone"], mask8(mask), include_blur=False)

            def prepare_blur(fs_key, color, skin):
                blur = cached_blur(fs_key, color, skin, f, reserve(source))
                return progress.call("Preparing RGB16 Blur", FS.rgb16, blur)

            blur_job = pool.submit(copy_context().run, prepare_blur, split_key, layers["color"], mask)
            try:
                # The worker uses unquantized Color and the original soft mask.
                for key in ("color", "detail"):
                    layers[key] = progress.call("Preparing RGB16 " + key.title(), FS.rgb16, layers[key])
                pixels_mask = mask8(mask)
                del mask
                return send(layers, p["skintone"], pixels_mask, blur_future=blur_job,
                            blur_strength=f["blur_strength"], blur_masked=f["blur_mask"])
            finally:
                blur_job.cancel()
        finally:
            mask_job.cancel()
