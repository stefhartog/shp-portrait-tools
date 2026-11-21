"""CPU fallback and opt-in numerical parity for the full-resolution CUDA renderer."""

import os
import unittest
from unittest.mock import Mock, patch

import numpy as np

from shp_portrait_tools.modules.dnb import cuda_bands, dodge_burn as D, processing_service, progress, skin_mask


PARAMS = {
    "S": 700, "bands": [(.3, 12., 1., .5), (12., 50., .5, .5)], "strength": 2.,
    "second_band": D.second_band_settings({"on": True, "invert": True}),
    "edge": .04, "tau": 1., "max_ev": 1., "layer_blur_at_s181": 0.,
}


class CudaFallbackTests(unittest.TestCase):
    def test_mask_refinement_falls_back_to_cpu_with_reason(self):
        image = np.full((32, 32, 3), 127, np.uint8)
        p = np.linspace(.2, .8, 1024, dtype=np.float32).reshape(32, 32)
        expected = skin_mask.refine(p, image)
        token = "test-mask-cuda-fallback"
        with patch.object(cuda_bands, "refine_skin", side_effect=cuda_bands.CudaUnavailable("low VRAM")), \
             progress.operation(token):
            actual = progress.detailed_call("source mask", skin_mask._refine_crop, p, image, "crop", "cuda")
        np.testing.assert_array_equal(actual, expected)
        details = progress.snapshot(token, consume=True)["steps"][0]["details"]
        self.assertIn("CPU fallback: low VRAM", details[1]["label"])

    def test_device_or_memory_failure_uses_cpu_and_reports_reason(self):
        image = np.full((32, 32, 3), .5, np.float32)
        params = dict(PARAMS, S=63)
        expected = processing_service.cached_dnb_band(params, image, 2, None, (None, None))
        token = "test-dnb-cuda-fallback"
        recheck = Mock()
        with patch.object(cuda_bands, "render_band", side_effect=cuda_bands.CudaUnavailable("low VRAM")), \
             self.assertLogs(processing_service.__name__, "WARNING") as logs, progress.operation(token):
            actual = processing_service.cached_dnb_band(
                params, image, 2, None, (None, None), backend="cuda", before_fallback=recheck)
        np.testing.assert_array_equal(actual, expected)
        recheck.assert_called_once_with()
        self.assertIn("low VRAM", logs.output[0])
        steps = progress.snapshot(token, consume=True)["steps"]
        self.assertEqual(steps[0]["error"], "low VRAM")
        self.assertIn("CPU", steps[1]["label"])

    def test_programming_errors_are_not_reported_as_device_fallback(self):
        with patch.object(cuda_bands, "render_band", side_effect=ValueError("test bug")), \
             patch.object(D, "band_stack") as cpu:
            with self.assertRaisesRegex(ValueError, "test bug"):
                processing_service.cached_dnb_band(PARAMS, None, 1, None, (None, None), backend="cuda")
        cpu.assert_not_called()


