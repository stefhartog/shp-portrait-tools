"""Skin-tool layer/mask application and scratch-document cleanup."""

import struct
import os
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


def tagged_rgb16(layer, icc):
    h, w = layer.shape[:2]
    rgba = np.empty((h, w, 4), np.uint16)
    rgba[..., :3] = layer
    rgba[..., 3] = 65535
    ok, encoded = cv2.imencode(".png", rgba, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    del rgba
    if not ok:
        raise ControlError("could not encode the RGB16 layer")
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

    def _open(self, rel):
        return progress.call("Importing " + rel.split(f" {os.getpid()}-")[0],
                             super()._open, rel)

    def _write_rgb(self, rel, pixels, icc):
        def write():
            (self.exchange / rel).write_bytes(tagged_rgb16(pixels, icc))
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

    def send_mask(self, mask8, doc, *, source_icc=None):
        gray16 = mask8.astype(np.uint16) * 257
        rgb = np.repeat(gray16[..., None], 3, axis=2)
        del gray16
        return self._send_skin_layers([("Skin mask", rgb, "Normal", False)], doc,
                                      source_icc=source_icc)

    def send_frequency(self, layers, mask8, doc, source_icc):
        return self._send_skin_layers(
            [("Color", layers["color"], "Normal", False),
             ("Blur", layers["blur"], "Normal", True),
             ("Detail", layers["detail"], "Linear Light", False)],
            doc, mask8, source_icc)

    def send_skintone(self, layers, settings, mask8, doc, source_icc):
        """Assemble Color / masked Blur / masked Hue / Detail, then copy one group."""
        if mask8.ndim != 2 or mask8.dtype != np.uint8:
            raise ControlError("Skintone mask must be grayscale 8-bit")
        h, w = mask8.shape
        if set(layers) != {"color", "blur", "detail"} or any(
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
        try:
            icc = source_icc
            self._write_rgb(files[0], layers["color"], icc)
            scratch = self._open(files[0])
            scratch_docs.append(scratch)
            color_id = int(self.client.run("layer.list")["layers"][0]["id"])
            self.client.run("layer.setProps", layer=color_id, name="Color", blend="Normal")
            self._write_rgb(files[3], layers["blur"], icc)
            blur_doc = self._open(files[3])
            scratch_docs.append(blur_doc)
            blur_id = int(self.client.run("layer.list")["layers"][0]["id"])
            self.client.run("layer.setProps", layer=blur_id, name="Blur", blend="Normal")
            self._activate(scratch)
            self.client.run("layer.select", layer=color_id)
            blur_id = int(self._copy_layers(document=scratch, source=blur_doc,
                                         layers=[blur_id])["layers"][0])
            self._release_source(blur_doc, scratch_docs)
            self._activate(scratch)
            hue, = ST.tables(settings)
            result = progress.call("Creating native Hue adjustment", self.client.run, "layer.newAdjustmentLayer.colorLookup",
                                     data=ST.cube_text(hue, "Hue"), fileName="SHP Skintone Hue.cube",
                                     interpolation="trilinear", dither=False)
            correction = int(result["layer"])
            self.client.run("layer.layerMask.delete", layer=correction)
            self.client.run("layer.setProps", layer=correction, name="Hue", blend="Normal")
            if not mask8.any():
                for lid in (blur_id, correction):
                    progress.call("Attaching empty skin mask", self.client.run, "layer.layerMask.hideAll", layer=lid)
            else:
                if not progress.call("Encoding shared mask", cv2.imwrite, str(self.exchange / files[1]), mask8):
                    raise ControlError("could not write Skintone mask")
                mask_doc = self._open(files[1])
                scratch_docs.append(mask_doc)
                for lid in (blur_id, correction):
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
            self._write_rgb(files[2], layers["detail"], icc)
            detail_doc = self._open(files[2])
            scratch_docs.append(detail_doc)
            detail_id = int(self.client.run("layer.list")["layers"][0]["id"])
            self.client.run("layer.setProps", layer=detail_id, name="Detail", blend="Linear Light")
            self._activate(scratch)
            self.client.run("layer.select", layer=correction)
            detail_id = int(self._copy_layers(document=scratch, source=detail_doc,
                                           layers=[detail_id])["layers"][0])
            self._release_source(detail_doc, scratch_docs)
            self._activate(scratch)
            for i, lid in enumerate((color_id, blur_id, correction, detail_id)):
                self.client.run("layer.select", layer=lid, mode="replace" if i == 0 else "add")
            group = int(progress.call("Assembling FS + Skintone folder", self.client.run, "layer.groupLayers", name="SHP FS + Skintone", mode="Normal")["layer"])
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            tops = self.client.run("layer.list").get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            copied = progress.call("Inserting final FS + Skintone folder", self.client.run, "layer.copyToDocument", document=int(target["index"]),
                                     source=scratch, layers=[group])
            return {"layer": copied["layers"][0], "document": target["name"]}
        finally:
            self._cleanup_docs(scratch_docs)
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError:
                pass
            for rel in files:
                (self.exchange / rel).unlink(missing_ok=True)

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
