"""Skin Tools' editor boundary for the Photoshop UXP connector (numeric RGB, not converted)."""

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import io
import math
from pathlib import Path
import secrets
import time

import cv2
import numpy as np
from PIL import Image, ImageCms, UnidentifiedImageError

from shp_portrait_tools.shared.photocraft import ControlError
from shp_portrait_tools.shared.photoshop import CHUNK, MAX_PIXELS, MAX_TRANSFER, PROTOCOL
from . import frequency_separation as FS, skintone as ST, photoshop_lut, progress
from .photocraft_bridge import tagged_rgb16, transfer_settings

_layer_start = ContextVar("photoshop_layer_start", default=None)


@dataclass(frozen=True)
class RawCapture:
    name: str
    pixels: np.ndarray
    icc: bytes


_CONNECTOR_LABELS = {
    "capture": "Photoshop pixel capture",
    "serialize": "Photoshop pixel serialization",
    "readback": "Photoshop getData() readback",
    "pack": "Connector tile validation and RGB packing",
    "export": "Photoshop PNG copy export (encode and write)",
    "file_read": "Connector PNG read",
    "upload": "Connector upload and Tools receipt",
    "download": "Connector payload download",
    "stage": "Connector payload staging",
    "import": "Photoshop import and layer creation",
}


@contextmanager
def layer_preparation():
    token = _layer_start.set(time.perf_counter())
    try:
        yield
    finally:
        _layer_start.reset(token)


def _connector_details(result, wall, phases):
    timing = result.get("timing")
    if not isinstance(timing, dict):
        return []
    details = [{"label": _CONNECTOR_LABELS[key], "seconds": timing[key]} for key in phases
               if type(timing.get(key)) in (int, float) and 0 <= timing[key] <= wall]
    return details if sum(step["seconds"] for step in details) <= wall else []


