"""Crop-only canvas resize, generated layer and undo grouping."""

import numpy as np
from PIL import Image

from shp_photocraft_tools.shared.photocraft import Bridge as BaseBridge, Client, ControlError
from shp_photocraft_tools.shared.photocraft import detect_config as _detect_config


def detect_config():
    return _detect_config("SHPC", "crop")


class Bridge(BaseBridge):
    env_prefix = "SHPC"
    module = "crop"
    app_name = "SHP Portrait Tools — AI Fill"

    def send_patch(self, layer_rgba, doc):
        return self.send_expand(layer_rgba, doc, (0, 0, 0, 0), name="SHP Portrait Tools Patch Fill", resize=False)

    def send_expand(self, layer_rgba, doc, margins, name="SHP Portrait Tools Crop & Expand", *, resize=True):
        """Resize by signed margins and add the generated layer as one undo step."""
        h, w = layer_rgba.shape[:2]
        left, top, right, bottom = (int(m) for m in margins)
        d = self._find(doc["index"], doc["name"])
        ow, oh = w - left - right, h - top - bottom
        if (d.get("width"), d.get("height")) not in ((ow, oh), (None, None)):
            raise ControlError(f"the document is {d.get('width')}x{d.get('height')} but the expansion started from "
                               f"{ow}x{oh}: get the picture again")
        rel = self._file("expand")
        Image.fromarray(np.ascontiguousarray(layer_rgba), "RGBA").save(self.exchange / rel, compress_level=1)
        scratch = None
        try:
            self._activate(d["index"])
            before = list((self.client.run("document.inspect") or {}).get("history") or [])
            scratch = self._open(rel)
            layers = (self.client.run("layer.list") or {}).get("layers") or []
            if not layers:
                raise ControlError("PhotoCraft opened an empty document")
            lid = int(layers[0]["id"])
            self.client.run("layer.setProps", layer=lid, name=name)
            target = self._find(doc["index"], doc["name"])
            self._activate(target["index"])
            if resize:
                self.client.run("image.crop", x=-left, y=-top, width=w, height=h)
            tops = (self.client.run("layer.list") or {}).get("layers") or []
            if tops:
                self.client.run("layer.select", layer=int(tops[0]["id"]))
            copied = self.client.run("layer.copyToDocument", document=int(target["index"]), source=scratch, layers=[lid])
            ids = (copied or {}).get("layers") or []
            self._one_step(before, name)
            return {"layer": ids[0] if ids else None, "document": target.get("name"), "width": w, "height": h}
        finally:
            if scratch is not None:
                self._close(scratch)
            try:
                self._activate(self._find(doc["index"], doc["name"])["index"])
            except ControlError:
                pass
            try:
                (self.exchange / rel).unlink()
            except OSError:
                pass

    def _one_step(self, before, name):
        """Best-effort history grouping; generated pixels remain correct on older PhotoCraft versions."""
        try:
            after = list((self.client.run("document.inspect") or {}).get("history") or [])
            if after[:len(before)] != before or len(after) - len(before) < 2:
                return
            snap = (self.client.run("history.newSnapshot", name=name, **{"from": "fullDocument"}) or {})["snapshot"]
            self.client.run("history.deleteState", state=len(before))
            self.client.run("history.selectSnapshot", snapshot=snap)
            self.client.run("history.deleteSnapshot", snapshot=snap)
        except (ControlError, KeyError, TypeError):
            pass
