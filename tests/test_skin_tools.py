"""CPU-only mask/filter/API regressions; no model inference or live document."""

import io
from unittest.mock import patch
from urllib.error import HTTPError
import unittest

import cv2
import numpy as np
from PIL import Image

import test_dnb as T
from test_dnb import PARAMS, synthetic_image
from shp_photocraft_tools.modules.dnb import dodge_burn as D, frequency_separation as F, ui_server as S, skintone as ST


class FilterTests(unittest.TestCase):
    def test_bounded_rgb16_conversion_is_exact_for_all_rows_and_sample_types(self):
        rng = np.random.default_rng(59)
        for dtype in (np.float32, np.float64):
            image = rng.uniform(-.05, 1.05, (147, 91, 3)).astype(dtype)
            image[0, :3] = np.array([0, .5, 1])[:, None]
            image = image[:, ::-1]
            np.testing.assert_array_equal(F.rgb16(image),
                                          np.round(np.clip(image, 0, 1) * 65535).astype(np.uint16))

    def test_ratio_uses_rounded_clamped_actual_radius_and_legacy_requests_stay_explicit(self):
        for S, radius in ((181, 6), (362, 12), (90.5, 3), (196.08333334, 7), (10000, 100)):
            for percent in (100, 200, 500):
                p = F.settings({"S": S, "frequency": {"radius": 99, "blur_radius": 1,
                                "radius_at_s181": 6, "blur_percent": percent}})
                self.assertEqual(p["radius"], radius)
                self.assertEqual(p["blur_radius"], radius * percent / 100)
        p = F.settings({"frequency": {"radius": 17, "blur_radius": 9}})
        self.assertEqual((p["radius"], p["blur_radius"]), (17, 9))
        for percent in (99, 501, float("nan")):
            with self.assertRaisesRegex(ValueError, "percent"):
                F.settings({"S": 181, "frequency": {"blur_percent": percent}})

    def test_radius_calibration_is_independent_metadata_not_processing_scale(self):
        image = synthetic_image()
        mask = np.ones(image.shape[:2], np.float32)
        p = F.settings({"frequency": {"radius": 17}})
        self.assertEqual(p["radius"], 17)
        self.assertEqual(p["radius_at_s181"], 6)
        p2 = F.settings({"frequency": {"radius": 17, "radius_at_s181": 12}})
        for key in ("method", "radius", "blur_radius"):
            self.assertEqual(p[key], p2[key])
        first = F.separate(image, mask, p["method"], p["radius"], p["blur_radius"])
        second = F.separate(image, mask, p2["method"], p2["radius"], p2["blur_radius"])
        for key in first:
            np.testing.assert_array_equal(first[key], second[key])
        for calibration in (0, 31, float("nan")):
            with self.assertRaisesRegex(ValueError, "calibration"):
                F.settings({"frequency": {"radius_at_s181": calibration}})

    def test_gaussian_excludes_non_skin_even_at_soft_boundaries(self):
        image = np.full((31, 39, 3), [1, 0, 0], np.float32)
        mask = np.zeros((31, 39), np.float32)
        mask[4:27, 8:32] = 1
        mask[4:27, 8:10] = 0.1
        image[mask > 0] = [.2, .4, .7]
        filtered = F.skin_filter(image, mask, 6)
        np.testing.assert_allclose(filtered[mask > 0], image[mask > 0], atol=2e-6)
        np.testing.assert_array_equal(filtered[mask == 0], image[mask == 0])
        ordinary = cv2.GaussianBlur(image, (0, 0), 6)
        self.assertGreater(float(np.abs(ordinary[mask > 0] - filtered[mask > 0]).max()), .1)

    def test_masked_median_is_a_real_neighborhood_median_not_a_zero_fill(self):
        image = np.full((7, 9, 3), 0.99, np.float32)
        image[:, 2:6] = .3
        image[3, 3] = .8
        mask = np.zeros((7, 9), np.float32)
        mask[:, 2:6] = .25
        result = F.skin_filter(image, mask, 2, "median")
        self.assertAlmostEqual(float(result[3, 3, 0]), .3, places=6)
        self.assertAlmostEqual(float(result[3, 5, 0]), .3, places=6)
        np.testing.assert_array_equal(result[mask == 0], image[mask == 0])

    def test_zero_sparse_and_full_masks_and_independent_blur(self):
        image = synthetic_image()
        for method in ("gaussian", "median"):
            reference = F.separate(image, np.ones(image.shape[:2], np.float32), method, 3, 5)
            for value in (0, 0.3, 1):
                mask = np.full(image.shape[:2], value, np.float32)
                layers = F.separate(image, mask, method, 3, 5)
                np.testing.assert_array_equal(layers["color"], reference["color"])
                np.testing.assert_array_equal(layers["detail"], reference["detail"])
                np.testing.assert_allclose(layers["color"] + 2 * layers["detail"] - 1, image, atol=2e-7)
                if value == 0:
                    np.testing.assert_array_equal(layers["blur"], layers["color"])
                    np.testing.assert_allclose(F.composite(layers, mask), image, atol=2e-7)
            mask = np.zeros(image.shape[:2], np.float32)
            mask[24, 32] = .001
            layers = F.separate(image, mask, method, 4, 0)
            np.testing.assert_array_equal(layers["blur"], layers["color"])
        layers = F.separate(image, np.ones(image.shape[:2], np.float32), radius=3, blur_radius=7)
        self.assertGreater(float(np.abs(layers["blur"] - layers["color"]).max()), .01)

    def test_full_image_separation_and_blur_only_mask_semantics(self):
        image = synthetic_image()
        mask = np.zeros(image.shape[:2], np.float32)
        mask[10:80, 20:90] = .25
        layers = F.separate(image, mask, radius=3, blur_radius=7)
        np.testing.assert_allclose(layers["color"], cv2.GaussianBlur(
            image, (0, 0), 3, borderType=cv2.BORDER_REPLICATE), atol=2e-6)
        self.assertGreater(float(np.abs(layers["color"][mask == 0] - image[mask == 0]).max()), .01)
        np.testing.assert_array_equal(layers["blur"][mask == 0], layers["color"][mask == 0])
        # Skin opacity blends only Blur, once, before the unmasked residual.
        expected = np.clip(image + (layers["blur"] - layers["color"]) * mask[..., None], 0, 1)
        np.testing.assert_allclose(F.composite(layers, mask), expected, atol=2e-7)
        np.testing.assert_allclose(F.composite(layers, mask)[mask == 0], image[mask == 0], atol=2e-7)

    def test_shared_combined_mask_does_not_depend_on_dnb_correction_strength(self):
        image = synthetic_image()
        p = S.parse_params(PARAMS)
        base, total, structure = S.tool_mask(image, None, p, 63)
        _, _, _, diag, expected = D.process(image, 63, p["bands"], strength=0, mask=base,
                                           shadows=p["shadows"], highlights=p["highlights"],
                                           tone_strength=p["tone_strength"], protect=p["protect"],
                                           protect_mm=p["protect_mm"], protect_thr=p["protect_thr"])
        np.testing.assert_array_equal(total, expected)
        np.testing.assert_array_equal(structure, diag["structure"])


class SkinApiTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    get = T.DnbTests.get
    post = T.DnbTests.post

    def test_all_send_modes_receive_exact_icc_captured_by_get(self):
        icc = T.ImageCms.ImageCmsProfile(T.ImageCms.createProfile("sRGB")).tobytes()
        with Image.open(self.image) as image:
            image.load()
            image.save(self.image, icc_profile=icc)
        received = []

        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": self.image.name}

            def send_skin_bands(inner, layers, mask, doc, *, source_icc=None):
                received.append(("dnb", source_icc))
                return {"layer": 1}

            def send_mask(inner, mask, doc, *, source_icc=None):
                received.append(("mask", source_icc))
                return {"layer": 2}

            def send_skintone(inner, layers, settings, mask, doc, source_icc, *, blur_future=None, transfer=None, blur_strength=100, blur_masked=True):
                if blur_future is not None:
                    layers = {**layers, "blur": blur_future.result()}
                received.append(("frequency", source_icc))
                self.assertTrue(all(a.dtype == np.uint16 for a in layers.values()))
                return {"layer": 3}

        S.BRIDGE["b"] = FakeBridge()
        self.post("/api/pc/fetch", {})
        for mode in ("dnb", "mask", "frequency"):
            self.post("/api/pc/send_layers", dict(PARAMS, mode=mode))
        self.assertEqual(received, [(mode, icc) for mode in ("dnb", "mask", "frequency")])

    def test_mode_previews_saves_and_send_shapes(self):
        sent = []

        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": self.image.name}

            def send_mask(inner, mask, doc, *, source_icc=None):
                sent.append(("mask", mask.copy()))
                return {"layer": 10, "document": doc["name"]}

            def send_skintone(inner, layers, settings, mask, doc, source_icc, *, blur_future=None, transfer=None, blur_strength=100, blur_masked=True):
                if blur_future is not None:
                    layers = {**layers, "blur": blur_future.result()}
                sent.append(("frequency", layers, mask.copy(), source_icc))
                return {"layer": 11, "document": doc["name"]}

        S.BRIDGE["b"] = FakeBridge()
        self.post("/api/pc/fetch", {})
        for mode in ("mask", "frequency"):
            p = dict(PARAMS, mode=mode, strength=0,
                     skintone={"hue_amount":0},
                     frequency={"method": "median", "radius": 4, "blur_radius": 7})
            run = self.post("/api/run", p)
            self.assertEqual(run["stats"]["preview_px"], [128, 96])
            for key in ("orig", "result", "mask", "analysis") + (("color", "detail", "blur") if mode == "frequency" else ()):
                data, _ = self.get("/img/" + key)
                with Image.open(io.BytesIO(data)) as img:
                    self.assertEqual(img.size, (128, 96))
            self.assertTrue(self.post("/api/pc/send_layers", p)["sent"])
            if mode == "frequency":
                layers = {key: a.astype(np.float32) / 65535 for key, a in sent[-1][1].items()}
                expected = S.encode(S.u8(F.composite(layers, sent[-1][2].astype(np.float32) / 255)), ".png")
                self.assertEqual(self.get("/img/result")[0], expected)
                opacity = sent[-1][2].astype(np.float32) / 255
                masked_blur = layers["color"] + (layers["blur"] - layers["color"]) * opacity[..., None]
                self.assertEqual(self.get("/img/blur")[0], S.encode(S.u8(masked_blur), ".png"))
            saved = self.post("/api/save", p)
            self.assertIn("skinmask.png", saved["files"])
            if mode == "frequency":
                self.assertEqual(set(saved["files"]), {"skinmask.png", "color.png", "detail.png", "blur.png", "result.png", "Hue.cube", "settings.json"})
        self.assertEqual(sent[0][1].shape, (96, 128))
        self.assertEqual(sent[0][1].dtype, np.uint8)
        self.assertEqual(set(sent[1][1]), {"color", "detail", "blur"})
        for layer in sent[1][1].values():
            self.assertEqual(layer.shape, (96, 128, 3))
            self.assertEqual(layer.dtype, np.uint16)
        np.testing.assert_array_equal(sent[0][1], sent[1][2])
        # A local load must not retain the previously fetched document identity.
        self.post("/api/load", {"path": str(self.image)})
        with self.assertRaises(HTTPError):
            self.post("/api/pc/send", dict(PARAMS, mode="mask"))

    def test_blur_pane_feathers_after_filtering_over_color_without_changing_layers(self):
        self.post("/api/load", {"path": str(self.image)})
        shape = S.STATE["img"].shape
        color = np.full(shape, .2, np.float32)
        blur = np.full(shape, .8, np.float32)
        layers = {"color": color, "blur": blur, "detail": np.full(shape, .5, np.float32)}
        mask = np.zeros(shape[:2], np.float32)
        mask[:, 32:64] = .25
        mask[:, 64:96] = .5
        mask[:, 96:] = 1
        originals = {key: a.copy() for key, a in layers.items()}
        with patch.object(S, "tool_mask", return_value=(mask, mask, None)), \
                patch.object(ST, "separate", return_value={key:F.rgb16(a).astype(np.float32)/65535 for key,a in layers.items()}):
            self.post("/api/run", dict(PARAMS, mode="frequency"))
        data, _ = self.get("/img/blur")
        actual = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        qcolor = F.rgb16(color).astype(np.float32) / 65535
        qblur = F.rgb16(blur).astype(np.float32) / 65535
        opacity = S.u8(mask).astype(np.float32) / 255
        expected = S.u8(qcolor + (qblur - qcolor) * opacity[..., None])
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(actual[mask == 0], S.u8(qcolor)[mask == 0])
        self.assertNotEqual(int(actual[0, 40, 0]), int(S.u8(blur)[0, 40, 0]))
        for key in layers:
            np.testing.assert_array_equal(layers[key], originals[key])

    def test_validation_preview_radius_scaling_and_no_dnb_processing(self):
        self.post("/api/load", {"path": str(self.image)})
        for mode in ("mask", "frequency"):
            with patch.object(D, "process", side_effect=AssertionError("D&B must not run in skin modes")):
                self.post("/api/run", dict(PARAMS, mode=mode))
        for extra in ({"mode": "bad"}, {"mode": "frequency", "frequency": {"method": "box"}},
                      {"mode": "frequency", "frequency": {"radius": 0}},
                      {"mode": "frequency", "frequency": {"blur_radius": 501}},
                      {"mode": "frequency", "frequency": {"radius": float("nan")}}):
            with self.assertRaises(HTTPError):
                self.post("/api/run", dict(PARAMS, **extra))
        p = S.parse_params(dict(PARAMS, mode="frequency",
                                frequency={"radius": 20, "blur_radius": 30}))
        with patch.object(F, "separate", return_value={}) as separated:
            S.frequency_layers(S.STATE["source_img"], np.ones((96, 128), np.float32), p, .25)
            self.assertEqual(separated.call_args.args[2:], ("gaussian", 5, 7.5))
        p = S.parse_params(dict(PARAMS, S=10000, mode="frequency",
                                frequency={"radius_at_s181": 6, "blur_percent": 500}))
        with patch.object(F, "separate", return_value={}) as separated:
            S.frequency_layers(S.STATE["source_img"], np.ones((96, 128), np.float32), p, .25)
            self.assertEqual(separated.call_args.args[2:], ("gaussian", 25, 125))
        layers = S.frequency_layers(S.STATE["source_img"], np.ones((96, 128), np.float32), p)
        self.assertTrue(np.isfinite(layers["blur"]).all())
        S.STATE["source_opaque"] = False
        with self.assertRaisesRegex(ValueError, "opaque"):
            S.frequency_layers(S.STATE["source_img"], np.ones((96, 128), np.float32), p)


if __name__ == "__main__":
    unittest.main()