@unittest.skipUnless(os.environ.get("SHP_TEST_DNB_CUDA") == "1", "requires CUDA 13 test device")
class CudaParityTests(unittest.TestCase):
    def test_wide_feather_keeps_full_radius_without_cpu_fallback(self):
        from shp_portrait_tools.modules.dnb import ui_server as U

        h, w = 151, 313
        image = np.full((h, w, 3), .45, np.float32)
        base = np.ones((h, w), np.float32)
        base[:, :60] = 0
        base[-16:, -20:] = 0
        masks = {"A": base}
        for inward in (True, False):
            with self.subTest(inward=inward):
                p = U.parse_params({"mode": "mask", "S": 630, "tone": {"on": False},
                                    "mask": {"on": True, "features": False, "colour": False,
                                             "feather": 5.2, "inward": inward}})
                expected = U.base_skin_mask(image, masks, p, p["S"])
                with patch.object(U, "STATE", dict(U.STATE, img=image, source_img=image, masks=masks,
                                                   full_mask_cache=None, landmark_generation=318)), \
                     patch.object(U, "SKIN_MASK_BACKEND", "cuda"), \
                     patch.object(U, "base_skin_mask", side_effect=AssertionError("unexpected CPU fallback")), \
                     progress.operation("test-wide-feather-cache"):
                    timing = progress.cache_timing()
                    actual, structure = U.cached_final_skin_mask(p)
                    step, = timing()["steps"]
                progress.snapshot("test-wide-feather-cache", consume=True)
                self.assertIsNone(structure)
                self.assertTrue(any("Feather (CUDA" in d["label"] and d["error"] is None
                                    for d in step["details"]))
                self.assertGreater(float(np.ptp(actual)), .3)
                np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-5)
                if inward:
                    self.assertTrue(np.all(actual <= base))

    def test_selected_colour_and_feather_match_cpu_cache_mask(self):
        from shp_portrait_tools.modules.dnb import ui_server as U

        h, w = 137, 199
        image = np.ascontiguousarray(self.stress_image()[:h, :w])
        image[42:60, :w//2] = (.38, .36, .86)
        image[42:60, w//2:] = (.36, .38, .86)
        base = np.zeros((h, w), np.float32)
        base[9:-9, 12:-12] = .94
        masks = {"A": base}
        for sat, smoothing, inward, use_base in ((.57, 0., True, True), (.57, 1.3, False, True),
                                               (.02, 1.3, True, False)):
            with self.subTest(sat=sat, smoothing=smoothing, inward=inward, use_base=use_base):
                p = U.parse_params({"mode":"mask", "S":181, "tone":{"on":False},
                                    "mask":{"on":use_base, "features":False, "colour":False,
                                            "inward":inward, "feather":.82},
                                    "color":{"on":True, "hue":358., "saturation":sat, "hue_range":22.,
                                             "saturation_range":.29, "smooth_mm":smoothing,
                                             "blur_mm":.65, "strength":.89}})
                expected = U.base_skin_mask(image, masks, p, p["S"])
                with patch.object(U, "STATE", dict(U.STATE, img=image, source_img=image, masks=masks,
                                                   full_mask_cache=None, landmark_generation=281)), \
                     patch.object(U, "SKIN_MASK_BACKEND", "cuda"), \
                     patch.object(D, "color_range_mask", side_effect=AssertionError("unexpected CPU fallback")), \
                     patch.object(cuda_bands, "render_final_mask", wraps=cuda_bands.render_final_mask) as run:
                    actual, structure = U.cached_final_skin_mask(p)
                    again, _ = U.cached_final_skin_mask(p)
                run.assert_called_once()
                self.assertIsNone(structure)
                self.assertGreater(float(np.ptp(actual)), .1)
                np.testing.assert_array_equal(actual, again)
                np.testing.assert_allclose(actual, expected, rtol=0, atol=8e-5)
                if use_base and inward:
                    self.assertTrue(np.all(actual <= base))

    def test_final_mask_tone_and_structure_parity(self):
        image = np.ascontiguousarray(self.stress_image()[:100, :127])
        image[50:, 60:65] = .09
        image[50:, 65:70] = .88
        base = np.full(image.shape[:2], .61, np.float32)
        for mode in ("srgb", "adobe"):
            for tone, protect, prepared in ((True, .8, base), (False, 1., None), (True, 0., None)):
                with self.subTest(mode=mode, tone=tone, protect=protect), D.colour_mode(mode):
                    args = (image, 362, prepared, (.1, .24) if tone else None,
                            (.74, .94) if tone else None, .8, protect, 4., .03)
                    expected = D.combined_mask(*args)
                    actual = cuda_bands.render_final_mask(*args)
                    for got, want in zip(actual, expected):
                        if want is None:
                            self.assertIsNone(got)
                        else:
                            self.assertTrue(np.isfinite(got).all())
                            np.testing.assert_allclose(got, want, atol=.002, rtol=0)

    def test_skin_mask_components_on_cuda_with_and_without_face(self):
        image = np.rint(self.stress_image() * 255).astype(np.uint8)
        rng = np.random.default_rng(42)
        face = (rng.uniform((190, 130), (240, 180), (478, 2)), None, .9)

        class Model:
            def run(self, *_):
                logits = np.zeros((1, 256, 256, 6), np.float32)
                logits[0, 60:196, 60:196, 2] = 9
                return [logits]

        with patch.object(skin_mask, "_SEG", (Model(), "image")):
            for mode, info, optional in (("srgb", None, False), ("adobe", face, True)):
                with self.subTest(mode=mode), D.colour_mode(mode):
                    args = image, info, (0, 0, image.shape[1], image.shape[0]), 181
                    kw = dict(use_features=optional, use_colour=optional, use_nostrils=optional)
                    expected = skin_mask.skin_mask(*args, **kw)
                    with patch.object(skin_mask, "refine", side_effect=AssertionError("unexpected CPU fallback")):
                        actual = skin_mask.skin_mask(*args, **kw, backend="cuda")
                    self.assertEqual(expected["rect"], actual["rect"])
                    for key in ("skin", "A", "features", "colour"):
                        np.testing.assert_allclose(actual[key], expected[key], rtol=0, atol=1e-4)

    @staticmethod
    def stress_image():
        h, w = 333, 487
        image = np.random.default_rng(42).random((h, w, 3), dtype=np.float32)
        image[:100] = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
        image[50:100] **= 6
        image[-50:] = 1 - image[-50:] ** 6
        return image

    def test_near_white_runs_on_cuda_at_zero_and_weak_blur(self):
        image = self.stress_image()
        for mode in ("srgb", "adobe"):
            for sigma in (0, .25, 3.1875):
                with self.subTest(mode=mode, sigma=sigma), D.colour_mode(mode):
                    self.assertGreater(float(D.luminance_linear(image).max()), .98)
                    params = dict(PARAMS, layer_blur_at_s181=sigma * 181 / PARAMS["S"])
                    expected = processing_service.cached_dnb_band(params, image, 1, None, (None, None))
                    with patch.object(D, "band_stack", side_effect=AssertionError("unexpected CPU fallback")):
                        actual = processing_service.cached_dnb_band(params, image, 1, None, (None, None),
                                                                    backend="cuda")
                    def composite(layer):
                        gray = np.rint(layer * 65535) / 65535
                        return np.rint(D.softlight(image, gray[..., None]) * 65535)

                    np.testing.assert_allclose(composite(expected), composite(actual), atol=2, rtol=0)

    def test_layer_and_rgb16_composite_parity(self):
        stress = self.stress_image()
        h, w = stress.shape[:2]
        mask = np.broadcast_to(np.linspace(0, 1, w, dtype=np.float32), (h, w)).copy()
        for mode in ("srgb", "adobe"):
            for base in (None, mask, np.zeros_like(mask)):
                for sigma in (0, 3.1875):
                    with self.subTest(mode=mode, mask=base is not None, sigma=sigma), D.colour_mode(mode):
                        image = np.minimum(stress, np.float32(.99))
                        params = dict(PARAMS, layer_blur_at_s181=sigma * 181 / PARAMS["S"])
                        cpu = [processing_service.cached_dnb_band(params, image, i, base, (mask, None))
                               for i in (1, 2)]
                        with patch.object(D, "band_stack", side_effect=AssertionError("unexpected CPU fallback")):
                            gpu = [processing_service.cached_dnb_band(params, image, i, base, (mask, None),
                                                                      backend="cuda")
                                   for i in (1, 2)]
                        for a, b in zip(cpu, gpu):
                            # The inversion is ill-conditioned near white at zero blur.
                            np.testing.assert_allclose(a, b, atol=6e-4 if sigma == 0 else 1e-5, rtol=0)

                        def composite(bands):
                            out = image
                            for band in bands:
                                gray = np.round(band * 65535) / 65535
                                after = D.softlight(out, gray[..., None])
                                out = out + (after - out) * mask[..., None]
                            return np.round(out * 65535)

                        np.testing.assert_allclose(composite(cpu), composite(gpu), atol=2, rtol=0)
