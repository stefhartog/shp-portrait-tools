"""Skin-tool layer/mask application and scratch-document cleanup."""

import struct
import tempfile
import time
import zlib

import cv2
import numpy as np
from PIL import Image

from shp_photocraft_tools.shared.photocraft import Bridge as BaseBridge, Client, ControlError
from shp_photocraft_tools.shared.photocraft import detect_config as _detect_config

MASK_ATTEMPTS = 4


def tagged_rgb16(layer, icc):
    h, w = layer.shape[:2]
    rgba = np.dstack([layer, np.full((h, w), 65535, np.uint16)])
    ok, encoded = cv2.imencode(".png", rgba, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise ControlError("could not encode the RGB16 layer")
    # Pillow cannot write RGB16. Inserting only iCCP leaves the samples untouched.
    png = encoded.tobytes()
    chunk = b"iCCP" + b"Destination\0\0" + zlib.compress(icc)
    return (png[:33] + struct.pack(">I", len(chunk) - 4) + chunk
            + struct.pack(">I", zlib.crc32(chunk)) + png[33:])


def detect_config():
    return _detect_config("DNB", "dnb")


class Bridge(BaseBridge):
    env_prefix = "DNB"
    module = "dnb"
    app_name = "SHP PhotoCraft D&B"

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

    def send(self, layer16, mask8, doc, name="SHP PhotoCraft D&B soft light"):
        """Copy the 16-bit RGB layer with its mask and blend mode into the target in one step."""
        h, w = mask8.shape
        if layer16.shape[:2] != (h, w):
            raise ControlError("layer and mask sizes differ")
        d = self._find(doc["index"], doc["name"])
        if (d.get("width"), d.get("height")) not in ((w, h), (None, None)):
            raise ControlError(f"the document is {d.get('width')}x{d.get('height')} but the layer is {w}x{h}: "
                               "fetch the picture again")
        rel_l, rel_m = self._file("layer"), self._file("mask")
        scratch = mask_doc = None
        try:
            icc = self._target_profile(d)
            (self.exchange / rel_l).write_bytes(tagged_rgb16(layer16, icc))
            if not cv2.imwrite(str(self.exchange / rel_m), mask8, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                raise ControlError("could not write the Soft Light mask")
            scratch = self._open(rel_l)
            layers = (self.client.run("layer.list") or {}).get("layers") or []
            if not layers:
                raise ControlError("PhotoCraft opened an empty document")
            lid = int(layers[0]["id"])
            mask_doc = self._open(rel_m)
            for attempt in range(MASK_ATTEMPTS):  # a click in PhotoCraft can clear the selection in between
                try:
                    self._activate(scratch)
                    self.client.run("select.loadSelection", channel="composite", document=mask_doc)
                    self.client.run("layer.layerMask.revealSelection", layer=lid)
                    break
                except ControlError:
                    if attempt == MASK_ATTEMPTS - 1:
                        raise
                    time.sleep(0.2)
            self.client.run("layer.setProps", layer=lid, blend="Soft Light", name=name)
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            tops = (self.client.run("layer.list") or {}).get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            copied = self.client.run("layer.copyToDocument", document=int(target["index"]), source=scratch, layers=[lid])
            ids = (copied or {}).get("layers") or []
            return {"layer": ids[0] if ids else None, "document": target.get("name")}
        finally:
            for idx in sorted((i for i in (scratch, mask_doc) if i is not None), reverse=True):
                self._close(idx)
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError:
                pass
            for rel in (rel_l, rel_m):
                try:
                    (self.exchange / rel).unlink()
                except OSError:
                    pass

    def send_mask(self, mask8, doc):
        gray16 = mask8.astype(np.uint16) * 257
        rgb = np.repeat(gray16[..., None], 3, axis=2)
        return self._send_skin_layers([("Skin mask", rgb, "Normal", False)], doc)

    def send_frequency(self, layers, mask8, doc, source_icc):
        return self._send_skin_layers(
            [("Color", layers["color"], "Normal", False),
             ("Blur", layers["blur"], "Normal", True),
             ("Detail", layers["detail"], "Linear Light", False)],
            doc, mask8, source_icc)

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
        scratch_docs, files = [], []
        try:
            icc = self._target_profile(target)
            if blur_mask is not None and (not source_icc or icc != source_icc):
                raise ControlError("the document profile changed: fetch the picture again before separating")
            assembly = None
            ids = []
            blur_id = None
            for name, pixels, blend, masked in layers:
                rel = self._file(name)
                files.append(rel)
                (self.exchange / rel).write_bytes(tagged_rgb16(pixels, icc))
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
            else:
                lid = ids[0]
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            tops = self.client.run("layer.list").get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            copied = self.client.run("layer.copyToDocument", document=int(target["index"]),
                                     source=assembly, layers=[lid])
            return {"layer": copied["layers"][0], "document": target["name"]}
        finally:
            for idx in sorted(scratch_docs, reverse=True):
                self._close(idx)
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError:
                pass
            for rel in files:
                (self.exchange / rel).unlink(missing_ok=True)
