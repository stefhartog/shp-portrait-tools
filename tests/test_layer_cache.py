"""Bounded full-resolution component caching and idle preparation."""

import unittest
from unittest.mock import patch
from contextlib import nullcontext
from pathlib import Path
import tempfile
import threading
import time
import json
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import cv2

from shp_photocraft_tools.modules.dnb import ui_server as U, frequency_separation as FS, dodge_burn as D, progress
from shp_photocraft_tools.modules.dnb.layer_cache import LayerCache
from shp_photocraft_tools.modules.dnb.mask_precache import MaskPrecache
from test_dnb import PARAMS


class LayerCacheTests(unittest.TestCase):
    def test_limit_lru_readonly_and_memory_pressure(self):
        memory = [10_000]
        cache = LayerCache(limit=32, memory=lambda: memory[0])
        a = np.zeros(4, np.float32)
        b = np.ones(4, np.float32)
        c = np.full(4, 2, np.float32)
        self.assertTrue(cache.put("a", (a,), 100))
        self.assertTrue(cache.put("b", (b,), 100))
        self.assertFalse(a.flags.writeable)
        self.assertIs(cache.get("a", 100)[0], a)
        self.assertTrue(cache.put("c", (c,), 100))
        self.assertIsNone(cache.get("b", 100))
        self.assertEqual(cache.bytes, 32)
        self.assertFalse(cache.put("large", (np.zeros(9, np.float32),), 100))
        self.assertFalse(cache.put("view", (a[:2],), 100))
        memory[0] = 99
        self.assertIsNone(cache.get("a", 100))
        self.assertEqual(cache.bytes, 0)
        self.assertFalse(cache.put("c", (c,), 100))
        cache.memory = lambda: None
        self.assertFalse(cache.put("c", (c,), 0))

    def test_write_rejection_reason_uses_admission_read_and_distinguishes_capacity_and_layout(self):
        readings = iter([99, 100])
        cache = LayerCache(limit=32, memory=lambda: next(readings))
        stored, reason = cache.put_with_reason("a", (np.zeros(4, np.float32),), 100)
        self.assertFalse(stored)
        self.assertIn("99 bytes", reason)
        self.assertIn("100 bytes", reason)
        self.assertTrue(cache.put("a", (np.zeros(4, np.float32),), 100))
        cache.memory = lambda: None
        _, reason = cache.put_with_reason("b", (np.zeros(4, np.float32),), 100)
        self.assertIn("could not be measured", reason)
        self.assertIn("100 bytes", reason)
        _, reason = cache.put_with_reason("large", (np.zeros(9, np.float32),), 100)
        self.assertIn("cache limit", reason)
        self.assertNotIn("RAM", reason)
        _, reason = cache.put_with_reason("view", (np.zeros(4, np.float32)[:2],), 100)
        self.assertIn("layout", reason)
        self.assertNotIn("RAM", reason)


