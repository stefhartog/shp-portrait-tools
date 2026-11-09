"""D&B CPU engine, API and settings contracts; no model inference or real PhotoCraft."""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.modules.crop import server as C
from shp_photocraft_tools.modules.dnb import dodge_burn as D, ui_server as S
from shp_photocraft_tools.modules.dnb import estimate_scale, estimate_body_scale, paths, skin_mask, skin_mask_compare
from shp_photocraft_tools.shared.ui import stylesheet

PARAMS = {"S": 63, "bands": [[1.5, 40, 1, 0.5]], "strength": 1.2, "edge": 0.12, "tau": 1, "max_ev": 1,
          "mask": {"on": True, "feather": 1, "features": True, "colour": True, "inward": True},
          "tone": {"on": True, "mid": 0.52, "range": 0.87, "taper": 0.16, "strength": 0.6},
          "color": {"on": False}, "structure": {"protect": 0.3, "mm": 4, "thr": 0.1}, "view": "whole"}


def synthetic_image():
    y, x = np.mgrid[:96, :128]
    luminance = np.clip(0.35 + x / 256 + 0.08 * np.sin(y / 5) * np.cos(x / 7), 0, 1).astype(np.float32)
    return np.dstack([luminance * 0.7, luminance * 0.85, luminance])


class DnbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "synthetic.png"
        cv2.imwrite(str(self.image), S.u8(synthetic_image()))
        self.patches = [
            patch.object(S, "ROOT", self.root),
            patch.object(S, "PRESETS", self.root / "new-data" / "presets.json"),
            patch.object(S, "STATE", dict(S.STATE, img=None, imgs={}, masks=None, last=None, version=0)),
            patch.object(S, "BRIDGE", {"b": None, "doc": None}),
            patch.object(S.M, "pick_target", return_value=None),
            patch.object(S, "GLOBAL_SETTINGS", self.root / "config" / "settings.json"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, route):
        with urlopen(self.base + route, timeout=5) as response:
            return response.read(), response.headers

    def post(self, route, body):
        headers={"Content-Type":"application/json"}
        if route in ("/api/pc/send_layers", "/api/global-settings"):
            headers["Origin"]=self.base
        request = Request(self.base + route, json.dumps(body).encode(), headers)
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_assets_and_unknown_routes(self):
        html, headers = self.get("/")
        self.assertIn(b"<title>SHP PhotoCraft D&amp;B</title>", html)
        self.assertLess(html.index(b"static/shp-ui.css"), html.index(b"<style>"))
        for route in ("/static/shp-ui.css", "/static/shp-ui.css?v=1"):
            css, headers = self.get(route)
            self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
            self.assertEqual(css, stylesheet())
            self.assertEqual(css, C.stylesheet())
            self.assertIn(b"width:74px", css)
            self.assertIn(b"width:42px", css)
        for route in ("/static/missing.css", "/static/../pyproject.toml", "/missing", "/img/orig"):
            with self.assertRaises(HTTPError) as error:
                self.get(route)
            self.assertEqual(error.exception.code, 404)

    def test_load_preview_analysis_picker_and_full_resolution_save(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        self.assertEqual((loaded["width"], loaded["height"]), (128, 96))
        self.assertFalse(loaded["has_mask"])
        result = self.post("/api/run", PARAMS)
        self.assertEqual(result["stats"]["preview_px"], [128, 96])
        for key in ("orig", "result", "analysis", "mask", "layer", "layerx", "layerm", "layermx"):
            data, headers = self.get("/img/" + key)
            self.assertTrue(data)
            self.assertIn(headers["Content-Type"], ("image/png", "image/jpeg"))
        picked = self.post("/api/color-pick", {"u": 0.5, "v": 0.5, "version": result["version"]})
        self.assertIn("light", picked)
        color_params = dict(PARAMS, color={"on": True, "hue": picked["hue"], "saturation": picked["saturation"]})
        self.assertTrue(self.post("/api/run", color_params)["stats"]["mask_used"])
        self.assertIn("avers", self.post("/api/analysis", {"analysis": {"detect": False, "skin": False, "structure": True}}))
        self.post("/api/centre", {"u": 0.25, "v": 0.25})
        self.post("/api/centre", {"reset": True})
        saved = self.post("/api/save", color_params)
        folder = Path(saved["dir"])
        self.assertTrue(folder.is_relative_to(self.root))
        self.assertEqual(len(saved["files"]), 4)
        layer = cv2.imread(str(folder / "synthetic_3_softlight_layer.png"), cv2.IMREAD_UNCHANGED)
        self.assertEqual(layer.shape, (96, 128, 3))
        self.assertEqual(layer.dtype, np.uint16)
        np.testing.assert_array_equal(layer[..., 0], layer[..., 1])
        self.assertIn("color", json.loads((folder / "settings.json").read_text()))
        self.assertIn("w", self.post("/api/tone", {"mid": 0.52, "range": 0.87, "taper": 0.16}))
        self.assertEqual(len(self.post("/api/profile", PARAMS)["gains"]), 120)

    def test_load_timings_measure_decode_analysis_and_actual_mask_separately(self):
        target = {"S": 181, "conf": "high", "desc": "test", "centre": (64, 48),
                  "face_info": None, "seen": []}
        ones = np.ones((96, 128), np.float32)
        components = {"A": ones, "features": ones * 0, "colour": ones, "skin": ones}
        with patch.object(S.M, "pick_target", return_value=target), \
                patch.object(S.M, "person_box", return_value=(0, 0, 128, 96)), \
                patch.object(S.K, "skin_mask", return_value=components), \
                patch.object(S.time, "perf_counter", side_effect=[0, 2, 7, 8, 11, 13]):
            loaded = S.api_load({"path": str(self.image)})
        self.assertEqual(loaded["timings"], {"loaded": 2, "analyzed": 5, "mask": 3})
        self.assertEqual(loaded["seconds"], 13)
        self.assertTrue(loaded["has_mask"])
        with patch.object(S.time, "perf_counter", side_effect=[0, 2, 7, 8, 9]), \
                patch.object(S.K, "skin_mask", side_effect=AssertionError("No mask without detection")):
            missing = S.api_load({"path": str(self.image)})
        self.assertEqual(missing["timings"], {"loaded": 2, "analyzed": 5, "mask": None})
        self.assertFalse(missing["has_mask"])
        with patch.object(S.M, "pick_target", side_effect=ValueError("analysis fixture failed")):
            with self.assertRaisesRegex(ValueError, "analysis fixture failed"):
                S.api_load({"path": str(self.image)})

    def test_get_load_timing_includes_photocraft_export_but_not_analysis(self):
        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": self.image.name}

        S.BRIDGE["b"] = FakeBridge()
        with patch.object(S.time, "perf_counter", side_effect=[0, 5, 6, 8, 13, 14, 15, 20]):
            loaded = S.api_pc_fetch({})
        self.assertEqual(loaded["timings"], {"loaded": 7, "analyzed": 5, "mask": None})
        self.assertEqual(loaded["seconds"], 20)
        self.assertEqual(loaded["pc_doc"]["index"], 1)

    def test_presets_default_roundtrip_and_validation(self):
        self.assertEqual(json.loads(self.get("/api/presets")[0]), {"default": None, "presets": {}})
        result = self.post("/api/presets/save", {"name": "Portrait", "settings": PARAMS})
        self.assertFalse(result["updated"])
        self.assertEqual(result["presets"]["Portrait"], PARAMS)
        self.assertTrue(self.post("/api/presets/save", {"name": "Portrait", "settings": PARAMS})["updated"])
        self.assertEqual(self.post("/api/presets/default", {"settings": PARAMS})["default"], PARAMS)
        with self.assertRaises(HTTPError):
            self.post("/api/presets/save", {"name": " ", "settings": PARAMS})
        self.assertEqual(self.post("/api/presets/delete", {"name": "Portrait"})["presets"], {})
        self.assertEqual(S.read_presets()["default"], PARAMS)

    def test_global_transfer_migrates_once_and_is_not_preset_owned(self):
        legacy = {"format": "tiff", "compressed": False, "opaque_alpha": False, "parallel": False}
        saved = {"default": {**PARAMS, "frequency_transfer": legacy},
                 "presets": {"Legacy": {**PARAMS, "frequency_transfer": legacy}}}
        S.write_presets(saved)
        original = S.PRESETS.read_bytes()
        settings = json.loads(self.get("/api/global-settings")[0])
        self.assertEqual(settings["layer_transfer"], legacy)
        persisted = json.loads(S.GLOBAL_SETTINGS.read_text())
        self.assertTrue(persisted["layer_transfer"]["exclude_opaque_alpha"])
        self.assertNotIn("opaque_alpha", persisted["layer_transfer"])
        self.assertEqual(S.PRESETS.read_bytes(), original)
        changed = {**legacy, "format": "png", "parallel": True}
        self.assertEqual(self.post("/api/global-settings", {"layer_transfer": changed})["layer_transfer"], changed)
        result = self.post("/api/presets/default", {"settings": {**PARAMS, "frequency_transfer": legacy}})
        self.assertNotIn("frequency_transfer", result["default"])
        result = self.post("/api/presets/save", {"name": "New", "settings": {**PARAMS, "frequency_transfer": legacy}})
        self.assertNotIn("frequency_transfer", result["presets"]["New"])
        self.assertEqual(result["presets"]["Legacy"]["frequency_transfer"], legacy)
        self.assertEqual(json.loads(self.get("/api/global-settings")[0])["layer_transfer"], changed)

    def test_global_settings_errors_do_not_overwrite_existing_preferences(self):
        S.GLOBAL_SETTINGS.parent.mkdir(parents=True)
        for invalid in ('{bad json', '[]', '{"layer_transfer":{"format":"jpeg"}}'):
            S.GLOBAL_SETTINGS.write_text(invalid)
            with self.assertRaises(HTTPError) as error:
                self.get("/api/global-settings")
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(S.GLOBAL_SETTINGS.read_text(), invalid)
        S.GLOBAL_SETTINGS.write_text(json.dumps({"other_setting": 42, "layer_transfer": {"format": "tiff"}}))
        self.post("/api/global-settings", {"layer_transfer": {"format": "png"}})
        self.assertEqual(json.loads(S.GLOBAL_SETTINGS.read_text())["other_setting"], 42)
        with self.assertRaises(HTTPError):
            self.post("/api/global-settings", {"layer_transfer": {"parallel": "yes"}})
        self.assertEqual(S.read_global_settings()["layer_transfer"]["format"], "png")

    def test_exclude_alpha_default_and_legacy_global_migration(self):
        self.assertFalse(S.read_global_settings()["layer_transfer"]["opaque_alpha"])
        self.assertTrue(json.loads(S.GLOBAL_SETTINGS.read_text())["layer_transfer"]["exclude_opaque_alpha"])
        for include in (True, False):
            S.GLOBAL_SETTINGS.write_text(json.dumps({"other":42, "layer_transfer":{"opaque_alpha":include}}))
            self.assertEqual(S.read_global_settings()["layer_transfer"]["opaque_alpha"], include)
            stored = json.loads(S.GLOBAL_SETTINGS.read_text())
            self.assertEqual(stored["layer_transfer"]["exclude_opaque_alpha"], not include)
            self.assertNotIn("opaque_alpha", stored["layer_transfer"])
            self.assertEqual(stored["other"], 42)
        for exclude in (True, False):
            settings = self.post("/api/global-settings", {"layer_transfer":{"exclude_opaque_alpha":exclude}})
            self.assertEqual(settings["layer_transfer"]["opaque_alpha"], not exclude)
        with self.assertRaises(HTTPError):
            self.post("/api/global-settings", {"layer_transfer":{"exclude_opaque_alpha":"true"}})

    def test_fetch_and_send_routes_use_separate_dnb_semantics(self):
        sent = []
        flattened=[]

        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": "synthetic.png"}

            def send_skin_bands(inner, layers, mask, doc, *, source_icc=None, mask_placement="folder"):
                self.assertEqual(mask_placement, "folder")
                sent.append((layers[0][1], mask, doc))
                return {"layer": 9, "document": doc["name"]}

            def send_result(inner,pixels,doc,*,source_icc):
                flattened.append((pixels,source_icc))
                return {"layer":10,"document":doc["name"]}

            def status(inner):
                return {"connected": True, "doc": {"index": 1}, "headless": True}

        S.BRIDGE["b"] = FakeBridge()
        self.assertTrue(self.post("/api/pc/status", {})["connected"])
        self.assertIn("pc_doc", self.post("/api/pc/fetch", {}))
        self.assertTrue(self.post("/api/pc/send", PARAMS)["sent"])
        result,icc=S.skin_result(S.parse_params(PARAMS))
        np.testing.assert_array_equal(flattened[0][0],S.FS.rgb16(result))
        self.assertEqual(flattened[0][1],icc)
        self.assertTrue(self.post("/api/pc/send_layers", PARAMS)["sent"])
        self.assertEqual(sent[0][0].dtype, np.uint16)
        self.assertEqual(sent[0][1].dtype, np.uint8)
        self.assertEqual(sent[0][0].shape, (96, 128, 3))
        self.assertEqual(sent[0][1].shape, (96, 128))

    def test_layer_blur_zero_legacy_and_full_resolution_preview_save_send(self):
        sent = []

        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": self.image.name}

            def send_skin_bands(inner, layers, mask, doc, *, source_icc=None, mask_placement="folder"):
                self.assertEqual(mask_placement, "folder")
                sent.append((layers[0][1].copy(), mask.copy()))
                return {"layer": 9, "document": doc["name"]}

        S.BRIDGE["b"] = FakeBridge()
        self.post("/api/pc/fetch", {})
        self.post("/api/run", PARAMS)
        legacy = {key: self.get("/img/" + key)[0] for key in ("layer", "layerm", "result", "mask")}
        self.post("/api/pc/send_layers", PARAMS)
        self.post("/api/run", dict(PARAMS, layer_blur_at_s181=0))
        for key, data in legacy.items():
            self.assertEqual(self.get("/img/" + key)[0], data)
        self.post("/api/pc/send_layers", dict(PARAMS, layer_blur_at_s181=0))
        for old, new in zip(sent[0], sent[1]):
            np.testing.assert_array_equal(old, new)
        p = dict(PARAMS, S=196.08333334, layer_blur_at_s181=2.3)
        self.post("/api/run", dict(p, layer_blur_at_s181=0))
        baseline_mask = self.get("/img/mask")[0]
        with patch.object(D, "blur_softlight_layer", wraps=D.blur_softlight_layer) as blur:
            self.post("/api/run", p)
            source, raw_layer, _, mask, sigma = blur.call_args.args
            self.assertAlmostEqual(sigma, 2.3 * p["S"] / 181)
            expected = cv2.GaussianBlur(raw_layer, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)
        self.assertGreater(float(np.max(np.abs(expected - raw_layer))), 1e-4)
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/layer")[0], np.uint8), 0), S.u8(expected))
        self.assertEqual(self.get("/img/mask")[0], baseline_mask)
        expected_masked = .5 + (expected - .5) * mask
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/layerm")[0], np.uint8), 0), S.u8(expected_masked))
        expected_result = source + (np.clip(D.softlight(source, expected[..., None]), 0, 1) - source) * mask[..., None]
        self.assertEqual(self.get("/img/result")[0], S.encode(S.u8(expected_result), ".jpg", 92, S.STATE["icc"]))
        self.post("/api/pc/send_layers", p)
        np.testing.assert_array_equal(sent[-1][0][..., 0], np.round(expected * 65535).astype(np.uint16))
        np.testing.assert_array_equal(sent[-1][1], S.u8(mask))
        saved = self.post("/api/save", p)
        folder = Path(saved["dir"])
        np.testing.assert_array_equal(cv2.imread(str(folder / "synthetic_3_softlight_layer.png"), cv2.IMREAD_UNCHANGED), sent[-1][0])
        np.testing.assert_array_equal(cv2.imread(str(folder / "synthetic_2_skinmask.png"), 0), sent[-1][1])
        self.post("/api/presets/save", {"name": "Blur", "settings": p})
        self.assertEqual(json.loads(self.get("/api/presets")[0])["presets"]["Blur"]["layer_blur_at_s181"], 2.3)
        for bad in (-.1, 20.1, float("nan")):
            with self.assertRaises(HTTPError):
                self.post("/api/run", dict(p, layer_blur_at_s181=bad))

    def test_layer_blur_preview_scales_sigma_without_changing_mask(self):
        cv2.imwrite(str(self.image), S.u8(cv2.resize(synthetic_image(), (1600, 1200))))
        self.post("/api/load", {"path": str(self.image)})
        p = dict(PARAMS, S=90.5, layer_blur_at_s181=4)
        with patch.object(D, "blur_softlight_layer", wraps=D.blur_softlight_layer) as blur:
            self.post("/api/run", p)
            self.assertEqual(blur.call_args.args[0].shape[:2], (1050, 1400))
            self.assertAlmostEqual(blur.call_args.args[-1], 4 * 90.5 / 181 * 1400 / 1600)
        # Other tools must not invoke this layer-only postprocess.
        with patch.object(D, "blur_softlight_layer", side_effect=AssertionError("D&B only")):
            self.post("/api/run", dict(p, mode="mask"))
            self.post("/api/run", dict(p, mode="frequency"))

    def test_missing_models_are_lazy_and_actionable(self):
        with patch.object(paths, "MODELS", self.root / "no-models"):
            with self.assertRaisesRegex(FileNotFoundError, "DNB_MODELS"):
                estimate_scale.detect(np.zeros((48, 64, 3), np.uint8))
            for module, attribute, fn in ((skin_mask, "_SEG", skin_mask.seg),
                                          (skin_mask_compare, "_SEG", skin_mask_compare.segmenter)):
                with patch.object(module, attribute, None):
                    with self.assertRaisesRegex(FileNotFoundError, "MODELS.md"):
                        fn()

    def test_icc_preserved_in_preview(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        target = self.root / "profile.jpg"
        Image.new("RGB", (128, 96), "tan").save(target, icc_profile=profile)
        self.post("/api/load", {"path": str(target)})
        self.post("/api/run", PARAMS)
        import io
        with Image.open(io.BytesIO(self.get("/img/orig")[0])) as image:
            self.assertEqual(image.info["icc_profile"], profile)

    def test_manual_scale_proposals_are_subject_scoped_and_commit_only_on_apply(self):
        self.post("/api/load", {"path": str(self.image)})
        kp = np.zeros((17, 2))
        sc = np.zeros(17)
        kp[5], kp[7] = [10, 20], [30, 20]
        sc[5], sc[7] = 1, 1
        S.STATE["seen"] = [
            {"kp": kp, "sc": sc, "face_info": None, "face": None, "target": True,
             "cues": [("face_ipd", 30, 0.08), ("iris", 32, 0.12), ("body", 4, 0.2)]},
            {"kp": None, "sc": None, "face_info": None, "face": None, "target": False, "cues": []},
        ]
        data = self.post("/api/landmarks", {})
        self.assertEqual(len(data["subjects"]), 2)
        self.assertEqual(data["subjects"][0]["points"]["l_sho"]["x"], 10)
        req = {"generation": data["generation"], "subject": 0,
               "points": {"l_eye": {"x": 20, "y": 10, "visible": True},
                          "r_eye": {"x": 40, "y": 10, "visible": True}}, "excluded": []}
        proposal = self.post("/api/landmarks/estimate", req)
        names = [c[0] for c in proposal["used"] + proposal["dropped"]]
        self.assertIn("manual_eye_centres_2d", names)
        self.assertNotIn("face_ipd", names)
        self.assertIn("iris", names)
        self.assertLessEqual(names.count("body"), 1)
        self.assertNotEqual(proposal["confidence"], "high")
        self.assertGreaterEqual(proposal["sigma"], 0.15)
        self.assertEqual(S.STATE["landmark_overrides"], {})
        self.assertEqual(S.STATE["seen"][0]["kp"][5, 0], 10)
        self.post("/api/landmarks/estimate", dict(req, apply=True))
        self.assertEqual(self.post("/api/landmarks", {})["subjects"][0]["overrides"]["points"], req["points"])
        self.assertIsNone(self.post("/api/landmarks", {})["subjects"][1]["overrides"])
        empty = self.post("/api/landmarks/estimate", dict(req, subject=1, points={}))
        self.assertIsNone(empty["S"])
        self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(S.STATE["landmark_overrides"], {})
        with self.assertRaises(HTTPError):
            self.post("/api/landmarks/estimate", req)

    def test_manual_segments_missing_degenerate_and_exclusion(self):
        self.post("/api/load", {"path": str(self.image)})
        generation = self.post("/api/landmarks", {})["generation"]
        def point(x, y):
            return {"x": x, "y": y, "visible": True}
        req = {"generation": generation, "subject": 0, "points": {
            "l_sho": point(10, 20), "l_elb": point(30, 20), "l_wri": point(30, 20),
            "r_sho": point(50, 20), "r_elb": point(55, 20)}, "excluded": []}
        result = self.post("/api/landmarks/estimate", req)
        self.assertEqual({row[0] for row in result["segments"]}, {"upper_arm_l", "upper_arm_r", "shoulders"})
        self.assertIn("forearm_l", result["unavailable"])
        self.assertIsNotNone(result["body_spread"])
        expected = estimate_body_scale.estimate(
            np.array([[p["x"], p["y"]] if (p := req["points"].get(n)) else [0, 0]
                      for n in estimate_body_scale.KP]),
            np.array([1 if n in req["points"] else 0 for n in estimate_body_scale.KP]))
        self.assertAlmostEqual(result["S"], expected["S"])
        excluded = self.post("/api/landmarks/estimate", dict(req, excluded=["upper_arm_r"]))
        self.assertNotIn("upper_arm_r", [row[0] for row in excluded["segments"]])
        req["points"]["l_elb"] = {"visible": False}
        missing = self.post("/api/landmarks/estimate", req)
        self.assertNotIn("upper_arm_l", [row[0] for row in missing["segments"]])
        req["points"]["l_sho"] = point(-1, 20)
        with self.assertRaises(HTTPError):
            self.post("/api/landmarks/estimate", req)

    def test_manual_scale_recalculates_all_skin_modes(self):
        self.post("/api/load", {"path": str(self.image)})
        generation = self.post("/api/landmarks", {})["generation"]
        req = {"generation": generation, "subject": 0,
               "points": {"l_eye": {"x": 20, "y": 20, "visible": True},
                          "r_eye": {"x": 80, "y": 20, "visible": True}}, "excluded": [], "apply": True}
        scale = self.post("/api/landmarks/estimate", req)["S"]
        for mode in ("dnb", "mask", "frequency"):
            result = self.post("/api/run", dict(PARAMS, S=scale, mode=mode))
            self.assertEqual(result["stats"]["preview_px"], [128, 96])
            self.assertAlmostEqual(S.STATE["last"]["S"], scale)

    def test_unscaled_detected_subjects_are_available_without_retargeting_mask(self):
        self.patches[4].stop()
        kp = np.zeros((17, 2))
        sc = np.zeros(17)
        sc[5] = 0.9
        kp[5] = (22, 33)
        subjects = [("body", None, None, 0, None, [])]
        with patch.object(skin_mask_compare.C, "build_subjects", return_value=(subjects, [(kp, sc)], None, None, [])):
            self.assertIsNone(skin_mask_compare.pick_target(self.image, S.u8(synthetic_image())))
            target = skin_mask_compare.pick_target(self.image, S.u8(synthetic_image()), include_unscaled=True)
        self.assertIsNone(target["S"])
        self.assertEqual(target["seen"][0]["kp"][5, 0], 22)


class EngineTests(unittest.TestCase):
    def test_brow_perimeters_fill_without_crossed_chain_holes(self):
        chains = [
            ([276, 283, 282, 295, 285], [300, 293, 334, 296, 336], skin_mask.L_BROW),
            ([46, 53, 52, 65, 55], [70, 63, 105, 66, 107], skin_mask.R_BROW),
        ]
        points = np.zeros((478, 3), np.float32)
        for offset, (lower, upper, perimeter) in zip((10, 70), chains):
            self.assertEqual(perimeter, lower + upper[::-1])
            for i, (a, b) in enumerate(zip(lower, upper)):
                points[a, :2] = (offset + 10 * i, 40)
                points[b, :2] = (offset + 10 * i, 20)
            filled = skin_mask_compare.poly_mask((64, 128, 3), points[:, :2], perimeter)
            self.assertTrue(np.all(filled[21:40, offset + 1:offset + 40] > 0),
                            "Brow interior has crossed-chain holes")
        self.assertEqual(skin_mask_compare.L_BROW, skin_mask.L_BROW)
        self.assertEqual(skin_mask_compare.R_BROW, skin_mask.R_BROW)
        face_info = (points, None, 1)
        with patch.object(skin_mask.cv2, "fillPoly", wraps=cv2.fillPoly) as fills:
            mask = skin_mask.feature_mask((64, 128, 3), face_info, 63)
            for call, (_, _, perimeter) in zip(fills.call_args_list[-2:], chains):
                np.testing.assert_array_equal(call.args[1][0], points[perimeter, :2])
        self.assertTrue(np.all(mask[25:36, 15:46] > 0.99))
        last = {"reg": np.zeros((64, 128, 3), np.float32), "k": 1, "off": (0, 0),
                "S": 63, "mreg": None}
        subject = {"face": None, "face_info": face_info, "kp": None, "sc": None, "target": True}
        with patch.dict(S.STATE, last=last, seen=[subject], pbox=None, imgs={}, icc=None), \
                patch.object(S.cv2, "polylines", wraps=cv2.polylines) as outlines:
            S.render_analysis({"detect": True, "skin": False, "structure": False})
            for call, (_, _, perimeter) in zip(outlines.call_args_list[-2:], chains):
                self.assertTrue(call.args[2], "Brow outline must be closed")
                np.testing.assert_array_equal(call.args[1][0], points[perimeter, :2])

    def test_mask_preserves_outside_pixels_and_unmasked_layer_is_valid(self):
        img = synthetic_image()
        mask = np.ones(img.shape[:2], np.float32)
        mask[:, :32] = 0
        layer, after, change, diagnostics, total = D.process(img, 63, [(1.5, 40, 1, 0.5)], mask=mask)
        np.testing.assert_allclose(after[:, :32], img[:, :32], atol=2e-6)
        self.assertTrue(np.isfinite(after).all())
        self.assertTrue(np.isfinite(layer).all())
        self.assertGreaterEqual(float(layer.min()), 0)
        self.assertLessEqual(float(layer.max()), 1)
        np.testing.assert_array_equal(total, mask)
        self.assertGreater(float(np.abs(change[:, 32:]).max()), 0)
        unblurred_layer, unblurred_after = D.blur_softlight_layer(img, layer, after, total, 0)
        self.assertIs(unblurred_layer, layer)
        self.assertIs(unblurred_after, after)
        unchanged_mask = total.copy()
        blurred_layer, blurred_after = D.blur_softlight_layer(img, layer, after, total, 3.2)
        np.testing.assert_array_equal(total, unchanged_mask)
        np.testing.assert_array_equal(blurred_after[:, :32], img[:, :32])
        _, unmasked_after = D.blur_softlight_layer(img, layer, after, None, 3.2)
        np.testing.assert_array_equal(unmasked_after, np.clip(D.softlight(img, blurred_layer[..., None]), 0, 1))

    def test_zero_strength_is_identity(self):
        img = synthetic_image()
        layer, after, change, diagnostics, total = D.process(img, 63, [(1.5, 40, 1, 0.5)], strength=0)
        np.testing.assert_allclose(after, img, atol=2e-6)
        np.testing.assert_allclose(layer, 0.5, atol=2e-6)
        self.assertIsNone(total)

    def test_entrypoints_outside_checkout_without_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SHPC_DATA=tmp + "/crop", DNB_DATA=tmp + "/dnb",
                       DNB_MODELS=tmp + "/models", SHP_PC_EXCHANGE=tmp + "/exchange", SHP_PC_TOKEN_FILE=tmp + "/token")
            for module in ("crop.server", "dnb.ui_server", "dnb.dodge_burn"):
                result = subprocess.run([sys.executable, "-m", "shp_photocraft_tools.modules." + module, "--help"],
                                        env=env, cwd=tmp, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_servers_print_bound_port_and_serve_without_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SHPC_DATA=tmp + "/crop", DNB_DATA=tmp + "/dnb",
                       DNB_MODELS=tmp + "/models", SHP_PC_EXCHANGE=tmp + "/exchange", SHP_PC_TOKEN_FILE=tmp + "/token")
            for module in ("crop.server", "dnb.ui_server"):
                process = subprocess.Popen([sys.executable, "-u", "-m", "shp_photocraft_tools.modules." + module, "--port", "0"],
                                           env=env, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                lines = queue.Queue()

                def read_output():
                    for line in process.stdout:
                        lines.put(line)

                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                try:
                    output = ""
                    for attempt in range(30):
                        line = lines.get(timeout=30)
                        output += line
                        match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
                        if match:
                            break
                    else:
                        self.fail(output)
                    self.assertGreater(int(match[1]), 0)
                    with urlopen(match[0], timeout=10) as response:
                        self.assertIn(b"SHP PhotoCraft", response.read())
                    with urlopen(match[0] + "/static/shp-ui.css", timeout=10) as response:
                        self.assertEqual(response.read(), stylesheet())
                finally:
                    process.terminate()
                    process.wait(timeout=10)
                    reader.join(timeout=10)
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
