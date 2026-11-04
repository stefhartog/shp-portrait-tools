"""D&B-only Soft Light layer, mask application and scratch-document cleanup."""

import time

import cv2
import numpy as np

from shp_photocraft_tools.shared.photocraft import Bridge as BaseBridge, Client, ControlError
from shp_photocraft_tools.shared.photocraft import detect_config as _detect_config

MASK_ATTEMPTS = 4


def detect_config():
    return _detect_config("DNB", "dnb")


class Bridge(BaseBridge):
    env_prefix = "DNB"
    module = "dnb"
    app_name = "SHP PhotoCraft D&B"

    def send(self, layer16, mask8, doc, name="SHP PhotoCraft D&B soft light"):
        """Copy the 16-bit RGB layer with its mask and blend mode into the target in one step."""
        h, w = mask8.shape
        if layer16.shape[:2] != (h, w):
            raise ControlError("layer and mask sizes differ")
        d = self._find(doc["index"], doc["name"])
        if (d.get("width"), d.get("height")) not in ((w, h), (None, None)):
            raise ControlError(f"the document is {d.get('width')}x{d.get('height')} but the layer is {w}x{h}: "
                               "fetch the picture again")
        rgba = np.dstack([layer16, np.full((h, w), 65535, np.uint16)])  # opens as a normal layer, not Background
        rel_l, rel_m = self._file("layer"), self._file("mask")
        cv2.imwrite(str(self.exchange / rel_l), rgba, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        cv2.imwrite(str(self.exchange / rel_m), mask8, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        scratch = mask_doc = None
        try:
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
