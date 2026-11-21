"""Assemble Save artifacts and dispatch prepared results to existing destinations."""

import datetime
import json
from pathlib import Path
import time

import cv2
import numpy as np

from . import frequency_separation as FS
from . import processing_service, progress
from . import skintone as ST
from .photocraft_bridge import tagged_rgb16


def _bundle_source(state):
    if state["path"] is not None:
        return Path(state["path"]).stem, {"image": state["path"]}
    return state["capture_name"], {"image": None, "source": state["capture_provenance"]}


def save_bundle(req, p, *, state, root, srgb, settings_for_request, transfer_options, u8,
                dnb_full_result, prepare_skin_result, validate_skintone_source):
    if p["mode"] == "combine":
        return _save_combined(req, p, state, root, srgb, settings_for_request, transfer_options,
                              prepare_skin_result, validate_skintone_source)
    if p["mode"] != "dnb":
        return _save_skin(req, p, state, root, srgb, settings_for_request, transfer_options, u8,
                          prepare_skin_result, validate_skintone_source)
    t = time.time()
    img = state["img"]
    layers, after, total = dnb_full_result(p, layered=True)
    cs = processing_service.first_band(layers, img.shape[:2])
    stem, provenance = _bundle_source(state)
    out = root / "results" / f"{stem}_{datetime.datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    transfer = transfer_options()
    (out / f"{stem}_1_result.{transfer['format']}").write_bytes(
        tagged_rgb16(FS.rgb16(after), state["icc"] or srgb, transfer))
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
    settings = {k: v for k, v in settings_for_request(req).items() if k != "view"}
    settings["output_settings"] = transfer
    settings.update(**provenance, stats={"p95": float(np.percentile(np.abs(d), 95) * 255), "max": float(np.abs(d).max() * 255)},
                    layer_order=[f"band {index}" for index, _ in reversed(layers)],
                    band1_on=p["band1_on"], second_band=p["second_band"],
                    files={"1_result": "picture with the settings applied (combined mask applied)",
                           "2_skinmask": "ONE mask = skin x color range x tone range x structure protection (8-bit gray; all white if none are used); use as the layer mask of file 3",
                           "3_softlight_layer": "16-bit RGB PNG (three identical gray channels, so no gray-profile conversion on import), Soft Light blend mode, 50% gray = no change, NO mask applied"},
                    soft_light="W3C soft-light formula; Photoshop's soft light differs slightly",
                    colour="embedded source ICC profile, or sRGB when source had none; flattened result is RGB16")
    (out / "settings.json").write_text(json.dumps(settings, indent=2))
    return {"dir": str(out), "files": sorted(f.name for f in out.iterdir()), "seconds": round(time.time() - t, 1)}


def _save_combined(req, p, state, root, srgb, settings_for_request, transfer_options,
                   prepare_skin_result, validate_skintone_source):
    validate_skintone_source()
    _, _, result = prepare_skin_result(p)
    stem, provenance = _bundle_source(state)
    out=root/"results"/f"{stem}_combine_{datetime.datetime.now():%Y%m%d-%H%M%S-%f}"
    out.mkdir(parents=True,exist_ok=False)
    transfer = transfer_options()
    filename = "result." + transfer["format"]
    (out/filename).write_bytes(tagged_rgb16(FS.rgb16(result),state["source_icc"] or srgb,transfer))
    (out/"settings.json").write_text(json.dumps({**settings_for_request(req), **provenance,
                                               "output_settings":transfer},indent=2))
    return {"dir":str(out),"files":[filename,"settings.json"]}


def _save_skin(req, p, state, root, srgb, settings_for_request, transfer_options, u8,
               prepare_skin_result, validate_skintone_source):
    if p["mode"] == "frequency":
        validate_skintone_source()
    t = time.time()
    mask, layers, result = prepare_skin_result(p)
    stem, provenance = _bundle_source(state)
    out = root / "results" / f"{stem}_{p['mode']}_{datetime.datetime.now():%Y%m%d-%H%M%S-%f}"
    out.mkdir(parents=True, exist_ok=True)
    mask_name = "skinmask.png"
    if not cv2.imwrite(str(out / mask_name), u8(mask)):
        raise ValueError("could not write skin mask")
    for key in ("color", "blur", "detail", "directional"):
        if key not in layers:
            continue
        pixels = layers[key]
        (out / f"{key}.png").write_bytes(tagged_rgb16(FS.rgb16(pixels), state["source_icc"] or srgb))
    if "directional_mask" in layers:
        if not cv2.imwrite(str(out / "directional_mask.png"), u8(layers["directional_mask"])):
            raise ValueError("could not write Directional Blur mask")
    if result is not None:
        transfer = transfer_options()
        (out / ("result." + transfer["format"])).write_bytes(
            tagged_rgb16(FS.rgb16(result), state["source_icc"] or srgb, transfer))
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
    (out / "settings.json").write_text(json.dumps({**settings_for_request(req), **provenance,
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


def send_editor_layers(p, *, shape, document, source_icc, bridge, u8, full_skin_mask,
                       send_combined_layers, send_skintone_parallel, cached_dnb_bands, send_band_payload):
    if p["mode"] == "combine":
        return {"sent": True, **send_combined_layers(p)}
    if p["mode"] == "frequency":
        return {"sent": True, **send_skintone_parallel(p)}
    if p["mode"] == "mask":
        mask = processing_service.shared_mask(p, full_skin_mask, report_progress=True)
        mask8 = u8(mask)
        del mask
        return {"sent": True, **bridge().send_mask(mask8, document, source_icc=source_icc)}
    payload, mask8 = processing_service.dnb_layers(p, shape, cached_bands=cached_dnb_bands, mask8=u8)
    return {"sent": True, **send_band_payload(payload, mask8)}


def send_editor_result(p, *, document, bridge, skin_result):
    result, icc = skin_result(p)
    return {"sent": True, **bridge().send_result(FS.rgb16(result), document, source_icc=icc)}


def send_local_result(p, *, state, srgb, transfer_options, u8, dnb_full_result,
                      prepare_skin_result, write_local_result):
    transfer = transfer_options() if p["mode"] != "mask" else None
    if p["mode"] == "dnb":
        cs, result, total = dnb_full_result(p)
        del cs, total
        data = progress.call("Encoding full-resolution result", tagged_rgb16,
                             FS.rgb16(result), state["icc"] or srgb, transfer)
    else:
        mask, _, result = prepare_skin_result(p, report_progress=True)
        if p["mode"] == "mask":
            ok, encoded = cv2.imencode(".png", u8(mask))
            if not ok:
                raise ValueError("could not encode the skin mask")
            data = encoded.tobytes()
        else:
            data = progress.call("Encoding full-resolution result", tagged_rgb16,
                                 FS.rgb16(result), state["source_icc"] or srgb, transfer)
    suffix = {"dnb": "shpdnb", "mask": "shpskinmask", "frequency": "shpfs", "combine":"shpcombine"}[p["mode"]]
    path = progress.call("Saving result beside source", write_local_result, state["path"], suffix, data,
                         state.get("source_directory"),
                         extension="." + transfer["format"] if transfer else ".png")
    return {"saved": True, "path": path}