class LayerSendCacheTests(unittest.TestCase):
    def setUp(self):
        source = np.random.default_rng(42).uniform(.15, .85, (20, 24, 3)).astype(np.float32)
        self.state = dict(U.STATE, img=source, source_img=source, masks=None, source_opaque=True,
                          skintone_error=None, landmark_generation=1, path="synthetic.png",
                          icc=U.SRGB_ICC, source_icc=U.SRGB_ICC, source_kind="photocraft",
                          full_mask_cache=None, full_mask_status_entry=None, pose=None)
        self.calls = []
        test = self

        class FakeBridge:
            def fetch(self, dest, *, capture_format):
                assert capture_format == "raw"
                return test.capture, test.document

            def send_skintone(self, layers, settings, mask, doc, icc, **kwargs):
                result = dict(layers)
                if "blur_future" in kwargs:
                    result["blur"] = kwargs["blur_future"].result()
                test.calls.append(("fs", {k: v.copy() for k, v in result.items()}, kwargs))
                return {"document": "synthetic"}

            def send_skin_bands(self, layers, mask, doc, **kwargs):
                test.calls.append(("bands", [(i, a.copy()) for i, a in layers], kwargs))
                return {"document": "synthetic"}

        for item in (patch.object(U, "STATE", self.state),
                     patch.object(U, "BRIDGE", {"doc": {"index": 1}, "b": None}),
                     patch.object(U.CACHE_COORDINATOR, "layer_records", {}),
                     patch.object(U.CACHE_COORDINATOR, "mask_timing", None),
                     patch.object(U.CACHE_COORDINATOR, "layer_timings", None),
                     patch.object(U.CACHE_COORDINATOR, "layer_active", {"mode": None, "stamp": None, "generation": None, "tabs": {}, "budget_note": None}),
                     patch.dict(U.EDITOR, selected="photocraft"),
                     patch.object(U, "bridge", return_value=FakeBridge()),
                     patch.object(U, "read_global_settings", return_value={"mask_placement": "folder"}),
                     patch.object(U, "LAYER_CACHE", LayerCache(limit=1024**2, memory=lambda: 100 * 1024**3))):
            item.start()
            self.addCleanup(item.stop)

    def test_fs_and_blur_reuse_preserves_pixels_and_invalidation(self):
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"radius": 2, "blur_radius": 3},
                            "skintone": {"on": False}})
        mask = U.full_skin_mask(p)
        baseline = U.skintone_layers(self.state["source_img"], mask, p, native=True)
        with patch.object(FS, "split", wraps=FS.split) as split, \
             patch.object(FS, "skin_filter", wraps=FS.skin_filter) as blur:
            U.send_skintone_parallel(p)
            changed = {**p, "frequency": {**p["frequency"], "blur_strength": 40}}
            U.send_skintone_parallel(changed)
            self.assertEqual(split.call_count, 1)
            self.assertEqual(blur.call_count, 2)  # split's all-image filter and skin-aware Blur
            for name in ("color", "detail", "blur"):
                np.testing.assert_array_equal(self.calls[0][1][name], baseline[name])
                np.testing.assert_array_equal(self.calls[1][1][name], baseline[name])
            changed["frequency"]["blur_radius"] = 4
            U.send_skintone_parallel(changed)
            self.assertEqual(split.call_count, 1)
            self.assertEqual(blur.call_count, 3)
            self.state["source_img"][0, 0, 0] += .01
            U.send_skintone_parallel(changed)
            self.assertEqual(split.call_count, 2)

    def test_deferred_blur_uses_split_key_and_never_reuses_other_sources(self):
        class Future:
            def __init__(self, fn, args):
                self.fn, self.args = fn, args
                self.value = None
                self.done = False

            def result(self):
                if not self.done:
                    self.value = self.fn(*self.args)
                    self.done = True
                return self.value

            def cancel(self):
                return False

        class DeferredExecutor:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, fn, *args):
                return Future(fn, args)

        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"radius": 2, "blur_radius": 3},
                            "skintone": {"on": False}})
        with patch.object(U.processing_service, "ThreadPoolExecutor", DeferredExecutor), \
             patch.object(FS, "skin_filter", wraps=FS.skin_filter) as filtered:
            U.send_skintone_parallel(p)
            U.send_skintone_parallel(p)
            self.assertEqual(filtered.call_count, 2)
            keys = [key for key in U.LAYER_CACHE.entries if key[0] == "fs-blur-v1"]
            self.assertEqual(len(keys), 1)
            self.assertEqual(keys[0][1][0], "fs-split-v1")
            self.state["source_img"][0, 0, 0] += .01
            U.send_skintone_parallel(p)
            self.assertEqual(filtered.call_count, 4)
            self.assertEqual(len([key for key in U.LAYER_CACHE.entries if key[0] == "fs-blur-v1"]), 2)

    def test_dnb_and_combine_cache_independently_and_mix_reuses_components(self):
        req = {**PARAMS, "mode": "combine", "S": 181, "frequency": {"radius": 2, "blur_radius": 3},
               "skintone": {"on": False}, "combine": {"fs_mix": 35, "dnb_mix": 65}}
        with patch.object(D, "band_stack", wraps=D.band_stack) as stack, \
             patch.object(FS, "split", wraps=FS.split) as split:
            U.api_pc_send_layers(req)
            req2 = {**req, "combine": {"fs_mix": 75, "dnb_mix": 15}}
            U.api_pc_send_layers(req2)
            self.assertEqual(stack.call_count, 1)
            self.assertEqual(split.call_count, 1)
            cached = self.calls[-1]
            U.LAYER_CACHE.clear()
            U.api_pc_send_layers(req2)
            fresh = self.calls[-1]
            for name in ("color", "detail", "blur"):
                np.testing.assert_array_equal(cached[1][name], fresh[1][name])
            np.testing.assert_array_equal(cached[2]["bands"][0][1], fresh[2]["bands"][0][1])
            U.api_pc_send_layers({**req, "mode": "dnb"})
            self.assertEqual(stack.call_count, 3)
            U.api_pc_send_layers({**req, "mode": "dnb"})
            self.assertEqual(stack.call_count, 3)
            U.api_pc_send_layers({**req, "mode": "dnb", "strength": .4})
            self.assertEqual(stack.call_count, 4)

    def test_opt_in_bands_reuse_cache_and_cannot_publish_after_supersession(self):
        p = U.parse_params({**PARAMS, "S": 181, "second_band": {"on": True, "invert": True}})
        mask = np.ones(self.state["img"].shape[:2], np.float32)
        self.state["masks"] = {"A": mask, "features": np.zeros_like(mask), "colour": mask}
        current = True

        def render(img, *args, **kwargs):
            nonlocal current
            current = not expire
            return np.full(img.shape[:2], .45, np.float32)

        expire = False
        with patch.object(U, "DNB_LAYER_BACKEND", "cuda"), \
             patch.object(U.processing_service.cuda_bands, "render_band", side_effect=render) as gpu:
            for kind in ("masked", "unmasked"):
                first, _, _ = U.cached_dnb_bands(p, kind, mask=mask)
                second, _, _ = U.cached_dnb_bands(p, kind, mask=mask)
                self.assertEqual([i for i, _ in first], [1, 2])
                self.assertIs(first[0][1], second[0][1])
            self.assertEqual(gpu.call_count, 4)
            self.assertIsNotNone(gpu.call_args_list[0].kwargs["mask"])
            self.assertIsNone(gpu.call_args_list[2].kwargs["mask"])
            self.assertTrue(gpu.call_args.kwargs["invert"])
            U.LAYER_CACHE.clear()
            expire = True
            with self.assertRaises(U.Superseded):
                U.cached_dnb_bands(p, "unmasked", mask=mask, commit_guard=lambda: nullcontext(current))
            self.assertEqual(U.LAYER_CACHE.bytes, 0)

    def test_mask_content_changes_invalidate_masked_bands_and_blur(self):
        shape = self.state["img"].shape[:2]
        self.state["masks"] = {"A": np.ones(shape, np.float32), "features": np.zeros(shape, np.float32),
                               "colour": np.ones(shape, np.float32)}
        req = {**PARAMS, "mode": "dnb", "S": 181}
        with patch.object(D, "band_stack", wraps=D.band_stack) as stack:
            U.api_pc_send_layers(req)
            U.api_pc_send_layers(req)
            self.assertEqual(stack.call_count, 1)
            self.state["masks"]["A"][3:7, 3:7] = .25
            U.api_pc_send_layers(req)
            self.assertEqual(stack.call_count, 2)

    def test_photoshop_send_reuses_matching_native_layers(self):
        self.state["source_kind"] = "photoshop"
        req = {**PARAMS, "mode": "frequency", "S": 181, "photoshop_hue": "native",
               "frequency": {"radius": 2, "blur_radius": 3}}
        with patch.dict(U.EDITOR, selected="photoshop"), patch.object(FS, "split", wraps=FS.split) as split:
            U.api_pc_send_layers(req)
            U.api_pc_send_layers(req)
            self.assertEqual(split.call_count, 1)
            self.assertEqual(self.calls[-1][2]["hue_output"], "native")
            for name in ("color", "detail", "blur"):
                np.testing.assert_array_equal(self.calls[0][1][name], self.calls[1][1][name])

    def test_skip_is_visible_in_send_progress(self):
        U.LAYER_CACHE.memory = lambda: 0
        req = {**PARAMS, "mode": "frequency", "S": 181, "frequency": {"radius": 2, "blur_radius": 3}}
        token = "layer-cache-skip-test"
        with progress.operation(token):
            U.api_pc_send_layers(req)
        steps = progress.snapshot(token, consume=True)["steps"]
        self.assertTrue(any("Layer cache skipped" in step["label"] for step in steps))
        self.assertEqual(U.LAYER_CACHE.bytes, 0)

    def test_photoshop_get_clears_even_identical_content(self):
        req = {**PARAMS, "mode": "frequency", "S": 181, "photoshop_hue": "native",
               "frequency": {"radius": 2, "blur_radius": 3}}
        pixels = np.random.default_rng(42).integers(0, 65536, (20, 24, 3), dtype=np.uint16)
        with tempfile.TemporaryDirectory() as directory:
            self.capture = U.PSB.RawCapture("capture", pixels.copy(), U.SRGB_ICC)
            self.document = {"index": 1, "width": 24, "height": 20, "depth": 16}
            with patch.dict(U.EDITOR, selected="photoshop"), \
                 patch.object(U.M, "pick_target", return_value=None), \
                 patch.object(FS, "split", wraps=FS.split) as split:
                U.api_pc_fetch({})
                U.api_pc_send_layers(req)
                first = self.calls[-1][1]
                generation = self.state["landmark_generation"]
                U.api_pc_fetch({})
                self.assertGreater(self.state["landmark_generation"], generation)
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
                U.api_pc_send_layers(req)
                self.assertEqual(split.call_count, 2)
                for name in ("color", "detail", "blur"):
                    np.testing.assert_array_equal(first[name], self.calls[-1][1][name])
                pixels[0, 0, 0] ^= 1
                self.capture = U.PSB.RawCapture("capture", pixels.copy(), U.SRGB_ICC)
                U.api_pc_fetch({})
                U.api_pc_send_layers(req)
                self.assertEqual(split.call_count, 3)
                self.document = {**self.document, "depth": 8}
                U.api_pc_fetch({})
                U.api_pc_send_layers(req)
                self.assertEqual(split.call_count, 4)

    def test_open_clears_cached_arrays_before_analysis_and_preserves_source_on_failure(self):
        import weakref

        old_image = self.state["img"]
        old_generation = self.state["landmark_generation"]
        self.state["imgs"] = {"orig": b"old preview"}
        self.state["preview_cache"] = {"entry": np.ones((100, 100), np.float32)}
        self.state["full_mask_cache"] = ("old", np.ones((100, 100), np.float32))
        refs = [weakref.ref(self.state["preview_cache"]["entry"]),
                weakref.ref(self.state["full_mask_cache"][1])]
        layer = np.ones((100, 100), np.float32)
        refs.append(weakref.ref(layer))
        self.assertTrue(U.LAYER_CACHE.put("old", (layer,), 0))
        del layer

        def fail(*args, **kwargs):
            self.assertEqual(U.LAYER_CACHE.bytes, 0)
            self.assertEqual(self.state["preview_cache"], {})
            self.assertIsNone(self.state["full_mask_cache"])
            self.assertTrue(all(ref() is None for ref in refs))
            self.assertIs(self.state["img"], old_image)
            raise ValueError("decode failed")

        with patch.object(U, "prepare_loaded_image", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "decode failed"):
                U.api_load({"path": "bad.png"})
        self.assertIs(self.state["img"], old_image)
        self.assertEqual(self.state["landmark_generation"], old_generation)
        self.assertEqual(self.state["imgs"], {"orig": b"old preview"})
        self.assertEqual(U.BRIDGE["doc"], {"index": 1})

    def test_open_new_image_discards_old_layers_before_analysis_and_releases_source(self):
        import weakref

        old_image = weakref.ref(self.state["img"])
        self.assertTrue(U.LAYER_CACHE.put("old", (np.ones((100, 100), np.float32),), 0))
        prepare = U.prepare_loaded_image
        def verify_before_decode(*args, **kwargs):
            self.assertEqual(U.LAYER_CACHE.bytes, 0)
            self.assertIsNotNone(old_image())
            return prepare(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.png"
            self.assertTrue(cv2.imwrite(str(path), np.full((20, 24, 3), 127, np.uint8)))
            with patch.object(U, "prepare_loaded_image", side_effect=verify_before_decode), \
                    patch.object(U.M, "pick_target", return_value=None):
                result = U.api_load({"path": str(path)})
        self.assertGreater(result["generation"], 1)
        self.assertIsNone(old_image())
        self.assertEqual(U.LAYER_CACHE.bytes, 0)

    def test_failed_get_clears_caches_but_keeps_existing_source_and_document(self):
        old_image, old_doc = self.state["img"], U.BRIDGE["doc"]
        self.assertTrue(U.LAYER_CACHE.put("old", (np.ones((10, 10), np.float32),), 0))
        class FailedBridge:
            def fetch(self, *args, **kwargs):
                self.assert_cache_cleared()
                raise ValueError("editor unavailable")
            def assert_cache_cleared(self):
                if U.LAYER_CACHE.bytes:
                    raise AssertionError("cache was not cleared before capture")
        with patch.object(U, "bridge", return_value=FailedBridge()):
            with self.assertRaisesRegex(ValueError, "editor unavailable"):
                U.api_pc_fetch({})
        self.assertIs(self.state["img"], old_image)
        self.assertIs(U.BRIDGE["doc"], old_doc)
        self.assertEqual(self.state["landmark_generation"], 1)

    def test_replacement_supersedes_running_and_queued_workers(self):
        coordinator = U.CACHE_COORDINATOR
        entered, release = threading.Event(), threading.Event()
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"on": False, "radius": 2}})
        original = FS.split
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release timed out")
            return original(*args, **kwargs)

        layer_job = MaskPrecache(coordinator.precache_layers, delay=.01)
        mask_job = MaskPrecache(coordinator.precache_mask, delay=10)
        with patch.object(coordinator, "layer_jobs", layer_job), patch.object(coordinator, "mask_jobs", mask_job), \
                patch.object(FS, "split", side_effect=slow):
            try:
                coordinator.queue_layer_precache(p, "old")
                self.assertTrue(entered.wait(5))
                coordinator.queue_mask_precache(U.parse_params({**PARAMS, "mode": "mask", "S": 181}))
                self.assertEqual(mask_job.status()["status"], "queued")
                U.begin_image_replacement()
                release.set()
                layer_job.close()
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
                self.assertIsNone(self.state["full_mask_cache"])
                self.assertEqual(mask_job.status()["status"], "idle")
                self.assertEqual(coordinator.layer_cache_status()["tabs"], {})
            finally:
                release.set()
                layer_job.close()
                mask_job.close()

    def test_replacement_prevents_running_mask_from_repopulating(self):
        coordinator = U.CACHE_COORDINATOR
        started, release = threading.Event(), threading.Event()
        original = coordinator._compute_mask
        def slow(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release timed out")
            return original(*args, **kwargs)

        job = MaskPrecache(coordinator.precache_mask, delay=.01)
        with patch.object(coordinator, "mask_jobs", job), patch.object(coordinator, "_compute_mask", slow):
            try:
                coordinator.queue_mask_precache(U.parse_params({**PARAMS, "mode": "mask", "S": 181}))
                self.assertTrue(started.wait(5))
                U.begin_image_replacement()
                release.set()
                job.close()
                self.assertIsNone(self.state["full_mask_cache"])
                self.assertEqual(job.status()["status"], "idle")
            finally:
                release.set()
                job.close()

    def test_masked_dnb_reuses_equal_reacquired_masks_and_rejects_profile_changes(self):
        shape = self.state["img"].shape[:2]
        self.state["masks"] = {"A": np.ones(shape, np.float32), "features": np.zeros(shape, np.float32),
                               "colour": np.ones(shape, np.float32)}
        req = {**PARAMS, "mode": "dnb", "S": 181}
        with patch.object(D, "band_stack", wraps=D.band_stack) as stack:
            U.api_pc_send_layers(req)
            self.state["img"] = self.state["img"].copy()
            self.state["source_img"] = self.state["img"]
            self.state["masks"] = {name: a.copy() for name, a in self.state["masks"].items()}
            self.state["landmark_generation"] += 1
            U.api_pc_send_layers(req)
            self.assertEqual(stack.call_count, 1)
            self.state["icc"] = b"different profile"
            U.api_pc_send_layers(req)
            self.assertEqual(stack.call_count, 2)

    def test_idle_preparation_matches_both_dnb_send_modes(self):
        shape = self.state["img"].shape[:2]
        self.state["masks"] = {"A": np.full(shape, .8, np.float32),
                               "features": np.zeros(shape, np.float32),
                               "colour": np.ones(shape, np.float32)}
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler):
            try:
                for mode in ("dnb", "combine"):
                    req = {**PARAMS, "mode": mode, "S": 181, "frequency": {"on": False, "radius": 2},
                           "skintone": {"on": False}}
                    p = U.parse_params(req)
                    U.CACHE_COORDINATOR.queue_layer_precache(p)
                    self.assertEqual(self.settle(scheduler)["status"], "ready")
                    with patch.object(D, "band_stack", wraps=D.band_stack) as stack:
                        U.api_pc_send_layers(req)
                        self.assertEqual(stack.call_count, 0)
                    prepared = self.calls[-1]
                    U.LAYER_CACHE.clear()
                    U.api_pc_send_layers(req)
                    fresh = self.calls[-1]
                    if mode == "combine":
                        np.testing.assert_array_equal(prepared[2]["bands"][0][1], fresh[2]["bands"][0][1])
                    else:
                        np.testing.assert_array_equal(prepared[1][0][1], fresh[1][0][1])
            finally:
                scheduler.close()

    def test_multicore_median_preserves_exact_split(self):
        source = np.random.default_rng(7).random((129, 150, 3), dtype=np.float32)
        serial = FS.split(source, "median_fast", 5, workers=1)
        parallel = FS.split(source, "median_fast", 5, workers=16)
        for key in ("color", "detail"):
            np.testing.assert_array_equal(serial[key], parallel[key])

    def settle(self, scheduler):
        deadline = time.monotonic() + 10
        while scheduler.status()["status"] in ("queued", "running"):
            if time.monotonic() >= deadline:
                self.fail("layer preparation did not settle")
            time.sleep(.005)
        return scheduler.status()

    def test_mask_timing_distinguishes_fresh_reused_and_superseded_work(self):
        coordinator = U.CACHE_COORDINATOR
        scheduler = MaskPrecache(coordinator.precache_mask, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "mask", "S": 181})
        with patch.object(coordinator, "mask_jobs", scheduler):
            try:
                for expected in ("calculated", "reused"):
                    coordinator.queue_mask_precache(p)
                    self.assertEqual(self.settle(scheduler)["status"], "ready")
                    result = coordinator.mask_cache_status()["timing"]
                    self.assertEqual(result["outcome"], expected)
                    self.assertGreaterEqual(result["seconds"], result["steps"][0]["seconds"])
                    scheduler.reset()
                    self.assertNotIn("timing", coordinator.mask_cache_status())
            finally:
                scheduler.close()

    def test_split_overlaps_pending_mask_but_blur_waits(self):
        coordinator = U.CACHE_COORDINATOR
        mask_job = MaskPrecache(coordinator.precache_mask, delay=.01)
        layer_job = MaskPrecache(coordinator.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"on": True, "radius": 2, "blur_radius": 3}})
        entered, release, split_done, blur_started = (threading.Event() for _ in range(4))
        original_mask, original_split, original_blur = U.cached_final_skin_mask, U.cached_fs_split, U.cached_fs_blur

        def held_mask(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("mask was not released")
            return original_mask(*args, **kwargs)

        def split(*args, **kwargs):
            result = original_split(*args, **kwargs)
            split_done.set()
            return result

        def blur(*args, **kwargs):
            blur_started.set()
            return original_blur(*args, **kwargs)

        with patch.object(coordinator, "mask_jobs", mask_job), patch.object(coordinator, "layer_jobs", layer_job), \
             patch.object(coordinator, "_compute_mask", held_mask), patch.object(U, "cached_fs_split", split), \
             patch.object(U, "cached_fs_blur", blur):
            try:
                coordinator.queue_mask_precache(p)
                self.assertTrue(entered.wait(5))
                coordinator.queue_layer_precache(p, "overlap")
                self.assertTrue(split_done.wait(5))
                self.assertFalse(blur_started.is_set())
                with U.foreground_lock():
                    self.assertFalse(blur_started.is_set())
                release.set()
                self.assertEqual(self.settle(layer_job)["status"], "ready")
                self.assertTrue(blur_started.is_set())
            finally:
                release.set()
                layer_job.close()
                mask_job.close()

    def test_cuda_band_overlaps_blur_and_combine_waits_for_both(self):
        coordinator = U.CACHE_COORDINATOR
        job = MaskPrecache(coordinator.precache_layers, delay=.01)
        req = {**PARAMS, "mode": "combine", "S": 181,
               "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "skintone": {"on": False}}
        blur_entered, dnb_entered, release = (threading.Event() for _ in range(3))
        blur = U.cached_fs_blur
        def held_blur(*args, **kwargs):
            blur_entered.set()
            if not release.wait(5):
                raise TimeoutError("blur was not released")
            return blur(*args, **kwargs)

        def held_band(img, *args, **kwargs):
            dnb_entered.set()
            if not release.wait(5):
                raise TimeoutError("band was not released")
            return np.full(img.shape[:2], .5, np.float32)

        with patch.object(coordinator, "layer_jobs", job), patch.object(U, "DNB_LAYER_BACKEND", "cuda"), \
             patch.object(U, "cached_fs_blur", held_blur), \
             patch.object(U.processing_service.cuda_bands, "render_band", side_effect=held_band) as gpu:
            try:
                coordinator.queue_layer_precache(U.parse_params(req), "cuda-overlap", req)
                self.assertTrue(blur_entered.wait(5))
                self.assertTrue(dnb_entered.wait(5))
                self.assertEqual(gpu.call_count, 1)
                self.assertNotIn("combine", coordinator.layer_cache_status()["records"])
                release.set()
                self.assertEqual(self.settle(job)["status"], "ready")
                status = coordinator.layer_cache_status()
                self.assertTrue(status["records"]["combine"]["cached"])
                self.assertEqual(gpu.call_count, 2)
                self.assertTrue(any("Blur" in step["label"] for step in status["timings"]["frequency"]["steps"]))
                self.assertFalse(any("CUDA" in step["label"] for step in status["timings"]["frequency"]["steps"]))
            finally:
                release.set()
                job.close()

    def test_mask_dwell_prepares_all_modes_and_reuses_only_unchanged_bands(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        req = {**PARAMS, "mode": "mask", "S": 181,
               "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "second_band": {"on": True, "lo": 2, "hi": 20, "strength": .7},
               "skintone": {"on": False}}
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), \
             patch.object(FS, "split", wraps=FS.split) as split, \
             patch.object(D, "band_stack", wraps=D.band_stack) as stack:
            try:
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(req), "first", req)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                status = U.CACHE_COORDINATOR.layer_cache_status()
                records = status["records"]
                self.assertEqual(status["timings"]["frequency"]["outcome"], "calculated")
                self.assertEqual(status["timings"]["dnb"]["outcome"], "mixed")
                self.assertTrue(status["timings"]["combine"]["shared_fs"])
                self.assertTrue(all(item["seconds"] >= 0 for item in status["timings"].values()))
                self.assertTrue(all(records[mode]["cached"] for mode in ("frequency", "dnb", "combine")))
                self.assertEqual((split.call_count, stack.call_count), (1, 4))
                for mode in ("frequency", "dnb", "combine"):
                    U.api_pc_send_layers({**req, "mode": mode})
                self.assertEqual((split.call_count, stack.call_count), (1, 4))

                changed = {**req, "strength": .4}
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(changed), "second", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (1, 6))
                self.assertEqual(U.CACHE_COORDINATOR.layer_cache_status()["timings"]["frequency"]["outcome"], "reused")
                self.assertTrue(all(U.CACHE_COORDINATOR.layer_cache_status()["records"][mode]["cached"]
                                    for mode in ("frequency", "dnb", "combine")))

                changed = {**changed, "second_band": {**req["second_band"], "strength": .4}}
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(changed), "third", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (1, 8))

                changed = {**changed, "frequency": {**req["frequency"], "radius": 3}}
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(changed), "fourth", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (2, 8))

                self.state["source_img"][0, 0, 0] += .01
                self.state["landmark_generation"] += 1
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(changed), "fifth", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (3, 12))
            finally:
                scheduler.close()

    def test_independent_bands_match_original_two_band_math(self):
        shape = self.state["img"].shape[:2]
        self.state["masks"] = {"A": np.full(shape, .8, np.float32),
                               "features": np.zeros(shape, np.float32),
                               "colour": np.ones(shape, np.float32)}
        req = {**PARAMS, "S": 181, "layer_blur_at_s181": 2,
               "second_band": {"on": True, "lo": 2, "hi": 20, "strength": .7, "invert": True}}
        p = U.parse_params({**req, "mode": "dnb"})
        expected, _, _ = U.dnb_full_result(p, layered=True)
        with patch.object(D, "softlight", side_effect=AssertionError("unused cache blend")):
            actual, _, _ = U.cached_dnb_bands(p, "masked")
        for (i, a), (j, b) in zip(expected, actual):
            self.assertEqual(i, j)
            np.testing.assert_array_equal(a, b)
        p = U.parse_params({**req, "mode": "combine"})
        mask = U.full_skin_mask(p)
        expected, _, _, _ = D.band_stack(self.state["source_img"], p["S"], p["bands"], p["strength"],
            p["second_band"], first_on=p["band1_on"], sigma=p["layer_blur_at_s181"] * p["S"] / 181,
            edge=p["edge"], tau=p["tau"], max_ev=p["max_ev"], mask_result=(mask, None))
        with patch.object(D, "softlight", side_effect=AssertionError("unused cache blend")):
            actual, _, _ = U.cached_dnb_bands(p, "unmasked", mask=mask)
        for (i, a), (j, b) in zip(expected, actual):
            self.assertEqual(i, j)
            np.testing.assert_array_equal(a, b)

    def test_budget_reports_when_all_components_cannot_fit(self):
        class Source:
            shape = (7008, 4672, 3)
            nbytes = 7008 * 4672 * 3 * 4
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=10)
        req = {**PARAMS, "mode": "mask", "S": 181, "second_band": {"on": True}}
        U.LAYER_CACHE.limit = 2 * 1024**3
        retained = Source.nbytes * 3 + Source.shape[0] * Source.shape[1] * 4 * 4
        self.assertGreater(retained, 1536 * 1024**2)
        self.assertLess(retained, U.LAYER_CACHE.limit)
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler):
            try:
                self.state["source_img"] = Source()
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(req), "fit", req)
                self.assertIsNone(U.CACHE_COORDINATOR.layer_cache_status()["budget_note"])
                self.state["source_img"] = type("Large", (), {"shape": (10000, 8000, 3),
                                                               "nbytes": 10000 * 8000 * 3 * 4})()
                U.CACHE_COORDINATOR.queue_layer_precache(U.parse_params(req), "large", req)
                self.assertIn("Some components may be evicted", U.CACHE_COORDINATOR.layer_cache_status()["budget_note"])
            finally:
                scheduler.close()

    def test_mask_preview_http_prepares_all_tab_records(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        req = {**PARAMS, "mode": "mask", "S": 181, "precache_layers": True,
               "layer_cache_revision": "global", "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "skintone": {"on": False}, "second_band": {"on": True}}
        try:
            with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), \
                 patch.object(U, "api_run", return_value={"version": 1}), \
                 patch.object(U.CACHE_COORDINATOR, "queue_mask_precache"):
                request = Request(base + "/api/run", json.dumps(req).encode(),
                                  {"Content-Type": "application/json", "Origin": base})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(json.load(response)["layer_precache"]["status"], "queued")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                with urlopen(base + "/api/layer-precache", timeout=5) as response:
                    status = json.load(response)
                self.assertEqual(status["mode"], "mask")
                self.assertEqual(set(status["timings"]), {"frequency", "dnb", "combine"})
                self.assertTrue(all(status["records"][mode]["cached"]
                                    and status["tabs"][mode]["state"] == "ready"
                                    for mode in ("frequency", "dnb", "combine")))
        finally:
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_latest_preparation_exact_pixels_and_foreground_reuse(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.04)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"method": "median_fast", "radius": 2, "blur_radius": 3},
                            "skintone": {"on": False}})
        newest = {**p, "frequency": {**p["frequency"], "radius": 3}}
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler):
            try:
                with patch.object(FS, "split", wraps=FS.split) as split:
                    U.CACHE_COORDINATOR.queue_layer_precache(p)
                    U.CACHE_COORDINATOR.queue_layer_precache(newest)
                    self.assertEqual(self.settle(scheduler)["status"], "ready")
                    self.assertEqual(split.call_count, 1)
                    U.send_skintone_parallel(newest)
                    self.assertEqual(split.call_count, 1)
                prepared = self.calls[-1][1]
                U.LAYER_CACHE.clear()
                U.send_skintone_parallel(newest)
                for name in ("color", "detail", "blur"):
                    np.testing.assert_array_equal(prepared[name], self.calls[-1][1][name])
            finally:
                scheduler.close()

    def test_tab_records_track_exact_retention_generation_and_mode(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "combine", "S": 181,
                            "frequency": {"on": False, "radius": 2},
                            "skintone": {"on": False}})
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler):
            try:
                U.CACHE_COORDINATOR.queue_layer_precache(p, "revision-1")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                records = U.CACHE_COORDINATOR.layer_cache_status()["records"]
                self.assertEqual(set(records), {"combine", "combine_bands", "frequency"})
                self.assertEqual(records["combine"], {"generation": 1, "stamp": "revision-1", "cached": True})
                self.assertEqual(records["frequency"], {"generation": 1, "stamp": "revision-1", "cached": True})
                self.state["landmark_generation"] = 2
                self.assertFalse(U.CACHE_COORDINATOR.layer_cache_status()["records"]["combine"]["cached"])
                self.state["landmark_generation"] = 1
                self.assertTrue(U.CACHE_COORDINATOR.layer_cache_status()["records"]["combine"]["cached"])
                with patch.object(U.LAYER_CACHE, "memory", return_value=0):
                    self.assertTrue(U.CACHE_COORDINATOR.layer_cache_status()["records"]["combine"]["cached"])
                U.LAYER_CACHE.clear()
                self.assertFalse(U.CACHE_COORDINATOR.layer_cache_status()["records"]["combine"]["cached"])
            finally:
                scheduler.close()

    def test_mix_preview_renders_while_background_split_is_held(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        req = {**PARAMS, "mode": "combine", "S": 181, "precache_layers": True,
               "layer_cache_revision": "global", "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "skintone": {"on": False}, "second_band": {"on": True}}
        started, release, foreground, full_resolution = (threading.Event() for _ in range(4))
        release_foreground = threading.Event()
        stage_order = []
        errors = []
        self.state.update(centre=(12, 10), avers=0)
        original_split = U.cached_fs_split
        original_blur = U.cached_fs_blur
        original_run = U.api_run

        def delayed(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release split")
            return original_split(*args, **kwargs)

        def blur(*args, **kwargs):
            stage_order.append("blur")
            return original_blur(*args, **kwargs)

        def run(request):
            if request.get("mix_only"):
                result = original_run(request)
                foreground.set()
                return result
            return {"version": 1}

        def post(body):
            request = Request(base + "/api/run", json.dumps(body).encode(),
                              {"Content-Type": "application/json", "Origin": base})
            with urlopen(request, timeout=5) as response:
                return json.load(response)

        def changed_mix():
            try:
                post({**req, "mix_only": True, "combine": {"fs_mix": 40, "dnb_mix": 60}})
            except Exception as error:
                errors.append(error)

        def wait_for_full_resolution():
            with U.foreground_lock(full_resolution=True):
                stage_order.append("foreground")
                full_resolution.set()
                if not release_foreground.wait(5):
                    errors.append(TimeoutError("test did not release foreground"))

        try:
            with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), \
                 patch.object(U, "api_run", side_effect=run), \
                 patch.object(U.CACHE_COORDINATOR, "queue_mask_precache"), \
                 patch.object(U, "cached_fs_blur", side_effect=blur), \
                 patch.object(U, "cached_fs_split", side_effect=delayed):
                post(req)
                self.assertTrue(started.wait(5))
                sequence = scheduler.status()["sequence"]
                self.assertTrue(U.CACHE_COORDINATOR.matching_layer_preparation(
                    {**req, "mix_only": True, "combine": {"fs_mix": 40, "dnb_mix": 60}}))
                self.assertFalse(U.CACHE_COORDINATOR.matching_layer_preparation({**req, "mix_only": True, "strength": .4}))
                full_thread = threading.Thread(target=wait_for_full_resolution)
                request_thread = threading.Thread(target=changed_mix)
                full_thread.start()
                try:
                    self.assertTrue(U.CACHE_COORDINATOR.foreground_waiting.wait(3))
                    request_thread.start()
                    self.assertTrue(foreground.wait(3))
                    request_thread.join(3)
                    self.assertFalse(request_thread.is_alive())
                    self.assertFalse(errors)
                    self.assertFalse(release.is_set())
                    self.assertFalse(full_resolution.is_set())
                    self.assertIn("result", self.state["imgs"])
                    self.assertEqual(scheduler.status()["sequence"], sequence)
                    release.set()
                    self.assertTrue(full_resolution.wait(3))
                    self.assertEqual(stage_order, ["foreground"])
                    self.assertEqual(scheduler.status()["status"], "running")
                    release_foreground.set()
                    self.assertEqual(self.settle(scheduler)["status"], "ready")
                    self.assertEqual(stage_order, ["foreground", "blur"])
                    self.assertEqual(scheduler.status()["sequence"], sequence)
                finally:
                    release.set()
                    release_foreground.set()
                    full_thread.join(5)
                    if request_thread.ident is not None:
                        request_thread.join(5)
        finally:
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_combine_publishes_shared_fs_before_bands_and_tracks_eviction(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "combine", "S": 181,
                            "frequency": {"on": True, "radius": 2, "blur_radius": 3},
                            "skintone": {"on": False}})
        entered, release = threading.Event(), threading.Event()
        original = D.band_stack

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out waiting for D&B")
            return original(*args, **kwargs)

        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), patch.object(D, "band_stack", side_effect=delayed):
            try:
                U.CACHE_COORDINATOR.queue_layer_precache(p, "revision-1")
                self.assertTrue(entered.wait(5))
                status = U.CACHE_COORDINATOR.layer_cache_status()
                self.assertTrue(status["records"]["frequency"]["cached"])
                self.assertNotIn("combine", status["records"])
                self.assertNotIn("dnb", status["records"])
                release.set()
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertTrue(U.CACHE_COORDINATOR.layer_cache_status()["records"]["combine"]["cached"])
                frequency = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                                            "frequency": {"on": True, "radius": 2, "blur_radius": 3},
                                            "skintone": {"on": False}})
                with patch.object(FS, "split", wraps=FS.split) as split, \
                     patch.object(FS, "skin_filter", wraps=FS.skin_filter) as blur:
                    U.send_skintone_parallel(frequency)
                    self.assertEqual(split.call_count, 0)
                    self.assertEqual(blur.call_count, 0)
                fs_keys = U.CACHE_COORDINATOR.layer_records["frequency"][2]
                with U.LAYER_CACHE.lock:
                    _, size = U.LAYER_CACHE.entries.pop(fs_keys[-1])
                    U.LAYER_CACHE.bytes -= size
                status = U.CACHE_COORDINATOR.layer_cache_status()["records"]
                self.assertFalse(status["frequency"]["cached"])
                self.assertFalse(status["combine"]["cached"])
            finally:
                release.set()
                scheduler.close()

    def test_combine_caches_split_at_zero_mix_but_not_unprepared_directional(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        base = {**PARAMS, "mode": "combine", "S": 181,
                "frequency": {"on": False, "radius": 2}, "skintone": {"on": False}}
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler):
            try:
                p = U.parse_params({**base, "combine": {"fs_mix": 0, "dnb_mix": 0}})
                U.CACHE_COORDINATOR.queue_layer_precache(p, "zero-mix")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertTrue(U.CACHE_COORDINATOR.layer_cache_status()["records"]["frequency"]["cached"])
                self.assertTrue(U.CACHE_COORDINATOR.layer_cache_status()["records"]["combine"]["cached"])
                p = {**p, "combine": {"fs_mix": 100, "dnb_mix": 100},
                     "directional": {**U.DB.DEFAULTS, "on": True}}
                U.CACHE_COORDINATOR.queue_layer_precache(p, "directional")
                self.assertEqual(self.settle(scheduler)["status"], "skipped")
                records = U.CACHE_COORDINATOR.layer_cache_status()["records"]
                self.assertNotEqual(records["combine"]["stamp"], "directional")
                self.assertNotEqual(records.get("frequency", {}).get("stamp"), "directional")
            finally:
                scheduler.close()

    def test_stale_generation_cannot_publish_components_or_ready_status(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"on": False, "radius": 2}})
        started, release = threading.Event(), threading.Event()
        original = FS.split
        def slow(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release timed out")
            return original(*args, **kwargs)
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), patch.object(FS, "split", side_effect=slow):
            try:
                U.CACHE_COORDINATOR.queue_layer_precache(p, "old")
                self.assertTrue(started.wait(5))
                with U.foreground_lock():
                    U.image_session.publish_source(
                        self.state, {"img": self.state["img"].copy(),
                                     "source_img": self.state["source_img"].copy(),
                                     "masks": dict(self.state["masks"] or {})},
                    )
                release.set()
                self.assertEqual(self.settle(scheduler)["status"], "superseded")
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
                self.assertNotIn("frequency", U.CACHE_COORDINATOR.layer_cache_status()["records"])
                self.assertNotEqual(U.CACHE_COORDINATOR.layer_cache_status()["tabs"]["frequency"]["state"], "ready")
            finally:
                release.set()
                scheduler.close()

    def test_low_or_unknown_memory_skips_before_allocating(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181})
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), patch.object(FS, "split", wraps=FS.split) as split:
            try:
                for free in (0, None):
                    U.LAYER_CACHE.memory = lambda: free
                    scheduler.reset()
                    U.CACHE_COORDINATOR.queue_layer_precache(p)
                    status = self.settle(scheduler)
                    self.assertEqual(status["status"], "skipped")
                    self.assertIn("RAM", status["message"])
                    self.assertIn("FS Color/Detail", status["message"])
                    self.assertIn(f"{2 * 1024**3 + 20 * 24 * 128:,} bytes", status["message"])
                    if free is None:
                        self.assertIn("could not be measured", status["message"])
                self.assertEqual(split.call_count, 0)
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
            finally:
                scheduler.close()

    def test_cache_write_reports_later_memory_read_and_threshold(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
        threshold = U.layer_reserve(self.state["img"])
        free = [100 * 1024**3]
        U.LAYER_CACHE.memory = lambda: free[0]
        original_split = FS.split

        def split(*args, **kwargs):
            result = original_split(*args, **kwargs)
            free[0] = threshold - 1
            return result

        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), patch.object(FS, "split", split):
            try:
                U.CACHE_COORDINATOR.queue_layer_precache(p)
                status = self.settle(scheduler)
                self.assertEqual(status["status"], "skipped")
                self.assertIn("FS Color/Detail cache", status["message"])
                self.assertIn(f"{threshold - 1:,} bytes", status["message"])
                self.assertIn(f"{threshold:,} bytes", status["message"])
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
            finally:
                scheduler.close()

    def test_background_admission_uses_two_gib_plus_per_pixel_allowance(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
        pixels = self.state["img"].shape[0] * self.state["img"].shape[1]
        threshold = 2 * 1024**3 + pixels * 128
        self.assertEqual(U.layer_reserve(self.state["img"]), 2 * 1024**3 + pixels * 64)
        free = [threshold - 1]
        U.LAYER_CACHE.memory = lambda: free[0]
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), \
                patch.object(FS, "split", wraps=FS.split) as split:
            try:
                U.CACHE_COORDINATOR.queue_layer_precache(p, "below")
                status = self.settle(scheduler)
                self.assertEqual(status["status"], "skipped")
                self.assertIn("FS Color/Detail", status["message"])
                self.assertIn(f"{threshold - 1:,} bytes", status["message"])
                self.assertIn(f"{threshold:,} bytes", status["message"])
                self.assertEqual(split.call_count, 0)
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
                free[0] = threshold
                scheduler.reset()
                U.CACHE_COORDINATOR.queue_layer_precache(p, "at")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual(split.call_count, 1)
                self.assertGreater(U.LAYER_CACHE.bytes, 0)
            finally:
                scheduler.close()

    def test_overlap_requires_two_gib_plus_256_bytes_per_pixel(self):
        coordinator = U.CACHE_COORDINATOR
        pixels = self.state["img"].shape[0] * self.state["img"].shape[1]
        threshold = 2 * 1024**3 + pixels * 256
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
        original_mask, original_split = coordinator._compute_mask, FS.split
        for free, overlaps in ((threshold - 1, False), (threshold, True)):
            with self.subTest(free=free):
                U.LAYER_CACHE.clear()
                U.LAYER_CACHE.memory = lambda: free
                entered, release, split_done = (threading.Event() for _ in range(3))
                def held_mask(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("mask was not released")
                    return original_mask(*args, **kwargs)
                def split(*args, **kwargs):
                    result = original_split(*args, **kwargs)
                    split_done.set()
                    return result
                mask_job = MaskPrecache(coordinator.precache_mask, delay=.01)
                layer_job = MaskPrecache(coordinator.precache_layers, delay=.01)
                with patch.object(coordinator, "mask_jobs", mask_job), \
                     patch.object(coordinator, "layer_jobs", layer_job), \
                     patch.object(coordinator, "_compute_mask", held_mask), patch.object(FS, "split", split):
                    try:
                        coordinator.queue_mask_precache(U.parse_params({**PARAMS, "mode": "mask", "S": 181}))
                        self.assertTrue(entered.wait(5))
                        coordinator.queue_layer_precache(p, "threshold")
                        self.assertEqual(split_done.wait(.5), overlaps)
                        release.set()
                        self.assertEqual(self.settle(layer_job)["status"], "ready")
                        self.assertTrue(split_done.is_set())
                    finally:
                        release.set()
                        layer_job.close()
                        mask_job.close()

    def test_mask_and_foreground_locks_exclude_preparation(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
        with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), patch.object(FS, "split", wraps=FS.split) as split:
            try:
                with U.CACHE_COORDINATOR.full_resolution_lock:
                    U.CACHE_COORDINATOR.queue_layer_precache(p)
                    time.sleep(.15)
                    self.assertEqual(split.call_count, 0)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual(split.call_count, 1)
            finally:
                scheduler.close()

    def test_http_preview_enables_preparation_and_cancels_same_origin_only(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=10)
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def post(path, body, origin=base):
            request = Request(base + path, json.dumps(body).encode(),
                              {"Content-Type": "application/json", "Origin": origin})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        try:
            with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), \
                 patch.object(U, "api_run", return_value={"version": 1}), \
                 patch.object(U.CACHE_COORDINATOR, "queue_mask_precache"):
                response = post("/api/run", {"mode": "frequency", "S": 181, "precache_layers": True})
                self.assertEqual(response["layer_precache"]["status"], "queued")
                post("/api/profile", {"bands": [[1, 2, 1, .2]], "strength": 1})
                post("/api/tone", {"mid": .5, "range": .5, "taper": .1})
                with urlopen(base + "/api/layer-precache", timeout=5) as response:
                    self.assertEqual(json.load(response)["status"], "queued")
                with self.assertRaises(HTTPError) as error:
                    post("/api/layer-precache/cancel", {}, "http://example.test")
                self.assertEqual(error.exception.code, 403)
                self.assertEqual(scheduler.status()["status"], "queued")
                post("/api/layer-precache/cancel", {})
                self.assertEqual(scheduler.status()["status"], "idle")
                response = post("/api/run", {"mode": "mask", "S": 181, "precache_layers": True})
                self.assertEqual(response["layer_precache"]["status"], "queued")
                self.assertEqual(response["layer_precache"]["tabs"]["frequency"]["state"], "queued")
                response = post("/api/run", {"mode": "frequency", "S": 181, "precache_layers": False})
                self.assertEqual(response["layer_precache"]["status"], "skipped")
                self.assertIn("disabled", response["layer_precache"]["message"])
                with self.assertRaises(HTTPError):
                    post("/api/run", {"mode": "frequency", "S": 181, "precache_layers": "yes"})
        finally:
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_graph_requests_are_responsive_and_do_not_cancel_running_preparation(self):
        scheduler = MaskPrecache(U.CACHE_COORDINATOR.precache_layers, delay=.01)
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        started, release = threading.Event(), threading.Event()
        original = FS.split
        def slow(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release timed out")
            return original(*args, **kwargs)
        def post(path, body):
            request = Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=2) as response:
                return json.load(response)
        try:
            with patch.object(U.CACHE_COORDINATOR, "layer_jobs", scheduler), patch.object(FS, "split", side_effect=slow):
                p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
                U.CACHE_COORDINATOR.queue_layer_precache(p)
                self.assertTrue(started.wait(5))
                self.assertIn("Color and Detail", scheduler.status()["message"])
                post("/api/profile", {"bands": [[1, 2, 1, .2]], "strength": 1})
                post("/api/tone", {"mid": .5, "range": .5, "taper": .1})
                self.assertEqual(scheduler.status()["status"], "running")
                release.set()
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertGreater(U.LAYER_CACHE.bytes, 0)
        finally:
            release.set()
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join()
