"""Skin-tool layer/mask application and scratch-document cleanup."""

import struct
import os
import math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import tempfile
import time
import zlib

import cv2
import numpy as np
from PIL import Image

from shp_photocraft_tools.shared.photocraft import Bridge as BaseBridge, Client, ControlError
from shp_photocraft_tools.shared.photocraft import detect_config as _detect_config
from . import skintone as ST
from . import progress

MASK_ATTEMPTS = 4


def transfer_settings(value=None):
    defaults = {"format": "png", "compressed": True, "opaque_alpha": True, "parallel": True}
    if value is None:
        return defaults
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("FS transfer settings must be an object with format, compressed, opaque_alpha and parallel")
    settings = {**defaults, **value}
    if settings["format"] not in ("png", "tiff"):
        raise ValueError("FS transfer format must be png or tiff")
    if any(type(settings[key]) is not bool for key in ("compressed", "opaque_alpha", "parallel")):
        raise ValueError("FS transfer compression, opaque alpha and parallel switches must be booleans")
    return settings


def _tag_tiff(data, icc, alpha):
    # Retain encoded samples/strip offsets; append an ICC-tagged replacement IFD.
    order = "<" if data[:2] == b"II" else ">" if data[:2] == b"MM" else None
    if order is None or struct.unpack_from(order + "H", data, 2)[0] != 42:
        raise ControlError("layer encoder did not produce a classic TIFF")
    offset = struct.unpack_from(order + "I", data, 4)[0]
    count = struct.unpack_from(order + "H", data, offset)[0]
    end = offset + 2 + count * 12
    if end + 4 > len(data) or struct.unpack_from(order + "I", data, end)[0] != 0:
        raise ControlError("layer encoder produced an invalid or multi-page TIFF")
    entries = {struct.unpack_from(order + "H", data, i)[0]: data[i:i+12]
               for i in range(offset + 2, end, 12)}
    profile_offset = len(data) + len(data) % 2
    if profile_offset >= 2**32 or len(icc) >= 2**32:
        raise ControlError("FS transfer exceeds classic TIFF's 4 GiB limit; use PNG")
    entries[34675] = struct.pack(order + "HHII", 34675, 7, len(icc), profile_offset)
    if alpha:
        entries[338] = struct.pack(order + "HHI", 338, 3, 1) + struct.pack(order + "HH", 2, 0)
    ifd_offset = profile_offset + len(icc) + len(icc) % 2
    if ifd_offset + 2 + len(entries)*12 + 4 >= 2**32:
        raise ControlError("FS transfer exceeds classic TIFF's 4 GiB limit; use PNG")
    ifd = struct.pack(order + "H", len(entries)) + b"".join(entries[k] for k in sorted(entries)) + b"\0"*4
    return b"".join((data[:4], struct.pack(order + "I", ifd_offset), data[8:],
                     b"\0"*(profile_offset-len(data)), icc, b"\0"*(len(icc)%2), ifd))


def tagged_rgb16(layer, icc, transfer=None):
    options = transfer_settings(transfer)
    if layer.dtype != np.uint16 or layer.ndim != 3 or layer.shape[2] != 3 or not icc:
        raise ControlError("layer encoding requires RGB16 pixels and the captured ICC profile")
    h, w = layer.shape[:2]
    pixels = layer
    if options["opaque_alpha"]:
        pixels = np.empty((h, w, 4), np.uint16)
        pixels[..., :3] = layer
        pixels[..., 3] = 65535
    if options["format"] == "tiff":
        ok, encoded = cv2.imencode(".tiff", pixels,
                                  [cv2.IMWRITE_TIFF_COMPRESSION, 8 if options["compressed"] else 1])
    else:
        ok, encoded = cv2.imencode(".png", pixels,
                                  [cv2.IMWRITE_PNG_COMPRESSION, 1 if options["compressed"] else 0])
    del pixels
    if not ok:
        raise ControlError("could not encode the RGB16 layer")
    if options["format"] == "tiff":
        return _tag_tiff(encoded.tobytes(), icc, options["opaque_alpha"])
    # Pillow cannot write RGB16. Inserting only iCCP leaves the samples untouched.
    png = memoryview(encoded).cast("B")
    chunk = b"iCCP" + b"Destination\0\0" + zlib.compress(icc)
    return b"".join((png[:33], struct.pack(">I", len(chunk) - 4), chunk,
                     struct.pack(">I", zlib.crc32(chunk)), png[33:]))


