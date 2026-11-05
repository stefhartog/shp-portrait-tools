"""FLUX.1 Fill graph, per-method persistence and isolated Comfy API regression."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.crop import expand as X
from shp_photocraft_tools.modules.crop import server as S
from test_expand import StubComfy, stub_server


class FillSettingsTests(unittest.TestCase):
    def test_existing_settings_and_legacy_presets_stay_flux2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            legacy = {key: value for key, value in X.DEFAULTS.items()
                      if not key.startswith("fill_") and key != "method"}
            legacy.update(feathering=160, steps=17, guidance=4.5, lora=False)
            path.write_text(json.dumps(legacy))
            settings = X.load_settings(path)
            self.assertEqual(settings["method"], "flux2")
            for key, value in legacy.items():
                self.assertEqual(settings[key], value)
            settings = X.fill_settings(path, {"method": "flux1_fill", "fill_steps": 32})
            self.assertEqual(settings["fill_feathering"], 64)
            self.assertEqual(settings["fill_guidance"], 30.0)
            self.assertEqual(settings["feathering"], 160)
            self.assertEqual(X.fill_settings(path, {"method": "flux2"})["steps"], 17)
            presets = Path(tmp) / "presets.json"
            presets.write_text(json.dumps({"presets": {"legacy": legacy}, "last": None}))
            with patch.object(S, "SETTINGS_FILE", path), patch.object(S, "PRESETS_FILE", presets), \
                    patch.object(S, "running", return_value=False):
                X.fill_settings(path, {"method": "flux1_fill", "fill_steps": 32})
                S.api_preset_save({"name": "fill"})
                S.api_preset_load({"name": "legacy"})
                self.assertEqual(X.load_settings(path)["method"], "flux2")
                self.assertEqual(X.load_settings(path)["feathering"], 160)
                S.api_preset_load({"name": "fill"})
                self.assertEqual(X.load_settings(path)["method"], "flux1_fill")
                self.assertEqual(X.load_settings(path)["fill_steps"], 32)
                self.assertEqual(X.load_settings(path)["steps"], 17)

    def test_validation_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            X.fill_settings(path, {"method": "flux1_fill", "fill_guidance": 30})
            before = path.read_bytes()
            for values in ({"method": "nunchaku"}, {"fill_guidance": 101}, {"fill_steps": 0},
                           {"fill_feathering": -1}, {"fill_megapixels": 0}, {"fill_unet": ""},
                           {"fill_clip_device": "cuda"}):
                with self.assertRaises(X.ExpandError):
                    X.fill_settings(path, values)
                self.assertEqual(path.read_bytes(), before)


class FillGraphTests(unittest.TestCase):
    def test_flux2_lanczos_skips_neural_upscale(self):
        settings = dict(X.DEFAULTS, upscale="lanczos")
        for seam in (True, False):
            graph = X.build_graph("p.png", (0, 10, 0, 0), "", 7, dict(settings, seam_correction=seam))
            self.assertNotIn("22", graph)
            self.assertNotIn("23", graph)
            self.assertEqual(graph["25"]["inputs"]["image"], ["21", 0])
            self.assertEqual(graph["25"]["inputs"]["upscale_method"], "lanczos")
            self.assertEqual(graph["26"]["inputs"]["destination"], ["27" if seam else "25", 0])
            for node in graph.values():
                for value in node["inputs"].values():
                    if isinstance(value, list):
                        self.assertIn(value[0], graph)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(X.fill_settings(path, {"upscale": "lanczos"})["upscale"], "lanczos")
            self.assertEqual(X.load_settings(path)["upscale"], "lanczos")

    def test_optional_neural_upscale(self):
        settings = dict(X.DEFAULTS, method="flux1_fill", fill_upscale=X.DEFAULTS["upscale"])
        graph = X.build_graph("p.png", (0, 10, 0, 0), "", 7, settings)
        self.assertEqual(graph["22"]["inputs"]["model_name"], X.DEFAULTS["upscale"])
        self.assertEqual(graph["23"]["inputs"], {"upscale_model": ["22", 0], "image": ["21", 0]})
        self.assertEqual(graph["25"]["inputs"]["image"], ["23", 0])
        self.assertEqual(graph["25"]["inputs"]["upscale_method"], "lanczos")
        self.assertEqual(graph["27"]["inputs"]["generated"], ["25", 0])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            saved = X.fill_settings(path, {"fill_upscale": X.DEFAULTS["upscale"]})
            self.assertEqual(X.load_settings(path), saved)
            self.assertEqual(X.fill_settings(path, {"fill_upscale": "lanczos"})["upscale"],
                             X.DEFAULTS["upscale"])

    def test_selectable_text_encoding_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            for device in ("default", "cpu"):
                settings = X.fill_settings(path, {"method": "flux1_fill", "fill_clip_device": device})
                self.assertEqual(X.load_settings(path)["fill_clip_device"], device)
                graph = X.build_graph("p.png", (0, 10, 0, 0), "", 7, settings)
                self.assertEqual(graph["9"]["inputs"]["device"], device)
                schema = FillClientTests.fill_schema()
                with patch.object(X.Comfy, "object_info", return_value=schema):
                    result = X.status(settings)
                self.assertEqual(result["ok"], device == "cpu")
                if device == "default":
                    self.assertIn("device 'default'", result["message"])
                schema["DualCLIPLoader"]["input"]["optional"]["device"] = [["cpu", "default"]]
                with patch.object(X.Comfy, "object_info", return_value=schema):
                    self.assertTrue(X.status(settings)["ok"])

    def test_corrected_native_fill_path(self):
        settings = dict(X.DEFAULTS, method="flux1_fill", lora=True, seam_correction=False,
                        feathering=160, steps=8, guidance=3.5)
        graph = X.build_graph("source.png", (0, 100, 25, 50), "  blue sky  ", 42, settings)
        for node in graph.values():
            for value in node["inputs"].values():
                if isinstance(value, list):
                    self.assertIn(value[0], graph)
        self.assertEqual(graph["2"]["inputs"]["feathering"], 64)
        self.assertEqual(graph["3"]["inputs"]["megapixels"], 0.5)
        self.assertEqual(graph["3"]["inputs"]["resolution_steps"], 16)
        self.assertEqual(graph["8"]["inputs"], {"unet_name": "flux1-fill-dev.safetensors",
                                               "weight_dtype": "fp8_e4m3fn"})
        self.assertEqual(graph["9"]["inputs"], {"clip_name1": X.FILL_CLIP_L, "clip_name2": X.FILL_T5,
                                               "type": "flux", "device": "cpu"})
        self.assertEqual(graph["10"]["inputs"]["vae_name"], "ae.safetensors")
        self.assertEqual(graph["11"]["inputs"]["text"], X.FILL_PROMPT + " The new area should show: blue sky")
        self.assertEqual(graph["12"]["inputs"]["guidance"], 30)
        self.assertEqual(graph["13"]["inputs"]["conditioning"], ["11", 0])
        self.assertEqual(graph["14"]["inputs"], {
            "positive": ["12", 0], "negative": ["13", 0], "vae": ["10", 0],
            "pixels": ["3", 0], "mask": ["7", 0], "noise_mask": False})
        self.assertEqual(graph["6"]["inputs"], {
            "image": ["5", 0], "upscale_method": "bilinear", "width": ["4", 0],
            "height": ["4", 1], "crop": "disabled"})
        self.assertEqual(graph["16"]["inputs"], {
            "model": ["8", 0], "seed": 42, "steps": 28, "cfg": 1.0, "sampler_name": "euler",
            "scheduler": "simple", "positive": ["14", 0], "negative": ["14", 1],
            "latent_image": ["14", 2], "denoise": 1.0})
        self.assertEqual(graph["21"]["inputs"], {
            "target": ["18", 0], "reference": ["3", 0], "exclude_mask": ["7", 0], "strength": 1.0})
        self.assertEqual(graph["25"]["inputs"], {
            "image": ["21", 0], "upscale_method": "lanczos", "width": ["24", 0],
            "height": ["24", 1], "crop": "disabled"})
        self.assertEqual(graph["27"]["inputs"], {
            "original": ["2", 0], "generated": ["25", 0], "new_area": ["2", 1],
            "band": 128, "strength": 1.0, "max_offset": 24, "min_offset": 1.5, "max_spread": 10})
        self.assertEqual(graph["26"]["inputs"], {
            "destination": ["27", 0], "source": ["2", 0], "x": 0, "y": 0,
            "resize_source": False, "mask": ["28", 0]})
        self.assertEqual(graph["29"]["inputs"]["mask"], ["2", 1])
        self.assertEqual([n["class_type"] for n in graph.values()].count("KSampler"), 1)
        for forbidden in ("UnetLoaderGGUF", "UpscaleModelLoader", "ImageUpscaleWithModel",
                          "LoraLoaderModelOnly", "ReferenceLatent", "SaveImage"):
            self.assertNotIn(forbidden, [n["class_type"] for n in graph.values()])
        empty = X.build_graph("source.png", (0, 100, 0, 0), "  ", 1, settings)
        self.assertEqual(empty["11"]["inputs"]["text"], X.FILL_PROMPT)

    def test_fill_values_do_not_change_flux2_graph(self):
        legacy = {k: v for k, v in X.DEFAULTS.items() if not k.startswith("fill_") and k != "method"}
        graph = X.build_graph("p.png", (0, 10, 0, 10), "", 7, legacy)
        mixed = dict(X.DEFAULTS, fill_steps=49, fill_feathering=8, fill_guidance=80)
        self.assertEqual(graph, X.build_graph("p.png", (0, 10, 0, 10), "", 7, mixed))
        with self.assertRaises(X.ExpandError):
            X.build_graph("p.png", (0, 10, 0, 0), "", 7, dict(mixed, method="unknown"))


class FillClientTests(unittest.TestCase):
    def setUp(self):
        self.server, self.url = stub_server()
        self.settings = dict(X.DEFAULTS, method="flux1_fill")
        self.state = StubComfy.state

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_method_specific_readiness(self):
        self.state["drop"] = list(set(X.REQUIRED_NODES) - set(X.FILL_REQUIRED_NODES))
        self.assertTrue(X.status(self.settings, self.url)["ok"])
        self.assertFalse(X.status(X.DEFAULTS, self.url)["ok"])
        self.state["drop"] = list(set(X.FILL_REQUIRED_NODES) - set(X.REQUIRED_NODES))
        self.assertTrue(X.status(X.DEFAULTS, self.url)["ok"])
        missing = X.status(self.settings, self.url)
        self.assertFalse(missing["ok"])
        self.assertIn("UNETLoader", missing["message"])
        self.state["drop"] = []
        missing = X.status(dict(self.settings, fill_unet="missing.safetensors"), self.url)
        self.assertFalse(missing["ok"])
        self.assertIn("FLUX.1 Fill model 'missing.safetensors'", missing["message"])
        for attr, expected in (("FILL_CLIP_L", "CLIP-L"), ("FILL_T5", "T5 encoder"), ("FILL_VAE", "Fill VAE")):
            with patch.object(X.Comfy, "object_info", return_value=self.fill_schema()), \
                    patch.object(X, attr, "missing.safetensors"):
                self.assertIn(expected, X.status(self.settings, self.url)["message"])

    def test_upscale_readiness_only_when_selected(self):
        self.assertTrue(X.status(self.settings, self.url)["ok"])
        neural = dict(self.settings, fill_upscale=X.DEFAULTS["upscale"])
        self.assertTrue(X.status(neural, self.url)["ok"])
        self.assertIn("Fill upscale model", X.status(dict(neural, fill_upscale="missing.pth"),
                                                    self.url)["message"])
        self.state["drop"] = ["UpscaleModelLoader", "ImageUpscaleWithModel"]
        self.assertTrue(X.status(self.settings, self.url)["ok"])
        missing = X.status(neural, self.url)
        self.assertFalse(missing["ok"])
        self.assertIn("UpscaleModelLoader", missing["message"])

    def test_flux2_lanczos_needs_no_upscale_nodes_or_weights(self):
        self.state["drop"] = ["UpscaleModelLoader", "ImageUpscaleWithModel"]
        result = X.status(dict(X.DEFAULTS, upscale="lanczos"), self.url)
        self.assertTrue(result["ok"], result["message"])
        self.assertFalse(X.status(X.DEFAULTS, self.url)["ok"])

    @staticmethod
    def fill_schema():
        return {
            **{name: {"input": {"required": {}}} for name in X.FILL_REQUIRED_NODES},
            "UNETLoader": {"input": {"required": {
                "unet_name": [[X.DEFAULTS["fill_unet"]]], "weight_dtype": [["fp8_e4m3fn"]]}}},
            "DualCLIPLoader": {"input": {"required": {
                "clip_name1": [[X.FILL_CLIP_L]], "clip_name2": [[X.FILL_T5]], "type": [["flux"]]},
                "optional": {"device": [["cpu"]]}}},
            "VAELoader": {"input": {"required": {"vae_name": [[X.FILL_VAE]]}}},
        }

    def test_readiness_checks_loader_capabilities(self):
        for node, section, field, value in (("UNETLoader", "required", "weight_dtype", "fp8_e4m3fn"),
                                            ("DualCLIPLoader", "optional", "device", "cpu")):
            schema = self.fill_schema()
            schema[node]["input"][section][field] = [["unsupported"]]
            with patch.object(X.Comfy, "object_info", return_value=schema):
                result = X.status(self.settings)
            self.assertFalse(result["ok"])
            self.assertIn(value, result["message"])

    def test_variations_crop_upload_alpha_and_cancellation(self):
        picture = Image.new("RGB", (300, 200), (30, 120, 200))
        job = X.Job()
        results = X.expand_variations(picture, (-100, 50, -100, 50), "sky", job, 42,
                                      self.settings, 2, self.url)
        self.assertEqual(self.state["uploaded"], (100, 200))
        self.assertEqual(self.state["seeds"], [42, 43])
        self.assertEqual(results[0]["canvas"].size, (100, 300))
        self.assertEqual(results[0]["margins"], (-100, 50, -100, 50))
        alpha = np.asarray(results[0]["layer"].getchannel("A"))
        self.assertEqual(alpha[0, 0], 255)
        self.assertEqual(alpha[100, 50], 0)
        self.assertEqual(job.results, results)
        self.assertIs(job.result, results[0])
        self.state["hang"] = True
        job = X.Job()
        timer = threading.Timer(0.5, lambda: setattr(job, "cancel", True))
        timer.start()
        try:
            with self.assertRaises(X.Cancelled):
                X.expand_variations(picture, (0, 50, 0, 50), "", job, 1, self.settings, 1, self.url)
        finally:
            timer.join()
        self.assertTrue(self.state["interrupted"])
        self.assertEqual(self.state["deleted"], {"delete": [self.state["prompt_id"]]})
        self.assertEqual(job.results, [])

    def test_execution_failure_surfaces(self):
        self.state["fail"] = True
        with self.assertRaisesRegex(X.ExpandError, "out of memory"):
            X.expand_variations(Image.new("RGB", (200, 200)), (0, 50, 0, 50), "", X.Job(), 1,
                                self.settings, 1, self.url)
