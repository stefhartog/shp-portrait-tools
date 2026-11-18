"""Full-resolution result and layer assembly; callers own I/O and source lifetime."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import numpy as np

from . import directional_blur as DB
from . import dodge_burn as D
from . import frequency_separation as FS
from . import progress
from . import skintone as ST


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


def combine_layers(p, source, *, full_mask, skintone_layers, cached_bands, mask8, send_fs, send_bands):
    mask = full_mask(p)
    fs_layers = skintone_layers(source, mask, p, cache_full=True) if p["combine"]["fs_mix"] > 0 else {}
    bands = []
    if p["combine"]["dnb_mix"] > 0 and (p["band1_on"] or p["second_band"]["on"]):
        bands, _, _ = cached_bands(p, "unmasked", mask=mask)
    payload = _rgb_bands(bands)
    if not fs_layers:
        if not payload:
            raise ValueError("enable an FS or D&B contribution before Send Layers")
        return send_bands(payload, mask8(mask), band_mix=p["combine"]["dnb_mix"])
    mix = p["combine"]["fs_mix"] / 100
    for key in ("color", "blur", "directional"):
        if key in fs_layers:
            fs_layers[key] = source + (fs_layers[key] - source) * mix
    fs_layers["detail"] = .5 + (fs_layers["detail"] - .5) * mix
    directional = None
    if "directional" in fs_layers:
        directional = (FS.rgb16(fs_layers.pop("directional")), mask8(fs_layers.pop("directional_mask")),
                       fs_layers.pop("directional_strength"))
    rgb = {key: FS.rgb16(value) for key, value in fs_layers.items()}
    settings = {**p["skintone"], "hue_amount": p["skintone"]["hue_amount"] * mix}
    return send_fs(rgb, settings, mask8(mask), include_blur=p["frequency"]["on"],
                   blur_strength=p["frequency"]["blur_strength"], blur_masked=p["frequency"]["blur_mask"],
                   directional=directional, bands=payload, band_mask8=mask8(mask) if payload else None,
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
