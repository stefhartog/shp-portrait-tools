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
from shp_photocraft_tools.modules.dnb import estimate_scale, paths, skin_mask, skin_mask_compare
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
        request = Request(self.base + route, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_assets_and_unknown_routes(self):
        html, headers = self.get("/")
        self.assertIn(b"<title>SHP PhotoCraft D&amp;B</title>", html)
        self.assertLess(html.index(b"/static/shp-ui.css"), html.index(b"<style>"))
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

    def test_fetch_and_send_routes_use_separate_dnb_semantics(self):
        sent = []

        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": "synthetic.png"}

            def send(inner, layer, mask, doc):
                sent.append((layer, mask, doc))
                return {"layer": 9, "document": doc["name"]}

            def status(inner):
                return {"connected": True, "doc": {"index": 1}, "headless": True}

        S.BRIDGE["b"] = FakeBridge()
        self.assertTrue(self.post("/api/pc/status", {})["connected"])
        self.assertIn("pc_doc", self.post("/api/pc/fetch", {}))
        self.assertTrue(self.post("/api/pc/send", PARAMS)["sent"])
        self.assertEqual(sent[0][0].dtype, np.uint16)
        self.assertEqual(sent[0][1].dtype, np.uint8)
        self.assertEqual(sent[0][0].shape, (96, 128, 3))
        self.assertEqual(sent[0][1].shape, (96, 128))

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


class EngineTests(unittest.TestCase):
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
