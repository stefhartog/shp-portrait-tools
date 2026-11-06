"""Reduced preview cache only; synthetic inputs and no GUI/native services."""

import copy
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import numpy as np

import test_dnb as T

S = T.S


class PreviewCacheTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    post = T.DnbTests.post
    def test_round_trip_restores_images_chart_region_and_pick_without_processing(self):
        self.post("/api/load", {"path": str(self.image)})
        requests = [{**copy.deepcopy(T.PARAMS), "mode": mode} for mode in ("dnb", "mask", "frequency")]
        snapshots = {}
        for request in requests:
            response = self.post("/api/run", request)
            snapshots[request["mode"]] = (response, dict(S.STATE["imgs"]), self.post(
                "/api/color-pick", {"version": response["version"], "u": .5, "v": .5}))
        with patch.object(S.D, "process", side_effect=AssertionError("D&B recomputed")), \
             patch.object(S, "tool_mask", side_effect=AssertionError("mask recomputed")), \
             patch.object(S, "skintone_layers", side_effect=AssertionError("FS recomputed")), \
             patch.object(S, "encode", side_effect=AssertionError("images reencoded")):
            for request in requests:
                response = self.post("/api/run", request)
                previous, images, pick = snapshots[request["mode"]]
                self.assertTrue(response["cached"])
                self.assertEqual(response["stats"], previous["stats"])
                self.assertEqual(S.STATE["imgs"], images)
                self.assertGreater(response["version"], previous["version"])
                self.assertEqual(self.post("/api/color-pick", {
                    "version": response["version"], "u": .5, "v": .5}), pick)
                self.assertEqual(S.STATE["region_box"], (0, 0, 128, 96))
        self.assertEqual(set(S.STATE["preview_cache"]), {"dnb", "mask", "frequency"})
        self.assertLessEqual(sum(e["bytes"] for e in S.STATE["preview_cache"].values()), S.PREVIEW_CACHE_LIMIT)
        for entry in S.STATE["preview_cache"].values():
            self.assertFalse(np.shares_memory(entry["state"]["last"]["reg"], S.STATE["img"]))

    def test_keys_follow_effective_mode_inputs_and_overlay_is_separate(self):
        self.post("/api/load", {"path": str(self.image)})
        for mode, irrelevant, relevant in (
            ("mask", {"strength": .4, "skintone": {"hue_amount": 10}}, {"tone": {"on": True, "mid": .3}}),
            ("dnb", {"frequency": {"method": "median"}, "skintone": {"hue_amount": 10}}, {"layer_blur_at_s181": 2}),
            ("frequency", {"bands": [[2, 8, .2, .5]], "tau": .3, "layer_blur_at_s181": 4}, {"skintone": {"hue_amount": 20}}),
        ):
            request = {**copy.deepcopy(T.PARAMS), "mode": mode}
            self.post("/api/run", request)
            self.assertTrue(self.post("/api/run", {**request, **irrelevant})["cached"])
            self.assertNotIn("cached", self.post("/api/run", {**request, **relevant}))
            self.post("/api/run", request)
            with patch.object(S, "tool_mask", side_effect=AssertionError("processing repeated")), \
                 patch.object(S.D, "process", side_effect=AssertionError("processing repeated")):
                response = self.post("/api/run", {**request, "analysis": {"detect": False, "skin": False, "structure": False}})
                self.assertTrue(response["cached"])
        # Two different calibrations which resolve to the same effective radius reuse output.
        request = {**copy.deepcopy(T.PARAMS), "mode": "frequency",
                   "frequency": {"radius_at_s181": 6, "blur_percent": 200}}
        self.post("/api/run", request)
        request["frequency"]["radius_at_s181"] = 6.01
        self.assertTrue(self.post("/api/run", request)["cached"])
        for mode, changes in (
            ("dnb", [{"bands": [[2, 20, .5, .2]]}, {"strength": .5}, {"edge": .2},
                     {"tau": .3}, {"max_ev": .5}, {"layer_blur_at_s181": 1}]),
            ("frequency", [{"frequency": {"method": "median"}}, {"frequency": {"radius": 9}},
                           {"frequency": {"blur_radius": 30}}, {"skintone": {"hue_center": 20}},
                           {"skintone": {"hue_width": 8}}, {"skintone": {"hue_amount": 40}},
                           {"skintone": {"hue_softness": 2}}]),
        ):
            base = {**copy.deepcopy(T.PARAMS), "mode": mode}
            key = S.preview_key(S.parse_params(base), "whole")
            for change in changes:
                self.assertNotEqual(S.preview_key(S.parse_params({**base, **change}), "whole"), key)

    def test_source_geometry_shared_controls_and_failures_cannot_restore_stale_preview(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        S.STATE["masks"] = {"A": np.ones((96, 128), np.float32),
                            "features": np.zeros((96, 128), np.float32),
                            "colour": np.ones((96, 128), np.float32)}
        request = copy.deepcopy(T.PARAMS)
        self.post("/api/run", request)
        for changed in (
            {"S": 200}, {"view": "detail"}, {"mask": {"on": False}},
            {"structure": {"protect": .8}}, {"color": {"on": True, "hue": 30, "saturation": .5}},
        ):
            self.assertNotIn("cached", self.post("/api/run", {**request, **changed}))
            self.post("/api/run", request)
        self.post("/api/centre", {"u": .2, "v": .2})
        self.assertNotIn("cached", self.post("/api/run", request))
        self.post("/api/load", {"path": str(self.image)})
        self.assertFalse(S.STATE["preview_cache"])
        with self.assertRaises(HTTPError):
            self.post("/api/run", {**request, "generation": loaded["generation"]})
        self.assertNotIn("cached", self.post("/api/run", request))
        bad = {**request, "mode": "frequency"}
        with patch.object(S, "skintone_layers", side_effect=ValueError("fixture failure")):
            with self.assertRaises(HTTPError):
                self.post("/api/run", bad)
        self.assertNotIn("frequency", S.STATE["preview_cache"])

    def test_cache_memory_cap_and_landmark_apply_release_entries(self):
        self.post("/api/load", {"path": str(self.image)})
        self.post("/api/run", T.PARAMS)
        size = S.STATE["preview_cache"]["dnb"]["bytes"]
        with patch.object(S, "PREVIEW_CACHE_LIMIT", size):
            self.post("/api/run", {**T.PARAMS, "mode": "mask"})
            self.assertLessEqual(sum(e["bytes"] for e in S.STATE["preview_cache"].values()), size)
        with patch.object(S, "PREVIEW_CACHE_LIMIT", 0):
            self.post("/api/run", T.PARAMS)
            self.assertFalse(S.STATE["preview_cache"])
        self.post("/api/run", T.PARAMS)
        # Applying landmarks is a distinct invalidation even if S ends up unchanged.
        with patch.object(S, "landmark_subjects", return_value=[
            {"face_info": None, "kp": None, "sc": None, "cues": [("fixture", 181, .2)]}]), \
             patch.object(S.C, "combine", return_value={"S": 181, "confidence": "medium", "sigma": .2,
                                                       "used": [], "dropped": []}):
            S.api_landmark_estimate({"generation": S.STATE["landmark_generation"], "subject": 0, "apply": True})
        self.assertFalse(S.STATE["preview_cache"])
