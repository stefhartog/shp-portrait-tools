"""Skin Tools' editor boundary for the Photoshop UXP connector (numeric RGB, not converted)."""

import hashlib
import io
import math
from pathlib import Path
import secrets

import numpy as np
from PIL import ImageCms

from shp_photocraft_tools.shared.photocraft import ControlError
from shp_photocraft_tools.shared.photoshop import CHUNK, MAX_PIXELS, MAX_TRANSFER, PROTOCOL
from . import frequency_separation as FS, skintone as ST
from .photocraft_bridge import tagged_rgb16, transfer_settings


def _opacity(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ControlError("Layer opacity must be 0-100 percent")
    return value


def _mask(mask, shape):
    if not isinstance(mask, np.ndarray) or mask.dtype != np.uint8 or mask.shape != shape:
        raise ControlError("Photoshop masks must be matching grayscale8 images")
    return mask


def _pixels(pixels, shape):
    if not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint16 or pixels.shape != (*shape, 3):
        raise ControlError("Photoshop layers need matching full RGB16 images")
    return pixels


class Bridge:
    def __init__(self, broker):
        self.broker = broker

    def launch(self):
        raise ControlError("Start Photoshop normally and open Plugins > Skin Tools Connector; no special launcher is needed")

    def test_native_frequency(self, *args, **kwargs):
        raise ControlError("Test Native FS is PhotoCraft-only; use Send Layers for Photoshop")

    def status(self):
        result = self.broker.execute("status", {})
        return {"connected": True, "doc": result.get("doc"), "headless": False}

    def fetch(self, inbox):
        inbox = Path(inbox)

        def finish(job, result):
            doc = result.get("doc")
            if not isinstance(doc, dict):
                raise ControlError("Photoshop returned no source identity")
            w, h, depth = (doc.get(key) for key in ("width", "height", "depth"))
            if (type(w) is not int or type(h) is not int or not 0 < w * h <= MAX_PIXELS
                    or min(w, h) <= 0 or depth not in (8, 16) or doc.get("mode") != "RGB"
                    or doc.get("session") != job.session or doc.get("protocol") != PROTOCOL
                    or type(doc.get("index")) is not int or not isinstance(doc.get("capture"), str)
                    or not isinstance(doc.get("marker"), str) or not isinstance(doc.get("profile"), str)
                    or doc.get("alpha") != "opaque"):
                raise ControlError("Invalid/unsupported Photoshop source; RGB 8/16-bit, opaque composite required")
            path, profile_path = job.blob("pixels"), job.blob("icc")
            expected = w * h * 3 * (depth // 8)
            if not path.is_file() or path.stat().st_size != expected:
                raise ControlError("Photoshop composite byte count does not match the full canvas")
            if not profile_path.is_file() or not 128 <= profile_path.stat().st_size <= 4 * 1024**2:
                raise ControlError("Select the source document's exact RGB ICC file in the connector")
            icc = profile_path.read_bytes()
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                name = ImageCms.getProfileDescription(profile).strip().rstrip("\0")
                if profile.profile.xcolor_space.strip() != "RGB" or name != doc["profile"]:
                    raise ValueError("ICC description/colour space differs from the Photoshop document")
            except (OSError, ValueError, TypeError) as error:
                raise ControlError(f"Cannot verify Photoshop ICC identity: {error}; select the exact matching ICC file") from error
            doc = {**doc, "editor": "photoshop", "profile_hash": hashlib.sha256(icc).hexdigest()}
            raw = np.fromfile(path, dtype="<u2" if depth == 16 else np.uint8).reshape(h, w, 3)
            bgr = np.ascontiguousarray(raw[..., ::-1])
            del raw
            if depth == 8:
                bgr = bgr.astype(np.uint16) * 257
            inbox.mkdir(parents=True, exist_ok=True)
            destination = inbox / f"photoshop-{secrets.token_hex(8)}.png"
            destination.write_bytes(tagged_rgb16(bgr, icc, {"opaque_alpha": False}))
            return destination, doc

        return self.broker.execute("capture", {}, finish=finish)

    def _send(self, nodes, doc, source_icc):
        if (not isinstance(doc, dict) or doc.get("editor") != "photoshop"
                or doc.get("session") != self.broker.session):
            raise ControlError("Photoshop session changed; Get Image again")
        if not isinstance(source_icc, bytes) or hashlib.sha256(source_icc).hexdigest() != doc.get("profile_hash"):
            raise ControlError("The captured Photoshop ICC identity changed; Get Image again")
        h, w = doc["height"], doc["width"]

        def prepare(job):
            blobs, size = {}, 0

            def binary(pixels, components):
                nonlocal size
                _pixels(pixels, (h, w)) if components == 3 else _mask(pixels, (h, w))
                name = f"b{len(blobs)}"
                depth = doc["depth"] if components == 3 else 8
                length = h * w * components * (depth // 8)
                size += length
                if size > MAX_TRANSFER:
                    raise ControlError("Photoshop layer bundle exceeds the 2 GiB safety limit")
                rows = max(1, CHUNK // (w * components * (depth // 8)))
                with job.blob(name).open("wb") as stream:
                    for y in range(0, h, rows):
                        tile = pixels[y:y + rows]
                        if components == 3:
                            tile = tile[..., ::-1]
                            tile = ((tile.astype(np.uint32) + 128) // 257).astype(np.uint8) if depth == 8 else tile.astype("<u2")
                        stream.write(tile.tobytes())
                blobs[name] = {"bytes": length, "components": components, "depth": depth,
                               "width": w, "height": h}
                return name

            def encode(node):
                result = {key: node[key] for key in ("name", "blend", "opacity")}
                if "children" in node:
                    result["children"] = [encode(child) for child in node["children"]]
                else:
                    result["pixels"] = binary(node["pixels"], 3)
                if node.get("mask") is not None:
                    result["mask"] = binary(node["mask"], 1)
                return result

            job.params.update({"doc": doc, "layers": [encode(node) for node in nodes], "blobs": blobs})

        return self.broker.execute("apply", {}, prepare=prepare)

    @staticmethod
    def _layer(name, pixels, blend="Normal", opacity=100, mask=None):
        return {"name": name, "pixels": pixels, "blend": blend, "opacity": _opacity(opacity), "mask": mask}

    @staticmethod
    def _group(children, mask, blend="Normal"):
        return {"name": "SkinTools", "children": children, "blend": blend, "opacity": 100, "mask": mask}

    def send_result(self, pixels, doc, source_icc):
        return self._send([self._layer("Corrected Result", pixels)], doc, source_icc)

    def send_mask(self, mask8, doc, *, source_icc=None, name="Skin mask"):
        _mask(mask8, (doc["height"], doc["width"]))
        return self._send([self._layer(name, np.repeat((mask8.astype(np.uint16) * 257)[..., None], 3, axis=2))],
                          doc, source_icc)

    def send_skin_bands(self, layers, mask8, doc, *, source_icc=None, band_mix=100, mask_placement="folder"):
        shape = (doc["height"], doc["width"])
        _mask(mask8, shape)
        if mask_placement not in ("folder", "layers"):
            raise ControlError("Mask placement must be folder or layers")
        nodes = self._bands(layers, shape, mask8 if mask_placement == "layers" else None, band_mix)
        if not nodes:
            raise ControlError("Enable at least one D&B band")
        return self._send([self._group(nodes, mask8 if mask_placement == "folder" else None, "Pass Through")],
                          doc, source_icc)

    def _bands(self, bands, shape, mask, opacity):
        _opacity(opacity)
        if not isinstance(bands, (list, tuple)):
            raise ControlError("D&B bands must be (index, RGB16) pairs")
        seen, nodes = set(), []
        for item in bands:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ControlError("D&B bands must be (index, RGB16) pairs")
            index, pixels = item
            if type(index) is not int or index not in (1, 2) or index in seen:
                raise ControlError("D&B bands must have unique indices 1 or 2")
            seen.add(index)
            nodes.append((index, self._layer(f"D&B band {index}", _pixels(pixels, shape), "Soft Light", opacity, mask)))
        return [node for _, node in sorted(nodes, reverse=True)]

    def send_skintone(self, layers, settings, mask8, doc, source_icc, *, blur_future=None, transfer=None,
                      include_blur=True, blur_strength=100, blur_masked=True, directional=None,
                      bands=None, band_mask8=None, band_mix=100, mask_placement="folder"):
        transfer_settings(transfer)
        shape = (doc["height"], doc["width"])
        _mask(mask8, shape)
        _opacity(blur_strength)
        if mask_placement not in ("folder", "layers") or type(blur_masked) is not bool:
            raise ControlError("Invalid Photoshop mask placement/Blur mask setting")
        layers = dict(layers)
        if blur_future is not None:
            if not include_blur:
                raise ControlError("Disabled Blur must not have a Blur job")
            layers["blur"] = blur_future.result()
        if set(layers) != ({"color", "blur", "detail"} if include_blur else {"color", "detail"}):
            raise ControlError("FS needs matching Color, optional Blur and Detail")
        for pixels in layers.values():
            _pixels(pixels, shape)
        per_layer = mask_placement == "layers"
        children = [self._layer("Color", layers["color"])]
        # Hue is a rendered, editable pixel correction, not an undocumented LUT descriptor.
        base = layers["color"].astype(np.float32) / 65535
        if include_blur:
            region = mask8.astype(np.float32) / 255 if per_layer and blur_masked else np.ones(shape, np.float32)
            base += (layers["blur"].astype(np.float32) / 65535 - base) * (region * blur_strength / 100)[..., None]
            children.insert(0, self._layer("Blur", layers["blur"], opacity=blur_strength,
                                           mask=mask8 if per_layer and blur_masked else None))
        if directional is not None:
            pixels, region, strength = directional
            _pixels(pixels, shape)
            _mask(region, shape)
            _opacity(strength)
            alpha = region.astype(np.float32) / 255 if per_layer else np.ones(shape, np.float32)
            base += (pixels.astype(np.float32) / 65535 - base) * (alpha * strength / 100)[..., None]
            children.insert(0, self._layer("Directional Blur", pixels, opacity=strength, mask=region if per_layer else None))
        if settings.get("on", True):
            hue, = ST.tables(dict(settings, hue_amount=100))
            corrected = ST.sample(np.ascontiguousarray(base[..., ::-1]), hue)[..., ::-1]
            children.insert(0, self._layer("Hue", FS.rgb16(corrected), opacity=settings["hue_amount"],
                                           mask=mask8 if per_layer else None))
        children.insert(0, self._layer("Detail", layers["detail"], "Linear Light"))
        bands = [] if bands is None else bands
        if bands:
            _mask(band_mask8, shape)
        elif band_mask8 is not None:
            raise ControlError("D&B mask requires enabled bands")
        children = self._bands(bands, shape, band_mask8 if per_layer else None, band_mix) + children
        return self._send([self._group(children, mask8 if not per_layer else None)], doc, source_icc)