def _raw_tile_details(result, wall, sizes):
    doc, tiles = result.get("doc"), result.get("pixel_tiles")
    if not isinstance(doc, dict) or not isinstance(tiles, list):
        return None
    w, h, depth = (doc.get(key) for key in ("width", "height", "depth"))
    if (type(w) is not int or type(h) is not int or type(depth) is not int
            or min(w, h) <= 0 or w * h > MAX_PIXELS or depth not in (8, 16)):
        return None
    stride = w * 3 * (depth // 8)
    rows = CHUNK // (w * 4 * (depth // 8))
    if not rows or len(tiles) != (h + rows - 1) // rows or sizes.get("pixels") != stride * h:
        return None
    for tile in tiles:
        if (not isinstance(tile, list) or len(tile) != 3 or type(tile[0]) is not int or tile[0] not in (3, 4)
                or not all(type(value) in (int, float) and 0 <= value <= wall for value in tile[1:])):
            return None
    readback, pack = (sum(tile[i] for tile in tiles) for i in (1, 2))
    if readback + pack > wall:
        return None
    return {"width": w, "height": h, "depth": depth, "rows": rows,
            "pixel_bytes": stride * h, "icc_bytes": sizes.get("icc", 0), "tiles": tiles,
            "readback": readback, "pack": pack}


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

    def fetch(self, inbox, *, capture_format="raw"):
        inbox = Path(inbox)
        if capture_format not in ("raw", "png"):
            raise ControlError("Invalid Photoshop capture format")
        if capture_format == "png" and "png-capture-v1" not in self.broker.capabilities:
            raise ControlError("PNG capture requires the updated Photoshop connector; install it and reconnect.")

        started = time.perf_counter()

        def finish(job, result):
            wall = time.perf_counter() - started
            names = ("pixels", "icc") if capture_format == "raw" else ("png",)
            sizes = {name: job.blob(name).stat().st_size for name in names if job.blob(name).is_file()}
            phases = ("capture", "serialize", "upload") if capture_format == "raw" else ("export", "file_read", "upload")
            details = _connector_details(result, wall, phases)
            tile_info = _raw_tile_details(result, wall, sizes) if capture_format == "raw" else None
            if tile_info:
                timing = result.get("timing")
                split = _connector_details({"timing": {**(timing if isinstance(timing, dict) else {}),
                                                       "readback": tile_info["readback"], "pack": tile_info["pack"]}},
                                           wall, ("capture", "readback", "pack", "upload"))
                if split:
                    details = split
                else:
                    tile_info = None
            progress.completed("Photoshop capture and transfer", wall, transfer="get",
                               bytes=sum(sizes.values()), details=details, **({"pixel_tiles": tile_info} if tile_info else {}))
            return prepare(job, result)

        def validate(job, result):
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
            if capture_format == "raw" and (not path.is_file() or path.stat().st_size != expected):
                raise ControlError("Photoshop composite byte count does not match the full canvas")
            if capture_format == "png":
                png = job.blob("png")
                png_size = result.get("png_bytes")
                limit = min(MAX_TRANSFER, w * h * 8 + 4 * 1024**2)
                if (type(png_size) is not int or not 0 < png_size <= limit or
                        not png.is_file() or png.stat().st_size != png_size):
                    raise ControlError("Photoshop PNG capture is empty, oversized or incomplete; choose raw capture.")
                try:
                    with png.open("rb") as stream:
                        header = stream.read(33)
                    if (len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n" or
                            header[12:16] != b"IHDR" or header[24] != depth or header[25] not in (2, 6)):
                        raise ValueError("PNG is not full-depth RGB or RGBA")
                    with Image.open(png) as image:
                        if image.format != "PNG" or image.size != (w, h) or "transparency" in image.info:
                            raise ValueError("PNG dimensions or transparency are unsupported")
                        if "icc_profile" not in image.info:
                            raise ValueError("PNG has no embedded ICC profile")
                        icc = image.info["icc_profile"]
                        if not isinstance(icc, bytes):
                            raise ValueError("PNG embedded ICC profile is corrupt or unreadable")
                        if not 128 <= len(icc) <= 4 * 1024**2:
                            raise ValueError("PNG embedded ICC profile has an invalid size")
                        image.verify()
                    bgr = cv2.imdecode(np.fromfile(png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                    if (bgr is None or bgr.dtype != (np.uint8 if depth == 8 else np.uint16) or
                            bgr.shape[:2] != (h, w) or bgr.ndim != 3 or bgr.shape[2] not in (3, 4)):
                        raise ValueError("PNG pixels are incomplete or have unexpected depth/channels")
                    if bgr.shape[2] == 4:
                        if not np.all(bgr[..., 3] == (255 if depth == 8 else 65535)):
                            raise ValueError("PNG composite has non-opaque pixels")
                        bgr = np.ascontiguousarray(bgr[..., :3])
                except (OSError, ValueError, TypeError, SyntaxError, IndexError, UnidentifiedImageError, cv2.error) as error:
                    raise ControlError(f"Photoshop PNG capture cannot be verified: {error}; choose raw capture.") from error
                if depth == 8:
                    bgr = bgr.astype(np.uint16) * 257
            else:
                if not profile_path.is_file() or not 128 <= profile_path.stat().st_size <= 4 * 1024**2:
                    raise ControlError("Select the source document's exact RGB ICC file in the connector")
                icc = profile_path.read_bytes()
                raw = np.fromfile(path, dtype="<u2" if depth == 16 else np.uint8).reshape(h, w, 3)
                bgr = np.ascontiguousarray(raw[..., ::-1])
                del raw
                if depth == 8:
                    bgr = bgr.astype(np.uint16) * 257
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                name = ImageCms.getProfileDescription(profile).strip().rstrip("\0")
                if profile.profile.xcolor_space.strip() != "RGB" or name != doc["profile"]:
                    raise ValueError("ICC description/colour space differs from the Photoshop document")
            except (OSError, ValueError, TypeError) as error:
                guidance = ("choose raw capture and select the exact matching ICC file"
                            if capture_format == "png" else "select the exact matching ICC file")
                raise ControlError(f"Cannot verify Photoshop ICC identity: {error}; {guidance}") from error
            doc = {**doc, "editor": "photoshop", "profile_hash": hashlib.sha256(icc).hexdigest()}
            return bgr, icc, doc

        def prepare(job, result):
            start = time.perf_counter()
            bgr, icc, doc = validate(job, result)
            progress.completed("Tools input validation and preparation", time.perf_counter() - start, transfer="get")
            stem = f"photoshop-{secrets.token_hex(8)}"
            if capture_format == "raw":
                return RawCapture(stem, bgr, icc), doc
            inbox.mkdir(parents=True, exist_ok=True)
            destination = inbox / f"{stem}.png"
            start = time.perf_counter()
            encoded = tagged_rgb16(bgr, icc, {"opaque_alpha": False}, png_strategy=cv2.IMWRITE_PNG_STRATEGY_RLE)
            progress.completed("Tools PNG encoding", time.perf_counter() - start, transfer="get", bytes=len(encoded))
            start = time.perf_counter()
            destination.write_bytes(encoded)
            progress.completed("Tools PNG disk write", time.perf_counter() - start, transfer="get", bytes=len(encoded))
            return destination, doc

        return self.broker.execute("capture", {"format": capture_format}, finish=finish)

    def _send(self, nodes, doc, source_icc):
        if (not isinstance(doc, dict) or doc.get("editor") != "photoshop"
                or doc.get("session") != self.broker.session):
            raise ControlError("Photoshop session changed; Get Image again")
        if not isinstance(source_icc, bytes) or hashlib.sha256(source_icc).hexdigest() != doc.get("profile_hash"):
            raise ControlError("The captured Photoshop ICC identity changed; Get Image again")
        h, w = doc["height"], doc["width"]
        start = _layer_start.get()
        if start is not None:
            progress.completed("Tools layer preparation (wall time)", time.perf_counter() - start, transfer="send")
            _layer_start.set(None)
        transfer_start = None
        payload_size = 0

        def prepare(job):
            nonlocal transfer_start, payload_size
            started = time.perf_counter()
            details = []
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
                nonlocal size
                result = {key: node[key] for key in ("name", "blend", "opacity")}
                if "children" in node:
                    result["children"] = [encode(child) for child in node["children"]]
                start = time.perf_counter()
                before = size
                if "lookup" in node:
                    name = f"b{len(blobs)}"
                    payload = node["lookup"]
                    size_in_bytes = len(payload)
                    if not 128 <= size_in_bytes <= CHUNK:
                        raise ControlError("Invalid Photoshop Hue lookup size")
                    size += size_in_bytes
                    if size > MAX_TRANSFER:
                        raise ControlError("Photoshop layer bundle exceeds the 2 GiB safety limit")
                    job.blob(name).write_bytes(payload)
                    blobs[name] = {"kind": "icc-lut", "bytes": size_in_bytes}
                    result["lookup"] = name
                elif "children" not in node:
                    result["pixels"] = binary(node["pixels"], 3)
                if node.get("mask") is not None:
                    result["mask"] = binary(node["mask"], 1)
                if size != before:
                    details.append({"label": node["name"], "seconds": time.perf_counter() - start, "bytes": size - before})
                return result

            job.params.update({"doc": doc, "layers": [encode(node) for node in nodes], "blobs": blobs})
            payload_size = size
            progress.completed("Tools layer payload encoding and staging (serial wall time)",
                               time.perf_counter() - started, transfer="send", bytes=size, details=details)
            transfer_start = time.perf_counter()

        result = self.broker.execute("apply", {}, prepare=prepare)
        if transfer_start is not None:
            elapsed = time.perf_counter() - transfer_start
            progress.completed("Photoshop transfer and import", elapsed, transfer="send", bytes=payload_size,
                               details=_connector_details(result, elapsed, ("download", "stage", "import")))
        return {key: value for key, value in result.items() if key != "timing"}

    @staticmethod
    def _layer(name, pixels, blend="Normal", opacity=100, mask=None):
        return {"name": name, "pixels": pixels, "blend": blend, "opacity": _opacity(opacity), "mask": mask}

    @staticmethod
    def _group(children, mask, blend="Normal"):
        return {"name": "SkinTools", "children": children, "blend": blend, "opacity": 100, "mask": mask}

    @staticmethod
    def _corrections(children, mask=None):
        return {"name": "Corrections", "children": children, "blend": "Pass Through", "opacity": 100, "mask": mask}

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
        per_layer = mask_placement == "layers"
        nodes = self._bands(layers, shape, mask8 if per_layer else None, band_mix)
        if not nodes:
            raise ControlError("Enable at least one D&B band")
        corrections = self._corrections(nodes, mask8 if not per_layer else None)
        return self._send([self._group([corrections], None, "Pass Through")], doc, source_icc)

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
                      bands=None, band_mask8=None, band_mix=100, mask_placement="folder", hue_output="native"):
        transfer_settings(transfer)
        shape = (doc["height"], doc["width"])
        _mask(mask8, shape)
        _opacity(blur_strength)
        if mask_placement not in ("folder", "layers") or type(blur_masked) is not bool:
            raise ControlError("Invalid Photoshop mask placement/Blur mask setting")
        if hue_output not in ("rendered", "native"):
            raise ControlError("Invalid Photoshop Hue output mode")
        if (hue_output == "native" and settings.get("on", True)
                and "hue-lut-device-link-v1" not in self.broker.capabilities):
            raise ControlError("Native Hue requires the updated Photoshop connector; install it and reconnect.")
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
        corrections = []
        render_hue = settings.get("on", True) and hue_output == "rendered"
        base = layers["color"].astype(np.float32) / 65535 if render_hue else None
        if include_blur:
            if render_hue:
                region = mask8.astype(np.float32) / 255 if per_layer and blur_masked else np.ones(shape, np.float32)
                base += (layers["blur"].astype(np.float32) / 65535 - base) * (region * blur_strength / 100)[..., None]
            corrections.insert(0, self._layer("Blur", layers["blur"], opacity=blur_strength,
                                              mask=mask8 if per_layer and blur_masked else None))
        if directional is not None:
            pixels, region, strength = directional
            _pixels(pixels, shape)
            _mask(region, shape)
            _opacity(strength)
            if render_hue:
                alpha = region.astype(np.float32) / 255 if per_layer else np.ones(shape, np.float32)
                base += (pixels.astype(np.float32) / 65535 - base) * (alpha * strength / 100)[..., None]
            corrections.insert(0, self._layer("Directional Blur", pixels, opacity=strength,
                                              mask=region if per_layer else None))
        if settings.get("on", True):
            hue, = ST.tables(dict(settings, hue_amount=100))
            if render_hue:
                corrected = ST.sample(np.ascontiguousarray(base[..., ::-1]), hue)[..., ::-1]
                corrections.insert(0, self._layer("Hue", FS.rgb16(corrected), opacity=settings["hue_amount"],
                                                  mask=mask8 if per_layer else None))
            else:
                corrections.insert(0, {"name": "Hue", "blend": "Normal", "opacity": _opacity(settings["hue_amount"]),
                                        "lookup": photoshop_lut.device_link(hue), "mask": mask8 if per_layer else None})
        bands = [] if bands is None else bands
        if bands:
            _mask(band_mask8, shape)
        elif band_mask8 is not None:
            raise ControlError("D&B mask requires enabled bands")
        corrections = self._bands(bands, shape, band_mask8 if per_layer else None, band_mix) + corrections
        children = [self._layer("Detail", layers["detail"], "Linear Light")]
        if corrections:
            children.append(self._corrections(corrections, mask8 if not per_layer else None))
        children.append(self._layer("Color", layers["color"]))
        return self._send([self._group(children, None)], doc, source_icc)
