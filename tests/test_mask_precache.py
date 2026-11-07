"""Debounce/coalescing, nonblocking preview scheduling and safe Send handoff."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch
import json
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from shp_photocraft_tools.modules.dnb import ui_server as U, dodge_burn as D
from shp_photocraft_tools.modules.dnb.mask_precache import MaskPrecache


class MaskPrecacheTests(unittest.TestCase):
    def setUp(self):
        self.old = U.STATE.copy()
        self.addCleanup(lambda: (U.STATE.clear(), U.STATE.update(self.old)))
        img = np.full((48, 64, 3), .5, np.float32)
        U.STATE.update(img=img, source_img=img, masks=None, path="synthetic.png",
                       landmark_generation=1, source_icc=None, icc=None, full_mask_cache=None)
        self.p = U.parse_params({"mode": "frequency", "S": 181})
        self.scheduler = MaskPrecache(U.precache_mask, delay=.04)
        self.addCleanup(self.scheduler.close)
        self.patch = patch.object(U, "MASK_PRECACHE", self.scheduler)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def settled(self):
        deadline = time.monotonic() + 5
        while self.scheduler.status()["status"] in ("queued", "running"):
            if time.monotonic() >= deadline:
                self.fail("background calculation did not settle")
            threading.Event().wait(.005)
        return self.scheduler.status()

    def test_debounce_latest_settings_and_ignore_fs_dnb_correction_only_edits(self):
        with patch.object(D, "combined_mask", wraps=D.combined_mask) as compute:
            U.queue_mask_precache(self.p)
            newest = {**self.p, "tone_strength": .3}
            U.queue_mask_precache(newest)
            seq = self.scheduler.status()["sequence"]
            U.queue_mask_precache({**newest, "frequency": {**newest["frequency"], "radius": 24}})
            U.queue_mask_precache({**newest, "mode": "dnb", "bands": [(.5, 2, 1, .2)], "strength": 4})
            self.assertEqual(seq, self.scheduler.status()["sequence"])
            self.assertEqual(self.settled()["status"], "ready")
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(compute.call_args.args[5], .3)
        with patch.object(D, "combined_mask", side_effect=AssertionError("must reuse precache")):
            U.full_skin_mask(newest)

    def test_preview_scheduling_and_status_stay_responsive_while_send_waits_without_duplicate_compute(self):
        started, release = threading.Event(), threading.Event()
        original = D.combined_mask
        def slow(*args):
            started.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        with patch.object(D, "combined_mask", side_effect=slow) as compute, ThreadPoolExecutor(max_workers=1) as pool:
            began = time.perf_counter()
            U.queue_mask_precache(self.p)
            self.assertLess(time.perf_counter() - began, .05)
            self.assertTrue(started.wait(5))
            self.assertEqual(U.mask_cache_status()["status"], "running")
            self.assertFalse(U.mask_cache_status()["cached"])
            began = time.perf_counter()
            U.queue_mask_precache(self.p)
            self.scheduler.status()
            self.assertLess(time.perf_counter() - began, .05)
            def send():
                with U.LOCK:
                    self.scheduler.select(U.mask_request_key(self.p))
                    return U.full_skin_mask(self.p)
            job = pool.submit(send)
            self.assertFalse(job.done())
            release.set()
            mask = job.result(timeout=5)
            self.assertEqual(compute.call_count, 1)
            self.assertFalse(mask.flags.writeable)
        self.assertEqual(self.settled()["status"], "ready")

    def test_superseded_calculation_cannot_publish_over_new_settings(self):
        started, release = threading.Event(), threading.Event()
        original = D.combined_mask
        def slow(*args):
            if args[5] == self.p["tone_strength"]:
                started.set()
                self.assertTrue(release.wait(5))
            return original(*args)
        with patch.object(D, "combined_mask", side_effect=slow):
            U.queue_mask_precache(self.p)
            self.assertTrue(started.wait(5))
            newest = {**self.p, "tone_strength": .2}
            U.queue_mask_precache(newest)
            release.set()
            self.assertEqual(self.settled()["status"], "ready")
        key = U.full_mask_key(newest, U.STATE["img"], U.STATE["masks"])
        self.assertEqual(U.STATE["full_mask_cache"][0], key)

    def test_source_reset_discards_inflight_result_and_background_errors_remain_visible(self):
        started, release = threading.Event(), threading.Event()
        original = D.combined_mask
        def slow(*args):
            started.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        with patch.object(D, "combined_mask", side_effect=slow):
            U.queue_mask_precache(self.p)
            self.assertTrue(started.wait(5))
            self.scheduler.reset()
            U.STATE.update(img=U.STATE["img"].copy(), landmark_generation=2, full_mask_cache=None)
            release.set()
        # A new request is serialized behind the old computation and surfaces real errors.
        with patch.object(D, "combined_mask", side_effect=ValueError("mask failed")):
            U.queue_mask_precache(self.p)
            status = self.settled()
            self.assertEqual(status["status"], "error")
            self.assertIn("mask failed", status["error"])
            self.assertFalse(U.mask_cache_status()["cached"])
        self.assertIsNone(U.STATE["full_mask_cache"])
        self.assertIsNotNone(U.full_skin_mask(self.p))

    def test_over_budget_does_not_start_worker_and_both_normal_modes_share_jobs(self):
        for mode in ("blemish",):
            U.queue_mask_precache({**self.p, "mode": mode})
            self.assertIsNone(self.scheduler.thread)
        U.STATE["img"] = np.broadcast_to(np.float32(.5), (9000, 9000, 3))
        U.queue_mask_precache(self.p)
        self.assertIsNone(self.scheduler.thread)
        self.assertEqual(self.scheduler.status()["status"], "skipped")
        self.assertIn("budget", self.scheduler.status()["message"])

    def test_http_preview_hook_status_and_optional_disable_are_explicit(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def post(body):
            request = Request(base + "/api/run", json.dumps(body).encode(),
                              {"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        try:
            with patch.object(U, "api_run", return_value={"version": 1, "avers": 1, "stats": {}}):
                response = post({"mode": "frequency", "S": 181})
                self.assertIn(response["mask_precache"]["status"], ("queued", "running", "ready"))
                self.assertEqual(self.settled()["status"], "ready")
                with urlopen(base + "/api/mask-precache", timeout=5) as response:
                    self.assertEqual(json.load(response)["status"], "ready")
                saved = U.STATE["full_mask_cache"]
                response = post({"mode": "frequency", "S": 181, "precache_mask": False})
                self.assertEqual(response["mask_precache"]["status"], "idle")
                self.assertTrue(response["mask_precache"]["cached"])
                self.assertFalse(response["mask_precache"]["enabled"])
                self.assertIs(U.STATE["full_mask_cache"], saved)
                with self.assertRaises(HTTPError) as error:
                    post({"mode": "frequency", "S": 181, "precache_mask": "false"})
                self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cache_status_tracks_retained_mask_send_hits_stale_settings_and_sources(self):
        U.queue_mask_precache({**self.p, "mode": "mask"})
        self.assertEqual(self.settled()["status"], "ready")
        self.assertTrue(U.mask_cache_status()["cached"])
        # A completed job does not imply its result is still retained.
        U.STATE["full_mask_cache"] = None
        self.assertFalse(U.mask_cache_status()["cached"])
        U.full_skin_mask(self.p)
        self.assertTrue(U.mask_cache_status()["cached"])
        newest = {**self.p, "tone_strength": .2}
        U.STATE["mask_status_key"] = U.mask_request_key(newest)
        self.assertFalse(U.mask_cache_status()["cached"])
        U.full_skin_mask(newest)
        self.assertTrue(U.mask_cache_status()["cached"])
        with patch.object(D, "combined_mask", side_effect=AssertionError("reuse matching mask")):
            U.full_skin_mask({**newest, "mode": "dnb"})
        self.assertTrue(U.mask_cache_status()["cached"])
        U.STATE["img"] = U.STATE["img"].copy()
        U.STATE["mask_status_key"] = U.mask_request_key(newest)
        self.assertFalse(U.mask_cache_status()["cached"])
