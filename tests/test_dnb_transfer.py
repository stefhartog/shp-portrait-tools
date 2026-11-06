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
from shp_photocraft_tools.modules.dnb import skintone as ST
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
        env["PHOTOCRAFT_CONFIG_DIR"] = str(self.root / "config")
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
                    exported, _ = self.bridge.fetch(self.root / "input")
                    with Image.open(exported) as image:
                        captured = image.info["icc_profile"]
                    before = self.save_pixels("before.png")
                    history = self.client.run("document.inspect")["history"]
                    with patch.object(self.bridge, "_target_profile",
                                      side_effect=AssertionError("redundant profile export")):
                        result = self.bridge.send(layer, mask, target, source_icc=captured)
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

    def test_early_source_close_preserves_exact_pixels_profiles_and_target_indices(self):
        innocent = self.new_target("linear-srgb")
        innocent_before = self.client.run("document.inspect")
        rng = np.random.default_rng(173)
        source = rng.uniform(.15, .85, (8, 8, 3)).astype(np.float32)
        mask = np.tile(np.array([0, 32, 64, 128, 192, 255, 0, 255], np.uint8), (8, 1))
        layers = ST.separate(source, mask / 255, "gaussian", 2, 3, native=True)
        for mode in ("dnb", "frequency", "skintone", "mask"):
            outputs = []
            peaks = []
            for early in (False, True):
                with self.subTest(mode=mode, early=early):
                    target = self.new_target("prophoto-compat")
                    history = len(self.client.run("document.inspect")["history"])
                    opened, inserted = [], []
                    open_doc, run = self.bridge._open, self.client.run

                    def observe_open(rel):
                        index = open_doc(rel)
                        opened.append(len(self.bridge.session()["documents"]))
                        return index

                    def observe_run(command, **params):
                        if command == "layer.copyToDocument" and params["document"] == target["index"]:
                            inserted.append(len(self.bridge.session()["documents"]))
                        return run(command, **params)

                    # Suppressing just the early release reproduces the previous
                    # lifetime: every source survives until final descending cleanup.
                    with patch.object(self.bridge, "_open", side_effect=observe_open), \
                            patch.object(self.client, "run", side_effect=observe_run), \
                            patch.object(self.bridge, "_release_source",
                                         wraps=self.bridge._release_source if early else None):
                        if mode == "dnb":
                            self.bridge.send(layers["detail"], mask, target)
                        elif mode == "mask":
                            self.bridge.send_mask(mask, target)
                        else:
                            icc = self.bridge._target_profile(target)
                            if mode == "frequency":
                                self.bridge.send_frequency(layers, mask, target, icc)
                            else:
                                self.bridge.send_skintone(layers, ST.settings(
                                    {"skintone": {"hue_center": 50, "hue_width": 2}}), mask, target, icc)
                    self.assertEqual(len(self.client.run("document.inspect")["history"]), history + 1)
                    self.client.call("doc.save", path="result.png")
                    path = self.bridge.exchange / "result.png"
                    with Image.open(path) as image:
                        icc = image.info["icc_profile"]
                    outputs.append((cv2.imread(str(path), -1), icc))
                    path.unlink()
                    peaks.append(max(opened))
                    self.assertEqual(inserted, [3 if early or mode == "mask" else
                                                4 if mode == "dnb" else 6])
                    self.assertEqual(len(self.bridge.session()["documents"]), 2)
                    self.assertEqual(self.bridge.active_doc()["index"], target["index"])
                    self.assertEqual(self.client.run("document.inspect", document=innocent["index"]),
                                     innocent_before)
                    self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                    self.client.run("file.close", document=target["index"])
            np.testing.assert_array_equal(outputs[0][0], outputs[1][0])
            self.assertEqual(outputs[0][1], outputs[1][1])
            if mode in ("frequency", "skintone"):
                self.assertEqual(peaks, [6, 4], "Keep only assembly plus one temporary source")

    def test_source_close_failure_is_reported_and_final_cleanup_keeps_user_documents(self):
        self.new_target("linear-srgb")
        target = self.new_target("srgb")
        before = self.client.run("document.inspect")
        layers = {key: np.full((8, 8, 3), 32768, np.uint16) for key in ("color", "blur", "detail")}
        icc = self.bridge._target_profile(target)
        run = self.client.run
        failed = []

        def fail_close(command, **params):
            if command == "file.close" and params["document"] > target["index"] + 1 and not failed:
                failed.append(params["document"])
                raise ControlError("fixture source close failed")
            return run(command, **params)

        with patch.object(self.client, "run", side_effect=fail_close):
            with self.assertRaisesRegex(ControlError, "source close failed"):
                self.bridge.send_skintone(layers, ST.settings({}), np.full((8, 8), 128, np.uint8),
                                          target, icc)
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.client.run("document.inspect"), before)
        self.assertEqual(len(self.bridge.session()["documents"]), 2)
        self.assertEqual(self.bridge.active_doc()["index"], target["index"])
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])

    def test_get_icc_reuse_avoids_send_export_with_exact_native_pixels_and_profile(self):
        source = np.full((8, 8, 3), [.25, .4, .7], np.float32)
        mask = np.tile(np.array([0, 32, 64, 128, 192, 255, 0, 255], np.uint8), (8, 1))
        layers = ST.separate(source, mask / 255, "gaussian", 2, 3, native=True)
        for mode in ("dnb", "mask", "frequency", "skintone"):
            outputs = []
            for cached in (False, True):
                with self.subTest(mode=mode, cached=cached):
                    target = self.new_target("prophoto-compat")
                    exported, _ = self.bridge.fetch(self.root / "input")
                    with Image.open(exported) as image:
                        captured = image.info["icc_profile"]
                    get_profile = self.bridge._target_profile
                    profile = captured if cached else get_profile(target)
                    with patch.object(self.bridge, "_target_profile", side_effect=AssertionError(
                            "Send must not export Get's destination again")), \
                            patch.object(self.bridge, "fetch", side_effect=AssertionError("redundant Get")):
                        if mode == "dnb":
                            self.bridge.send(layers["detail"], mask, target, source_icc=profile)
                        elif mode == "mask":
                            self.bridge.send_mask(mask, target, source_icc=profile)
                        elif mode == "frequency":
                            self.bridge.send_frequency(layers, mask, target, profile)
                        else:
                            self.bridge.send_skintone(layers, ST.settings(
                                {"skintone": {"hue_center": 50, "hue_width": 2}}), mask, target, profile)
                    self.client.call("doc.save", path="result.png")
                    path = self.bridge.exchange / "result.png"
                    with Image.open(path) as image:
                        self.assertEqual(image.info["icc_profile"], captured)
                    outputs.append(cv2.imread(str(path), -1))
                    path.unlink()
                    self.assertEqual(len(self.bridge.session()["documents"]), 1)
                    self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                    self.client.run("file.close", document=target["index"])
            np.testing.assert_array_equal(outputs[0], outputs[1])

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

    def test_frequency_refuses_stale_size_missing_profile_and_cleans_scratch(self):
        doc = self.new_target("srgb")
        layers = {key: np.full((8, 8, 3), 32768, np.uint16) for key in ("color", "blur", "detail")}
        mask = np.full((8, 8), 255, np.uint8)
        icc = self.bridge._target_profile(doc)
        with self.assertRaisesRegex(ControlError, "no ICC profile captured"):
            self.bridge.send_frequency(layers, mask, doc, None)
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
