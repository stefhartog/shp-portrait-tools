"""Bounded full-resolution component caching and idle preparation."""

import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
import threading
import time
import json
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

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
                return test.image, test.document

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
                     patch.object(U, "LAYER_PRECACHE_RECORDS", {}),
                     patch.object(U, "LAYER_PRECACHE_ACTIVE", {"mode": None, "stamp": None, "generation": None, "tabs": {}, "budget_note": None}),
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
        with patch.object(U, "ThreadPoolExecutor", DeferredExecutor), \
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

    def test_photoshop_get_reuses_identical_content_but_not_changed_pixels_or_depth(self):
        req = {**PARAMS, "mode": "frequency", "S": 181, "photoshop_hue": "native",
               "frequency": {"radius": 2, "blur_radius": 3}}
        pixels = np.random.default_rng(42).integers(0, 65536, (20, 24, 3), dtype=np.uint16)
        with tempfile.TemporaryDirectory() as directory:
            self.image = Path(directory) / "capture.png"
            self.image.write_bytes(U.PCB.tagged_rgb16(pixels, U.SRGB_ICC, {"opaque_alpha": False}))
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
                U.api_pc_send_layers(req)
                self.assertEqual(split.call_count, 1)
                for name in ("color", "detail", "blur"):
                    np.testing.assert_array_equal(first[name], self.calls[-1][1][name])
                pixels[0, 0, 0] ^= 1
                self.image.write_bytes(U.PCB.tagged_rgb16(pixels, U.SRGB_ICC, {"opaque_alpha": False}))
                U.api_pc_fetch({})
                U.api_pc_send_layers(req)
                self.assertEqual(split.call_count, 2)
                self.document = {**self.document, "depth": 8}
                U.api_pc_fetch({})
                U.api_pc_send_layers(req)
                self.assertEqual(split.call_count, 3)

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
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        with patch.object(U, "LAYER_PRECACHE", scheduler):
            try:
                for mode in ("dnb", "combine"):
                    req = {**PARAMS, "mode": mode, "S": 181, "frequency": {"on": False, "radius": 2},
                           "skintone": {"on": False}}
                    p = U.parse_params(req)
                    U.queue_layer_precache(p)
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

    def test_mask_dwell_prepares_all_modes_and_reuses_only_unchanged_bands(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        req = {**PARAMS, "mode": "mask", "S": 181,
               "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "second_band": {"on": True, "lo": 2, "hi": 20, "strength": .7},
               "skintone": {"on": False}}
        with patch.object(U, "LAYER_PRECACHE", scheduler), \
             patch.object(FS, "split", wraps=FS.split) as split, \
             patch.object(D, "band_stack", wraps=D.band_stack) as stack:
            try:
                U.queue_layer_precache(U.parse_params(req), "first", req)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                records = U.layer_cache_status()["records"]
                self.assertTrue(all(records[mode]["cached"] for mode in ("frequency", "dnb", "combine")))
                self.assertEqual((split.call_count, stack.call_count), (1, 4))
                for mode in ("frequency", "dnb", "combine"):
                    U.api_pc_send_layers({**req, "mode": mode})
                self.assertEqual((split.call_count, stack.call_count), (1, 4))

                changed = {**req, "strength": .4}
                U.queue_layer_precache(U.parse_params(changed), "second", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (1, 6))
                self.assertTrue(all(U.layer_cache_status()["records"][mode]["cached"]
                                    for mode in ("frequency", "dnb", "combine")))

                changed = {**changed, "second_band": {**req["second_band"], "strength": .4}}
                U.queue_layer_precache(U.parse_params(changed), "third", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (1, 8))

                changed = {**changed, "frequency": {**req["frequency"], "radius": 3}}
                U.queue_layer_precache(U.parse_params(changed), "fourth", changed)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual((split.call_count, stack.call_count), (2, 8))

                self.state["source_img"][0, 0, 0] += .01
                self.state["landmark_generation"] += 1
                U.queue_layer_precache(U.parse_params(changed), "fifth", changed)
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
        scheduler = MaskPrecache(U.precache_layers, delay=10)
        req = {**PARAMS, "mode": "mask", "S": 181, "second_band": {"on": True}}
        U.LAYER_CACHE.limit = 2 * 1024**3
        retained = Source.nbytes * 3 + Source.shape[0] * Source.shape[1] * 4 * 4
        self.assertGreater(retained, 1536 * 1024**2)
        self.assertLess(retained, U.LAYER_CACHE.limit)
        with patch.object(U, "LAYER_PRECACHE", scheduler):
            try:
                self.state["source_img"] = Source()
                U.queue_layer_precache(U.parse_params(req), "fit", req)
                self.assertIsNone(U.layer_cache_status()["budget_note"])
                self.state["source_img"] = type("Large", (), {"shape": (10000, 8000, 3),
                                                               "nbytes": 10000 * 8000 * 3 * 4})()
                U.queue_layer_precache(U.parse_params(req), "large", req)
                self.assertIn("Some components may be evicted", U.layer_cache_status()["budget_note"])
            finally:
                scheduler.close()

    def test_mask_preview_http_prepares_all_tab_records(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        req = {**PARAMS, "mode": "mask", "S": 181, "precache_layers": True,
               "layer_cache_revision": "global", "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "skintone": {"on": False}, "second_band": {"on": True}}
        try:
            with patch.object(U, "LAYER_PRECACHE", scheduler), \
                 patch.object(U, "api_run", return_value={"version": 1}), \
                 patch.object(U, "queue_mask_precache"):
                request = Request(base + "/api/run", json.dumps(req).encode(),
                                  {"Content-Type": "application/json", "Origin": base})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(json.load(response)["layer_precache"]["status"], "queued")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                with urlopen(base + "/api/layer-precache", timeout=5) as response:
                    status = json.load(response)
                self.assertEqual(status["mode"], "mask")
                self.assertTrue(all(status["records"][mode]["cached"]
                                    and status["tabs"][mode]["state"] == "ready"
                                    for mode in ("frequency", "dnb", "combine")))
        finally:
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_latest_preparation_exact_pixels_and_foreground_reuse(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.04)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"method": "median_fast", "radius": 2, "blur_radius": 3},
                            "skintone": {"on": False}})
        newest = {**p, "frequency": {**p["frequency"], "radius": 3}}
        with patch.object(U, "LAYER_PRECACHE", scheduler):
            try:
                with patch.object(FS, "split", wraps=FS.split) as split:
                    U.queue_layer_precache(p)
                    U.queue_layer_precache(newest)
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
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "combine", "S": 181,
                            "frequency": {"on": False, "radius": 2},
                            "skintone": {"on": False}})
        with patch.object(U, "LAYER_PRECACHE", scheduler):
            try:
                U.queue_layer_precache(p, "revision-1")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                records = U.layer_cache_status()["records"]
                self.assertEqual(set(records), {"combine", "combine_bands", "frequency"})
                self.assertEqual(records["combine"], {"generation": 1, "stamp": "revision-1", "cached": True})
                self.assertEqual(records["frequency"], {"generation": 1, "stamp": "revision-1", "cached": True})
                self.state["landmark_generation"] = 2
                self.assertFalse(U.layer_cache_status()["records"]["combine"]["cached"])
                self.state["landmark_generation"] = 1
                self.assertTrue(U.layer_cache_status()["records"]["combine"]["cached"])
                with patch.object(U.LAYER_CACHE, "memory", return_value=0):
                    self.assertTrue(U.layer_cache_status()["records"]["combine"]["cached"])
                U.LAYER_CACHE.clear()
                self.assertFalse(U.layer_cache_status()["records"]["combine"]["cached"])
            finally:
                scheduler.close()

    def test_mix_preview_yields_to_foreground_without_superseding_worker(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        req = {**PARAMS, "mode": "combine", "S": 181, "precache_layers": True,
               "layer_cache_revision": "global", "frequency": {"on": True, "radius": 2, "blur_radius": 3},
               "skintone": {"on": False}, "second_band": {"on": True}}
        started, release, foreground = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_split = U.cached_fs_split

        def delayed(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release split")
            return original_split(*args, **kwargs)

        def run(request):
            if request.get("mix_only"):
                foreground.set()
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

        try:
            with patch.object(U, "LAYER_PRECACHE", scheduler), \
                 patch.object(U, "api_run", side_effect=run), \
                 patch.object(U, "queue_mask_precache"), \
                 patch.object(U, "cached_fs_split", side_effect=delayed):
                post(req)
                self.assertTrue(started.wait(5))
                sequence = scheduler.status()["sequence"]
                self.assertTrue(U.matching_layer_preparation(
                    {**req, "mix_only": True, "combine": {"fs_mix": 40, "dnb_mix": 60}}))
                self.assertFalse(U.matching_layer_preparation({**req, "mix_only": True, "strength": .4}))
                request_thread = threading.Thread(target=changed_mix)
                request_thread.start()
                try:
                    self.assertTrue(U.FOREGROUND_WAITING.wait(5))
                    release.set()
                    self.assertTrue(foreground.wait(5))
                    request_thread.join(5)
                    self.assertFalse(request_thread.is_alive())
                    self.assertFalse(errors)
                    self.assertEqual(scheduler.status()["sequence"], sequence)
                    self.assertEqual(self.settle(scheduler)["status"], "ready")
                    self.assertEqual(scheduler.status()["sequence"], sequence)
                finally:
                    release.set()
                    request_thread.join(5)
        finally:
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_combine_publishes_shared_fs_before_bands_and_tracks_eviction(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
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

        with patch.object(U, "LAYER_PRECACHE", scheduler), patch.object(D, "band_stack", side_effect=delayed):
            try:
                U.queue_layer_precache(p, "revision-1")
                self.assertTrue(entered.wait(5))
                status = U.layer_cache_status()
                self.assertTrue(status["records"]["frequency"]["cached"])
                self.assertNotIn("combine", status["records"])
                self.assertNotIn("dnb", status["records"])
                release.set()
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertTrue(U.layer_cache_status()["records"]["combine"]["cached"])
                frequency = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                                            "frequency": {"on": True, "radius": 2, "blur_radius": 3},
                                            "skintone": {"on": False}})
                with patch.object(FS, "split", wraps=FS.split) as split, \
                     patch.object(FS, "skin_filter", wraps=FS.skin_filter) as blur:
                    U.send_skintone_parallel(frequency)
                    self.assertEqual(split.call_count, 0)
                    self.assertEqual(blur.call_count, 0)
                fs_keys = U.LAYER_PRECACHE_RECORDS["frequency"][2]
                with U.LAYER_CACHE.lock:
                    _, size = U.LAYER_CACHE.entries.pop(fs_keys[-1])
                    U.LAYER_CACHE.bytes -= size
                status = U.layer_cache_status()["records"]
                self.assertFalse(status["frequency"]["cached"])
                self.assertFalse(status["combine"]["cached"])
            finally:
                release.set()
                scheduler.close()

    def test_combine_does_not_claim_unprepared_directional_or_disabled_fs(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        base = {**PARAMS, "mode": "combine", "S": 181,
                "frequency": {"on": False, "radius": 2}, "skintone": {"on": False}}
        with patch.object(U, "LAYER_PRECACHE", scheduler):
            try:
                p = U.parse_params({**base, "combine": {"fs_mix": 0, "dnb_mix": 100}})
                U.queue_layer_precache(p, "no-fs")
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertNotIn("frequency", U.layer_cache_status()["records"])
                self.assertTrue(U.layer_cache_status()["records"]["combine"]["cached"])
                p = {**p, "combine": {"fs_mix": 100, "dnb_mix": 100},
                     "directional": {**U.DB.DEFAULTS, "on": True}}
                U.queue_layer_precache(p, "directional")
                self.assertEqual(self.settle(scheduler)["status"], "skipped")
                records = U.layer_cache_status()["records"]
                self.assertNotEqual(records["combine"]["stamp"], "directional")
                self.assertNotIn("frequency", records)
            finally:
                scheduler.close()

    def test_cancelled_computation_never_commits_and_foreground_waits(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181,
                            "frequency": {"on": False, "radius": 2}})
        started, release, foreground = threading.Event(), threading.Event(), threading.Event()
        original = FS.split
        def slow(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release timed out")
            return original(*args, **kwargs)
        def request():
            scheduler.reset()
            with U.LOCK:
                foreground.set()
        with patch.object(U, "LAYER_PRECACHE", scheduler), patch.object(FS, "split", side_effect=slow):
            try:
                U.queue_layer_precache(p)
                self.assertTrue(started.wait(5))
                thread = threading.Thread(target=request)
                thread.start()
                self.assertFalse(foreground.wait(.05))
                release.set()
                thread.join(5)
                self.assertTrue(foreground.is_set())
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
            finally:
                release.set()
                scheduler.close()

    def test_low_or_unknown_memory_skips_before_allocating(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181})
        with patch.object(U, "LAYER_PRECACHE", scheduler), patch.object(FS, "split", wraps=FS.split) as split:
            try:
                for free in (0, None):
                    U.LAYER_CACHE.memory = lambda: free
                    scheduler.reset()
                    U.queue_layer_precache(p)
                    status = self.settle(scheduler)
                    self.assertEqual(status["status"], "skipped")
                    self.assertIn("RAM", status["message"])
                self.assertEqual(split.call_count, 0)
                self.assertEqual(U.LAYER_CACHE.bytes, 0)
            finally:
                scheduler.close()

    def test_mask_and_foreground_locks_exclude_preparation(self):
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
        p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
        with patch.object(U, "LAYER_PRECACHE", scheduler), patch.object(FS, "split", wraps=FS.split) as split:
            try:
                with U.FULL_MASK_LOCK, U.LOCK:
                    U.queue_layer_precache(p)
                    time.sleep(.15)
                    self.assertEqual(split.call_count, 0)
                self.assertEqual(self.settle(scheduler)["status"], "ready")
                self.assertEqual(split.call_count, 1)
            finally:
                scheduler.close()

    def test_http_preview_enables_preparation_and_cancels_same_origin_only(self):
        scheduler = MaskPrecache(U.precache_layers, delay=10)
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
            with patch.object(U, "LAYER_PRECACHE", scheduler), \
                 patch.object(U, "api_run", return_value={"version": 1}), \
                 patch.object(U, "queue_mask_precache"):
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
        scheduler = MaskPrecache(U.precache_layers, delay=.01)
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
            with patch.object(U, "LAYER_PRECACHE", scheduler), patch.object(FS, "split", side_effect=slow):
                p = U.parse_params({**PARAMS, "mode": "frequency", "S": 181, "frequency": {"on": False}})
                U.queue_layer_precache(p)
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
