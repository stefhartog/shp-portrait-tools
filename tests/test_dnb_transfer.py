"""Optional real-engine regressions. Only a new, isolated stdio process is used."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge
from shp_photocraft_tools.modules.dnb import frequency_separation as F
from shp_photocraft_tools.shared.photocraft import ControlError


class StdioClient:
    def __init__(self, process):
        self.process = process

    def call(self, method, **params):
        self.process.stdin.write(json.dumps({"id": 1, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise ControlError("isolated PhotoCraft process closed stdout")
        reply = json.loads(line)
        if not reply["ok"]:
            raise ControlError(f"{method}: {reply.get('error')}")
        return reply.get("result")

    def run(self, command, **params):
        return self.call("engine.execute", command=command, params=params)


@unittest.skipUnless(shutil.which("photocraft-cli"), "photocraft-cli is not installed")
class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dnb-transfer-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = Bridge(port=1, token_file=self.root / "unused",
                             exchange=self.root / "exchange", read_root=self.root)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHOTOCRAFT_")}
        self.process = subprocess.Popen(
            ["photocraft-cli", "serve", "--automation-read-root", str(self.root),
             "--automation-write-root", str(self.bridge.exchange)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.close_process)
        self.bridge.client = StdioClient(self.process)
        self.bridge.headless = True
        self.client = self.bridge.client

    def close_process(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=20)
        self.process.stdout.close()

    def new_target(self, profile, depth=16):
        self.client.call("doc.new", width=8, height=8, depth=depth)
        self.client.run("edit.assignProfile", profile=profile)
        self.client.run("edit.fill", contents="color", color=[0.35, 0.5, 0.65, 1])
        return self.bridge.active_doc()

    def save_pixels(self, name):
        self.client.call("doc.save", path=name)
        path = self.bridge.exchange / name
        pixels = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        path.unlink()
        return pixels

    def test_numeric_corrections_masks_and_srgb_baseline(self):
        # 32768 is round(0.5 * 65535), the engine's existing 16-bit neutral.
        gray = np.tile(np.array([0, 16384, 29491, 32768, 36044, 49151, 65535, 32768],
                                np.uint16), (8, 1))
        layer = np.repeat(gray[..., None], 3, axis=2)
        mask = np.tile(np.array([255, 0, 64, 128, 192, 255, 255, 255], np.uint8), (8, 1))
        profiles = ("srgb", "none", "display-p3", "adobe-rgb-compat", "prophoto-compat",
                    "linear-srgb", "rec2020", "aces-cg")
        for profile in profiles:
            for depth in (8, 16, 32):
                with self.subTest(profile=profile, depth=depth):
                    target = self.new_target(profile, depth)
                    before = self.save_pixels("before.png")
                    history = self.client.run("document.inspect")["history"]
                    result = self.bridge.send(layer, mask, target)
                    inspection = self.client.run("document.inspect")
                    self.assertEqual(len(inspection["history"]), len(history) + 1)
                    self.assertEqual((inspection["width"], inspection["height"]), (8, 8))
                    self.assertEqual(len(self.bridge.session()["documents"]), 1)
                    top = inspection["layers"][0]
                    self.assertEqual(top["id"], result["layer"])
                    self.assertEqual(top["blend"], "Soft Light")
                    self.assertTrue(top["hasMask"])
                    after = self.save_pixels("after.png")
                    # Reference: unchanged W3C Soft Light in destination numeric space.
                    b = before[..., :3].astype(np.float64) / np.iinfo(before.dtype).max
                    s = layer.astype(np.float64) / 65535
                    d = np.where(b <= 0.25, ((16 * b - 12) * b + 4) * b, np.sqrt(b))
                    soft = np.where(s <= 0.5, b - (1 - 2 * s) * b * (1 - b),
                                    b + (2 * s - 1) * (d - b))
                    expected = b + (soft - b) * (mask[..., None] / 255)
                    scale = np.iinfo(after.dtype).max
                    np.testing.assert_allclose(after[..., :3] / scale, expected, atol=2 / scale)
                    # Isolate copied data and mask, not just its composite appearance.
                    self.client.run("layer.setProps", layer=top["id"], blend="Normal")
                    self.client.run("layer.setProps", layer=inspection["layers"][1]["id"], visible=False)
                    copied = self.save_pixels("copied.png")
                    self.assertEqual(copied.shape, (8, 8, 4))
                    visible = mask > 0
                    np.testing.assert_array_equal(copied[..., 3],
                                                  mask.astype(copied.dtype) * (257 if copied.dtype == np.uint16 else 1))
                    expected_samples = np.round(layer / 65535 * np.iinfo(copied.dtype).max)
                    np.testing.assert_array_equal(copied[..., :3][visible], expected_samples[visible])
                    self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                    self.client.run("file.close", document=target["index"])

    def test_old_untagged_copy_moves_neutral_in_wide_gamut(self):
        for profile, expected in (("srgb", 0.5), ("adobe-rgb-compat", 0.49612),
                                  ("prophoto-compat", 0.42467), ("linear-srgb", 0.21405)):
            with self.subTest(profile=profile):
                target = self.new_target(profile)
                rgba = np.full((8, 8, 4), 32768, np.uint16)
                rgba[..., 3] = 65535
                cv2.imwrite(str(self.root / "old-layer.png"), rgba)
                self.client.call("doc.open", path="old-layer.png")
                source = self.bridge.session()["active"]
                lid = self.client.run("layer.list")["layers"][0]["id"]
                self.bridge._activate(target["index"])
                self.client.run("layer.copyToDocument", document=target["index"], source=source, layers=[lid])
                pixel = self.client.run("document.sampleColor", x=1, y=1)
                np.testing.assert_allclose(pixel[:3], expected, atol=2e-5)
                self.client.run("file.close", document=source)
                self.client.run("file.close", document=target["index"])

    def test_custom_embedded_icc_is_not_replaced_with_a_builtin(self):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        Image.new("RGB", (8, 8), "gray").save(self.root / "custom.png", icc_profile=icc)
        self.client.call("doc.open", path="custom.png")
        self.client.run("image.mode.bits16")
        target = self.bridge.active_doc()
        layer = np.full((8, 8, 3), 32768, np.uint16)
        layer[:, :4] = 36044
        mask = np.full((8, 8), 255, np.uint8)
        result = self.bridge.send(layer, mask, target)
        self.client.call("doc.save", path="custom-result.png")
        with Image.open(self.bridge.exchange / "custom-result.png") as image:
            self.assertEqual(image.info["icc_profile"], icc)
        self.client.run("layer.setProps", layer=result["layer"], blend="Normal")
        for x, expected in ((1, 36044 / 65535), (5, 32768 / 65535)):
            pixel = self.client.run("document.sampleColor", x=x, y=1)
            np.testing.assert_allclose(pixel[:3], expected, atol=1e-7)

    def test_frequency_group_reconstruction_profiles_masks_and_single_undo(self):
        y, x = np.mgrid[:16, :20]
        src = np.stack([.15 + x / 35, .3 + .2 * np.sin(y * 2), .6 + .3 * np.cos(x * 2)], axis=2)
        source16 = F.rgb16(src)
        mask = np.full((16, 20), 255, np.uint8)
        mask[:, :3] = 0
        mask[:, 3:6] = 64
        mask[:, 6:9] = 128
        for profile in ("srgb", "none", "display-p3", "adobe-rgb-compat", "prophoto-compat",
                        "linear-srgb", "rec2020", "aces-cg"):
            for depth in (8, 16, 32):
                for method in ("gaussian", "median"):
                    with self.subTest(profile=profile, depth=depth, method=method):
                        cv2.imwrite(str(self.root / "source.png"), source16)
                        self.client.call("doc.open", path="source.png")
                        self.client.run("edit.assignProfile", profile=profile)
                        self.client.run(f"image.mode.bits{depth}")
                        doc = self.bridge.active_doc()
                        path, _ = self.bridge.fetch(self.root / "input")
                        raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[..., :3]
                        img = raw.astype(np.float32) / np.iinfo(raw.dtype).max
                        with Image.open(path) as image:
                            icc = image.info["icc_profile"]
                        before = self.save_pixels("before.png")
                        histories = len(self.client.run("document.inspect")["history"])
                        layers = {key: F.rgb16(a) for key, a in F.separate(img, mask / 255, method, 2, 3).items()}
                        result = self.bridge.send_frequency(layers, mask, doc, icc)
                        inspection = self.client.run("document.inspect")
                        self.assertEqual(len(inspection["history"]), histories + 1)
                        self.assertEqual(len(self.bridge.session()["documents"]), 1)
                        group = inspection["layers"][0]
                        self.assertEqual(group["id"], result["layer"])
                        self.assertEqual(group["blend"], "Normal")
                        self.assertFalse(group["hasMask"])
                        detail, blur, color = group["children"]
                        self.assertEqual([a["name"] for a in group["children"]], ["Detail", "Blur", "Color"])
                        self.assertEqual(detail["blend"], "Linear Light")
                        self.assertTrue(blur["hasMask"])
                        self.assertFalse(detail["hasMask"])
                        self.assertFalse(color["hasMask"])
                        after = self.save_pixels("after.png")
                        scale = np.iinfo(after.dtype).max
                        numeric = {key: a.astype(np.float64) / 65535 for key, a in layers.items()}
                        np.testing.assert_allclose(after[..., :3] / scale,
                                                   F.composite(numeric, mask / 255), atol=3 / scale)
                        # Hiding Blur leaves a full-image reconstruction, independent of mask.
                        self.client.run("layer.setProps", layer=blur["id"], visible=False)
                        reconstructed = self.save_pixels("reconstructed.png")
                        np.testing.assert_allclose(reconstructed[..., :3] / scale, before[..., :3] / scale,
                                                   atol=3 / scale)
                        self.client.run("layer.setProps", layer=blur["id"], visible=True)
                        # Only Blur uses the soft mask: Color itself is full-image and unmasked.
                        self.client.run("layer.setProps", layer=detail["id"], visible=False)
                        low = self.save_pixels("low.png")[..., :3] / scale
                        expected = numeric["color"] + (numeric["blur"] - numeric["color"]) * (mask[..., None] / 255)
                        np.testing.assert_allclose(low, expected, atol=2 / scale)
                        # Disable Blur's mask and its entire unattenuated data becomes visible.
                        self.client.run("layer.layerMask.enabled", layer=blur["id"], enabled=False)
                        smoothed = self.save_pixels("smoothed.png")[..., :3] / scale
                        np.testing.assert_allclose(smoothed, numeric["blur"], atol=2 / scale)
                        self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                        self.client.run("file.close", document=doc["index"])

    def test_skin_mask_send_is_grayscale_numeric_layer_without_changing_original_mask(self):
        doc = self.new_target("prophoto-compat")
        original = self.client.run("document.inspect")["layers"][0]["id"]
        self.client.run("layer.layerMask.revealAll", layer=original)
        mask = np.tile(np.array([0, 32, 64, 128, 192, 255, 0, 255], np.uint8), (8, 1))
        history = len(self.client.run("document.inspect")["history"])
        self.bridge.send_mask(mask, doc)
        inspection = self.client.run("document.inspect")
        self.assertEqual(len(inspection["history"]), history + 1)
        self.assertEqual(inspection["layers"][0]["name"], "Skin mask")
        self.assertFalse(inspection["layers"][0]["hasMask"])
        self.assertTrue(inspection["layers"][1]["hasMask"])
        for x in range(8):
            np.testing.assert_allclose(self.client.run("document.sampleColor", x=x, y=2)[:3],
                                       mask[2, x] / 255, atol=1e-7)

    def test_frequency_refuses_stale_size_profile_and_cleans_scratch(self):
        doc = self.new_target("srgb")
        layers = {key: np.full((8, 8, 3), 32768, np.uint16) for key in ("color", "blur", "detail")}
        mask = np.full((8, 8), 255, np.uint8)
        icc = self.bridge._target_profile(doc)
        self.client.run("edit.assignProfile", profile="linear-srgb")
        with self.assertRaisesRegex(ControlError, "profile changed"):
            self.bridge.send_frequency(layers, mask, doc, icc)
        self.assertEqual(len(self.client.run("document.inspect")["layers"]), 1)
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])
        wrong_size = {key: np.full((9, 8, 3), 32768, np.uint16) for key in layers}
        with self.assertRaisesRegex(ControlError, "document size changed"):
            self.bridge.send_frequency(wrong_size, np.full((9, 8), 255, np.uint8), doc, icc)

    def test_frequency_failed_target_copy_removes_only_scratch_documents(self):
        doc = self.new_target("adobe-rgb-compat")
        icc = self.bridge._target_profile(doc)
        mask = np.full((8, 8), 255, np.uint8)
        layers = {key: np.full((8, 8, 3), 32768, np.uint16) for key in ("color", "blur", "detail")}
        run = self.client.run

        def fail_target_copy(command, **params):
            if command == "layer.copyToDocument" and params["document"] == doc["index"]:
                raise ControlError("fixture target copy failed")
            return run(command, **params)

        with patch.object(self.client, "run", side_effect=fail_target_copy):
            with self.assertRaisesRegex(ControlError, "fixture target copy failed"):
                self.bridge.send_frequency(layers, mask, doc, icc)
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(self.bridge.active_doc()["index"], doc["index"])
        self.assertEqual(len(self.client.run("document.inspect")["layers"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])