def detect_config():
    return _detect_config("DNB", "dnb")


class Bridge(BaseBridge):
    env_prefix = "DNB"
    module = "dnb"
    app_name = "SHP PhotoCraft D&B"

    def test_native_frequency(self, doc, source_path, method, radius, *, source_pixels=None):
        """Build only native Color/Detail on a verified merged snapshot."""
        if method not in ("gaussian", "median"):
            raise ControlError("Test Native FS supports Gaussian or Median only; Median Fast is a Tools-only method")
        if not math.isfinite(radius) or not 1 <= radius <= 100:
            raise ControlError("native FS test radius must be 1-100 full-image pixels")
        if method == "median" and radius != round(radius):
            raise ControlError("native Median needs an integer radius; choose an integer FS radius")
        target = self.active_doc()
        if target is None or (target.get("index"), target.get("name")) != (doc["index"], doc["name"]):
            raise ControlError("activate the document used by Get Image before the native FS test")
        original = cv2.imread(str(source_path), cv2.IMREAD_UNCHANGED)
        if original is None or original.dtype != np.uint16 or original.ndim != 3 or original.shape[2] not in (3, 4):
            raise ControlError("native FS test requires a fetched RGB16 image; Get an RGB16 document first")
        if original.shape[2] == 4 and not np.all(original[..., 3] == 65535):
            raise ControlError("native FS test requires an opaque merged image")
        if source_pixels is not None:
            if source_pixels.shape != original[..., :3].shape:
                raise ControlError("the fetched source file changed; Get Image again")
            for y in range(0, original.shape[0], 64):
                if not np.array_equal(original[y:y + 64, :, :3].astype(np.float32) / np.float32(65535),
                                      source_pixels[y:y + 64]):
                    raise ControlError("the fetched source file changed; Get Image again")
        with Image.open(source_path) as image:
            icc = image.info.get("icc_profile")
        if not icc:
            raise ControlError("native FS test requires the ICC profile captured by Get Image")
        before = self.client.run("document.inspect")
        if before.get("mode") != "Rgb" or before.get("depth") != 16:
            raise ControlError("native FS test requires an RGB16 destination; it never converts document mode or depth")
        if not isinstance(before.get("revision"), int):
            raise ControlError("PhotoCraft cannot report the destination revision required by the native FS test")
        channels = before.get("channels") or {}
        channel = channels.get("target") or {}
        kind = channel.get("kind")
        if kind in ("composite", "layerMask", "vectorMask"):
            restore_channel = {"channel": {"composite": "composite", "layerMask": "mask",
                                          "vectorMask": "vectorMask"}[kind]}
        elif kind == "color":
            restore_channel = ({"colors": channels["selectedColors"]} if channels.get("selectedColors")
                               else {"channel": {"color": channel["index"]}})
        elif kind == "alpha":
            restore_channel = {"channel": {"alpha": channel["index"]}}
        else:
            raise ControlError("PhotoCraft cannot report the channel target required by the native FS test")
        if channels.get("layerMaskView") is not None or channels.get("quickMask") is not None:
            raise ControlError("leave Quick Mask and isolated layer-mask view before the native FS test")
        h, w = original.shape[:2]
        if (before.get("width"), before.get("height")) != (w, h):
            raise ControlError("document size changed; Get Image again before the native FS test")
        filter_id = "filter.blur.gaussianBlur" if method == "gaussian" else "filter.noise.median"
        required = {filter_id, "image.duplicate", "layer.list", "layer.duplicate", "layer.select",
                    "layer.setProps", "layer.arrange.bringToFront", "image.applyImage",
                    "layer.groupLayers", "layer.copyToDocument", "channel.target.composite",
                    "select.deselect", "layer.moveTo", "layer.delete", "file.close", "document.inspect",
                    "channel.target", "channel.setVisible"}
        catalog = self.client.call("engine.commands")
        if not isinstance(catalog, list):
            raise ControlError("PhotoCraft returned an invalid command catalog")
        missing = required - {item["id"] for item in catalog}
        if missing:
            raise ControlError("PhotoCraft lacks native FS commands: " + ", ".join(sorted(missing)))

        def fingerprint(info):
            return {key: info.get(key) for key in (
                "id", "revision", "mode", "depth", "width", "height", "history", "activeLayer",
                "layers", "channels", "hasSelection")}

        def selected_layers(layers):
            return [int(layer["id"]) for layer in layers if layer.get("selected")] + [
                lid for layer in layers for lid in selected_layers(layer.get("children") or [])]

        selection = selected_layers(before.get("layers") or [])
        if not selection and before.get("activeLayer") is not None:
            selection = [int(before["activeLayer"])]
        rel = self._file("native FS verification")
        scratch = inserted = None
        run = getattr(self.client, "run_once", self.client.run)
        scratch_name = f"SHP native FS test {os.getpid()}-{next(self._names)}"
        insertion_attempted = False
        timings = {}
        cleanup_errors = []
        primary = None
        insertion_complete = False
        started = time.perf_counter()

        def call(label, fn, *args, **kwargs):
            start = time.perf_counter()
            result = progress.call(label, fn, *args, **kwargs)
            timings[label] = time.perf_counter() - start
            return result

        try:
            call("Making native merged snapshot", run, "image.duplicate",
                 name=scratch_name, mergedOnly=True)
            scratch = int(self.session()["active"])
            if scratch == target["index"]:
                raise ControlError("PhotoCraft did not create a separate native FS snapshot")
            info = run("document.inspect")
            if info.get("hasSelection"):
                run("select.deselect")
            run("channel.target.composite")
            layers = run("layer.list").get("layers") or []
            if len(layers) != 1 or layers[0].get("kind") != "Pixel":
                raise ControlError("PhotoCraft did not create one merged raster source")
            color = int(layers[0]["id"])
            call("Verifying fetched source", self._save, rel, scratch)
            exported = cv2.imread(str(self.exchange / rel), cv2.IMREAD_UNCHANGED)
            with Image.open(self.exchange / rel) as image:
                exported_icc = image.info.get("icc_profile")
            if (exported is None or exported.dtype != original.dtype
                    or exported.shape != original.shape or not np.array_equal(exported, original)
                    or exported_icc != icc):
                raise ControlError("the current merged pixels or ICC profile differ from Get Image; Get Image again")
            del exported, original
            run("layer.setProps", layer=color, name="Color", blend="Normal",
                            opacity=1, fill=1, clipped=False)
            copied = run("layer.duplicate", layer=color, name="Detail")
            detail_ids = copied.get("layers") or ([copied["layer"]] if "layer" in copied else [])
            if len(detail_ids) != 1:
                raise ControlError("PhotoCraft did not return the native Detail layer ID")
            detail = int(detail_ids[0])
            run("layer.select", layer=color)
            call("Native " + method.title() + " Color", run, filter_id,
                 radius=radius, target="pixels")
            run("layer.select", layer=detail)
            call("Native Apply Image Detail", run, "image.applyImage",
                 source={"document": scratch, "layer": color, "channel": "composite", "invert": True},
                 blending="add", scale=2, offset=0, opacity=100,
                 preserveTransparency=False, target="pixels")
            run("layer.setProps", layer=detail, blend="Linear Light",
                            opacity=1, fill=1, clipped=False)
            run("layer.arrange.bringToFront", layer=detail)
            run("layer.select", layer=color)
            run("layer.select", layer=detail, mode="add")
            group = int(run("layer.groupLayers",
                                       name=f"SHP Native FS Test ({method.title()}, r={radius:g})",
                                       mode="Normal")["layer"])
            self._activate(target["index"])
            if fingerprint(run("document.inspect")) != fingerprint(before):
                raise ControlError("the destination changed during native FS preparation; no folder was inserted")
            insertion_attempted = True
            result = call("Inserting native FS test folder", run, "layer.copyToDocument",
                          document=target["index"], source=scratch, layers=[group])
            inserted_ids = result.get("layers") or []
            if len(inserted_ids) != 1:
                raise ControlError("PhotoCraft did not return the inserted test folder ID; inspect the destination")
            inserted = int(inserted_ids[0])
            tops = before.get("layers") or []
            if tops:
                run("layer.moveTo", layer=inserted, target=int(tops[0]["id"]), position="above")
            insertion_complete = True
        except Exception as error:
            primary = error
            if insertion_attempted and inserted is None:
                primary = ControlError("native FS insertion may have executed; inspect the destination before retrying: " + str(error))
                raise primary from error
            raise
        finally:
            if scratch is None:
                try:
                    scratch = next((int(item["index"]) for item in self.session()["documents"]
                                    if item.get("name") == scratch_name), None)
                except ControlError as error:
                    cleanup_errors.append("could not locate native FS snapshot: " + str(error))
            if inserted is not None and not insertion_complete:
                try:
                    self._activate(target["index"])
                    run("layer.delete", layer=inserted)
                except ControlError as error:
                    cleanup_errors.append("could not remove incomplete test folder: " + str(error))
            if scratch is not None and scratch != target["index"]:
                try:
                    run("file.close", document=scratch)
                except ControlError as error:
                    cleanup_errors.append("could not close native FS snapshot: " + str(error))
            try:
                current = self._find(target["index"], target["name"])
                if current["index"] != target["index"]:
                    raise ControlError("destination index changed")
                self._activate(target["index"])
                if insertion_attempted:
                    for index, lid in enumerate(selection):
                        run("layer.select", layer=lid, mode="replace" if index == 0 else "add")
                    if len(selection) > 1 and before.get("activeLayer") is not None:
                        run("layer.select", layer=int(before["activeLayer"]), mode="add")
                    run("channel.target", **restore_channel)
                    restored = run("document.inspect").get("channels") or {}
                    for index, row in enumerate(channels.get("colors") or []):
                        actual = next((item for i, item in enumerate(restored.get("colors") or []) if i == index), None)
                        if actual is None or actual["visible"] != row["visible"]:
                            run("channel.setVisible", channel={"color": index}, visible=row["visible"])
                    for row in channels.get("alpha") or []:
                        actual = next((item for item in restored.get("alpha") or [] if item["index"] == row["index"]), None)
                        if actual is None or actual["visible"] != row["visible"]:
                            run("channel.setVisible", channel={"alpha": row["index"]}, visible=row["visible"])
                    if (run("document.inspect").get("channels") or {}) != channels:
                        raise ControlError("PhotoCraft did not restore the original channel target/view")
            except ControlError as error:
                cleanup_errors.append("could not restore destination state: " + str(error))
            try:
                (self.exchange / rel).unlink(missing_ok=True)
            except OSError as error:
                cleanup_errors.append("could not remove verification export: " + str(error))
            if cleanup_errors:
                prefix = str(primary) if primary else "native FS folder inserted" if insertion_complete else "native FS test failed"
                raise ControlError(prefix + "; " + "; ".join(cleanup_errors)) from primary
        return {"sent": True, "layer": inserted, "document": target["name"],
                "method": method, "radius": radius, "timings": timings,
                "seconds": time.perf_counter() - started,
                "note": "Native Color/Detail only; no Blur, Hue or masks. Native filtering differs from Tools. "
                        "Folder insertion/reordering may take multiple undo steps."}

    def _open(self, rel):
        return progress.call("Importing " + rel.split(f" {os.getpid()}-")[0],
                             super()._open, rel)

    def _write_rgb(self, rel, pixels, icc, transfer=None):
        def write():
            (self.exchange / rel).write_bytes(tagged_rgb16(pixels, icc, transfer))
        return progress.call("Encoding " + rel.split(f" {os.getpid()}-")[0], write)

    def _copy_layers(self, **params):
        return progress.call("Copying layers to document", self.client.run, "layer.copyToDocument", **params)

    def _attach_mask(self, scratch, mask_doc, layer, label):
        def attach():
            self._activate(scratch)
            self.client.run("select.loadSelection", channel="composite", document=mask_doc)
            self.client.run("layer.layerMask.revealSelection", layer=layer)
        progress.call(label, attach)

    def _cleanup_docs(self, scratch_docs):
        def cleanup():
            for index in sorted(scratch_docs, reverse=True):
                self._close(index)
        progress.call("Cleaning up scratch documents", cleanup)

    def _release_source(self, index, scratch_docs):
        # Sources open after the assembly. Closing the highest managed index cannot
        # shift the assembly or target; the final cleanup also closes highest first.
        if index != max(scratch_docs) or len(scratch_docs) < 2:
            raise ControlError("cannot close a source before its dependent scratch documents")
        progress.call("Releasing temporary source document", self.client.run, "file.close", document=index)
        scratch_docs.remove(index)

    def _target_profile(self, target):
        # A correction is blend data, not a colour image: copying between different
        # profiles would colour-convert its samples and move Soft Light's neutral.
        self._activate(target["index"])
        with tempfile.TemporaryDirectory(prefix="dnb-profile-", dir=self.exchange) as tmp:
            exported, _ = self.fetch(tmp)
            with Image.open(exported) as image:
                icc = image.info.get("icc_profile")
            if not icc:
                raise ControlError("PhotoCraft did not export the destination ICC profile; "
                                   "cannot preserve numeric layer values")
            return icc

    def send(self, layer16, mask8, doc, name="SHP PhotoCraft D&B soft light", *, source_icc=None):
        """Copy the 16-bit RGB layer with its mask and blend mode into the target in one step."""
        h, w = mask8.shape
        if layer16.shape[:2] != (h, w):
            raise ControlError("layer and mask sizes differ")
        d = self._find(doc["index"], doc["name"])
        if (d.get("width"), d.get("height")) not in ((w, h), (None, None)):
            raise ControlError(f"the document is {d.get('width')}x{d.get('height')} but the layer is {w}x{h}: "
                               "fetch the picture again")
        rel_l, rel_m = self._file("layer"), self._file("mask")
        scratch_docs = []
        try:
            # Get's exact bytes preserve numeric blend data without another merged
            # export. The caller must Get again after changing the destination.
            icc = source_icc or self._target_profile(d)
            self._write_rgb(rel_l, layer16, icc)
            if not progress.call("Encoding shared mask", cv2.imwrite, str(self.exchange / rel_m), mask8, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                raise ControlError("could not write the Soft Light mask")
            scratch = self._open(rel_l)
            scratch_docs.append(scratch)
            layers = (self.client.run("layer.list") or {}).get("layers") or []
            if not layers:
                raise ControlError("PhotoCraft opened an empty document")
            lid = int(layers[0]["id"])
            mask_doc = self._open(rel_m)
            scratch_docs.append(mask_doc)
            for attempt in range(MASK_ATTEMPTS):  # a click in PhotoCraft can clear the selection in between
                try:
                    self._attach_mask(scratch, mask_doc, lid, "Attaching Soft Light mask")
                    break
                except ControlError:
                    if attempt == MASK_ATTEMPTS - 1:
                        raise
                    time.sleep(0.2)
            self._release_source(mask_doc, scratch_docs)
            self._activate(scratch)
            self.client.run("layer.setProps", layer=lid, blend="Soft Light", name=name)
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            tops = (self.client.run("layer.list") or {}).get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            copied = progress.call("Inserting final D&B layer", self.client.run, "layer.copyToDocument", document=int(target["index"]), source=scratch, layers=[lid])
            ids = (copied or {}).get("layers") or []
            return {"layer": ids[0] if ids else None, "document": target.get("name")}
        finally:
            self._cleanup_docs(scratch_docs)
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError:
                pass
            for rel in (rel_l, rel_m):
                try:
                    (self.exchange / rel).unlink()
                except OSError:
                    pass

    def send_mask(self, mask8, doc, *, source_icc=None, name="Skin mask"):
        gray16 = mask8.astype(np.uint16) * 257
        rgb = np.repeat(gray16[..., None], 3, axis=2)
        del gray16
        return self._send_skin_layers([(name, rgb, "Normal", False)], doc,
                                      source_icc=source_icc)

    def send_frequency(self, layers, mask8, doc, source_icc):
        return self._send_skin_layers(
            [("Color", layers["color"], "Normal", False),
             ("Blur", layers["blur"], "Normal", True),
             ("Detail", layers["detail"], "Linear Light", False)],
            doc, mask8, source_icc)

    def send_skintone(self, layers, settings, mask8, doc, source_icc, *, blur_future=None, transfer=None, include_blur=True,
                      blur_strength=100, blur_masked=True):
        """Assemble Color / masked Blur / masked Hue / Detail, then copy one group."""
        options = transfer_settings(transfer)
        if (not isinstance(blur_strength, (float, int)) or isinstance(blur_strength, bool)
                or not math.isfinite(blur_strength) or not 0 <= blur_strength <= 100
                or type(blur_masked) is not bool):
            raise ControlError("Blur needs 0-100 percent strength and a boolean skin-mask setting")
        if mask8.ndim != 2 or mask8.dtype != np.uint8:
            raise ControlError("Skintone mask must be grayscale 8-bit")
        h, w = mask8.shape
        if not include_blur and blur_future is not None:
            raise ControlError("Disabled FS Blur must not have a Blur job")
        expected = {"color", "detail"} if blur_future is not None or not include_blur else {"color", "blur", "detail"}
        if set(layers) != expected or any(
                a.shape != (h, w, 3) or a.dtype != np.uint16 for a in layers.values()):
            raise ControlError("FS + Skintone needs matching RGB16 Color, Blur and Detail layers")
        target = self._find(doc["index"], doc["name"])
        if (target.get("width"), target.get("height")) != (w, h):
            raise ControlError("the document size changed: fetch the picture again")
        if not source_icc:
            raise ControlError("no ICC profile captured: fetch the picture again before Skintone")
        scratch_docs = []
        files = [self._file("Skintone Color"), self._file("Skintone mask"),
                 self._file("Skintone Detail"), self._file("Skintone Blur")]
        if options["format"] == "tiff":
            for i in (0, 2, 3):
                files[i] = str(Path(files[i]).with_suffix(".tiff"))
        primary = None
        insertion_attempted = False
        inserted = False
        encoder = None
        detail_job = None
        try:
            icc = source_icc
            progress.call("FS transfer: " + options["format"].upper() +
                          (" lossless compressed" if options["compressed"] else "uncompressed") +
                          (", opaque alpha" if options["opaque_alpha"] else ", RGB without alpha") +
                          (", parallel encoding" if options["parallel"] else ", serial encoding"), lambda: None)
            if options["parallel"] and (blur_future is not None or transfer is not None):
                encoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skin-fs-encode")
                detail_job = encoder.submit(copy_context().run, self._write_rgb,
                                            files[2], layers["detail"], icc, options)
            self._write_rgb(files[0], layers["color"], icc, options)
            scratch = self._open(files[0])
            scratch_docs.append(scratch)
            color_id = int(self.client.run("layer.list")["layers"][0]["id"])
            self.client.run("layer.setProps", layer=color_id, name="Color", blend="Normal")
            blur_id = None
            if include_blur:
                if blur_future is not None:
                    blur = blur_future.result()
                    if blur.shape != (h, w, 3) or blur.dtype != np.uint16:
                        raise ControlError("FS + Skintone needs matching RGB16 Blur")
                else:
                    blur = layers["blur"]
                self._write_rgb(files[3], blur, icc, options)
                del blur
                blur_doc = self._open(files[3])
                scratch_docs.append(blur_doc)
                blur_id = int(self.client.run("layer.list")["layers"][0]["id"])
                self.client.run("layer.setProps", layer=blur_id, name="Blur", blend="Normal")
                self._activate(scratch)
                self.client.run("layer.select", layer=color_id)
                blur_id = int(self._copy_layers(document=scratch, source=blur_doc,
                                             layers=[blur_id])["layers"][0])
                self.client.run("layer.setProps", layer=blur_id, name="Blur", blend="Normal", opacity=blur_strength/100)
                self._release_source(blur_doc, scratch_docs)
                self._activate(scratch)
            correction = None
            if settings.get("on", True):
                hue, = ST.tables(dict(settings, hue_amount=100))
                result = progress.call("Creating native Hue adjustment", self.client.run, "layer.newAdjustmentLayer.colorLookup",
                                         data=ST.cube_text(hue, "Hue"), fileName="SHP Skintone Hue.cube",
                                         interpolation="trilinear", dither=False)
                correction = int(result["layer"])
                self.client.run("layer.layerMask.delete", layer=correction)
                self.client.run("layer.setProps", layer=correction, name="Hue", blend="Normal", opacity=settings["hue_amount"]/100)
            masked_layers = tuple(lid for lid in (blur_id if blur_masked else None, correction) if lid is not None)
            if not mask8.any():
                for lid in masked_layers:
                    progress.call("Attaching empty skin mask", self.client.run, "layer.layerMask.hideAll", layer=lid)
            elif masked_layers:
                if not progress.call("Encoding shared mask", cv2.imwrite, str(self.exchange / files[1]), mask8):
                    raise ControlError("could not write Skintone mask")
                mask_doc = self._open(files[1])
                scratch_docs.append(mask_doc)
                for lid in masked_layers:
                    for attempt in range(MASK_ATTEMPTS):
                        try:
                            self._attach_mask(scratch, mask_doc, lid,
                                              "Attaching Blur mask" if lid == blur_id else "Attaching Hue mask")
                            break
                        except ControlError:
                            if attempt == MASK_ATTEMPTS - 1:
                                raise
                            time.sleep(0.2)
                self._release_source(mask_doc, scratch_docs)
                self._activate(scratch)
            if detail_job is None:
                self._write_rgb(files[2], layers["detail"], icc, options)
            else:
                detail_job.result()
            detail_doc = self._open(files[2])
            scratch_docs.append(detail_doc)
            detail_id = int(self.client.run("layer.list")["layers"][0]["id"])
            self.client.run("layer.setProps", layer=detail_id, name="Detail", blend="Linear Light")
            self._activate(scratch)
            self.client.run("layer.select", layer=correction if correction is not None else blur_id if blur_id is not None else color_id)
            detail_id = int(self._copy_layers(document=scratch, source=detail_doc,
                                           layers=[detail_id])["layers"][0])
            self._release_source(detail_doc, scratch_docs)
            self._activate(scratch)
            ordered_layers = tuple(lid for lid in (color_id, blur_id, correction, detail_id) if lid is not None)
            for i, lid in enumerate(ordered_layers):
                self.client.run("layer.select", layer=lid, mode="replace" if i == 0 else "add")
            group = int(progress.call("Assembling FS + Skintone folder", self.client.run, "layer.groupLayers", name="SHP FS + Skintone", mode="Normal")["layer"])
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            tops = self.client.run("layer.list").get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            insertion_attempted = True
            copied = progress.call("Inserting final FS + Skintone folder",
                                     getattr(self.client, "run_once", self.client.run),
                                     "layer.copyToDocument", document=int(target["index"]),
                                     source=scratch, layers=[group])
            if not isinstance(copied, dict) or len(copied.get("layers") or []) != 1:
                raise ControlError("PhotoCraft did not return the inserted FS folder ID; inspect the destination")
            inserted = True
            return {"layer": copied["layers"][0], "document": target["name"]}
        except Exception as error:
            primary = error
            if insertion_attempted and not inserted:
                primary = ControlError("FS + Skintone insertion failed or lost its reply; inspect the destination before retrying: " + str(error))
                raise primary from error
            raise
        finally:
            cleanup_errors = []
            if encoder is not None:
                encoder.shutdown(wait=True, cancel_futures=True)
                if detail_job is not None and not detail_job.cancelled():
                    try:
                        detail_job.result()
                    except Exception as error:
                        if error is not primary:
                            cleanup_errors.append("Detail encoding failed: " + str(error))
            for index in sorted(scratch_docs, reverse=True):
                try:
                    self.client.run("file.close", document=index)
                except ControlError as error:
                    cleanup_errors.append("could not close FS scratch document: " + str(error))
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError as error:
                cleanup_errors.append("could not restore FS destination: " + str(error))
            for rel in files:
                try:
                    (self.exchange / rel).unlink(missing_ok=True)
                except OSError as error:
                    cleanup_errors.append("could not remove FS transfer file: " + str(error))
            if cleanup_errors:
                prefix = str(primary) if primary else "FS + Skintone folder inserted" if inserted else "FS + Skintone send failed"
                raise ControlError(prefix + "; " + "; ".join(cleanup_errors)) from primary

    def _send_skin_layers(self, layers, doc, blur_mask=None, source_icc=None):
        """Assemble off-document, then copy one layer/group in one target history step."""
        h, w = layers[0][1].shape[:2]
        if any(a.dtype != np.uint16 or a.shape != (h, w, 3) for _, a, _, _ in layers):
            raise ControlError("skin layers must be matching RGB16 images")
        if blur_mask is not None and (blur_mask.shape != (h, w) or blur_mask.dtype != np.uint8):
            raise ControlError("layer and mask sizes differ")
        target = self._find(doc["index"], doc["name"])
        if (target.get("width"), target.get("height")) not in ((w, h), (None, None)):
            raise ControlError("the document size changed: fetch the picture again")
        if blur_mask is not None and not source_icc:
            raise ControlError("no ICC profile captured: fetch the picture again before separating")
        scratch_docs, files = [], []
        try:
            icc = source_icc or self._target_profile(target)
            assembly = None
            ids = []
            blur_id = None
            for name, pixels, blend, masked in layers:
                rel = self._file(name)
                files.append(rel)
                self._write_rgb(rel, pixels, icc)
                scratch = self._open(rel)
                scratch_docs.append(scratch)
                lid = int(self.client.run("layer.list")["layers"][0]["id"])
                self.client.run("layer.setProps", layer=lid, name=name, blend=blend)
                if assembly is None:
                    assembly = scratch
                else:
                    self._activate(assembly)
                    self.client.run("layer.select", layer=ids[-1])
                    result = self.client.run("layer.copyToDocument", document=assembly, source=scratch, layers=[lid])
                    lid = int(result["layers"][0])
                    self._release_source(scratch, scratch_docs)
                    self._activate(assembly)
                ids.append(lid)
                if masked:
                    blur_id = lid
            self._activate(assembly)
            if blur_mask is not None:
                for i, lid in enumerate(ids):
                    self.client.run("layer.select", layer=lid, mode="replace" if i == 0 else "add")
                lid = int(self.client.run("layer.groupLayers", name="SHP Frequency Separation", mode="Normal")["layer"])
                rel = self._file("skin mask")
                files.append(rel)
                if not cv2.imwrite(str(self.exchange / rel), blur_mask):
                    raise ControlError("could not write the skin mask")
                mask_doc = self._open(rel)
                scratch_docs.append(mask_doc)
                for attempt in range(MASK_ATTEMPTS):
                    try:
                        self._activate(assembly)
                        self.client.run("select.loadSelection", channel="composite", document=mask_doc)
                        self.client.run("layer.layerMask.revealSelection", layer=blur_id)
                        break
                    except ControlError:
                        if attempt == MASK_ATTEMPTS - 1:
                            raise
                        time.sleep(0.2)
                self._release_source(mask_doc, scratch_docs)
                self._activate(assembly)
            else:
                lid = ids[0]
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            tops = self.client.run("layer.list").get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            copied = progress.call("Inserting final skin layers", self.client.run, "layer.copyToDocument", document=int(target["index"]),
                                     source=assembly, layers=[lid])
            return {"layer": copied["layers"][0], "document": target["name"]}
        finally:
            self._cleanup_docs(scratch_docs)
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError:
                pass
            for rel in files:
                (self.exchange / rel).unlink(missing_ok=True)
