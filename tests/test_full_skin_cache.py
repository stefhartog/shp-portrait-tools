"""Final full-resolution masks: cross-mode reuse, exact math, bounded safe storage."""

from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import unittest
from unittest.mock import patch

import numpy as np

from shp_photocraft_tools.modules.dnb import ui_server as U, dodge_burn as D, progress


class FullMaskCacheTests(unittest.TestCase):
    def setUp(self):
        self.old_state = U.STATE.copy()
        self.addCleanup(lambda: (U.STATE.clear(), U.STATE.update(self.old_state)))
        self.img = np.random.default_rng(9).uniform(.15, .85, (48, 64, 3)).astype(np.float32)
        masks = {"A": np.full((48, 64), .85, np.float32),
                 "features": np.zeros((48, 64), np.float32), "colour": np.ones((48, 64), np.float32)}
        U.STATE.update(img=self.img, source_img=self.img, masks=masks, path="synthetic.png",
                       landmark_generation=41, icc=b"profile", source_icc=b"profile", full_mask_cache=None)
        self.req = {"mode": "frequency", "S": 181,
                    "mask": {"on": True, "feather": .2}, "tone": {"on": True},
                    "structure": {"protect": .2, "mm": 1, "thr": .1},
                    "bands": [[.5, 2, .5, .2], [2, 6, .3, .2]]}
        self.p = U.parse_params(self.req)

    def test_fs_then_dnb_reuse_exact_final_mask_without_changing_dnb_math(self):
        base = U.base_skin_mask(self.img, U.STATE["masks"], self.p, self.p["S"])
        expected_mask, _ = D.combined_mask(self.img, self.p["S"], base, self.p["shadows"], self.p["highlights"],
                                         self.p["tone_strength"], self.p["protect"], self.p["protect_mm"], self.p["protect_thr"])
        p = U.parse_params({**self.req, "mode": "dnb"})
        original = D.process(self.img, p["S"], p["bands"], p["strength"], p["edge"], p["tau"], p["max_ev"],
                             p["shadows"], p["highlights"], mask=base, tone_strength=p["tone_strength"],
                             protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"])
        with patch.object(D, "combined_mask", wraps=D.combined_mask) as compute:
            mask = U.full_skin_mask(self.p)
            cs, after, total = U.dnb_full_result(p)
            self.assertEqual(compute.call_count, 1)
        np.testing.assert_array_equal(mask, expected_mask)
        for actual, expected in ((cs, original[0]), (after, original[1]), (total, original[-1])):
            np.testing.assert_array_equal(actual, expected)
        self.assertFalse(mask.flags.writeable)
        with self.assertRaises(ValueError):
            mask[0, 0] = 0
        with self.assertRaises(ValueError):
            mask.flags.writeable = True
        np.testing.assert_array_equal(U.full_skin_mask(self.p), expected_mask)

    def test_different_effective_masks_miss_but_fs_and_dnb_controls_do_not(self):
        with patch.object(D, "combined_mask", wraps=D.combined_mask) as compute:
            U.full_skin_mask(self.p)
            dnb = U.parse_params({**self.req, "mode": "dnb", "strength": 4, "edge": .4, "tau": .1})
            U.full_skin_mask(dnb)
            changed_frequency = U.parse_params({**self.req, "frequency": {"radius": 24, "blur_radius": 48},
                                                "skintone": {"hue_center": 40}})
            U.full_skin_mask(changed_frequency)
            self.assertEqual(compute.call_count, 1)
            different = U.parse_params({**self.req, "mask": {"on": True, "features": False, "feather": 1}})
            U.full_skin_mask(different)
            self.assertEqual(compute.call_count, 2)

    def test_each_relevant_setting_is_in_the_key_and_disabled_controls_are_ignored(self):
        key = U.full_mask_key(self.p, self.img, U.STATE["masks"])
        for name, value in (("S", 362), ("tone_strength", .3), ("protect", .4),
                            ("protect_mm", 2), ("protect_thr", .2),
                            ("shadows", None), ("highlights", None)):
            p = {**self.p, name: value}
            self.assertNotEqual(U.full_mask_key(p, self.img, U.STATE["masks"]), key, name)
        p = copy.deepcopy(self.p)
        p["color_filter"].update(on=True, hue=30, saturation=.4)
        color_key = U.full_mask_key(p, self.img, U.STATE["masks"])
        for name in p["color_filter"]:
            other = copy.deepcopy(p)
            other["color_filter"][name] = False if name == "on" else other["color_filter"][name] + .1
            self.assertNotEqual(U.full_mask_key(other, self.img, U.STATE["masks"]), color_key, name)
        p = U.parse_params({**self.req, "mask": {"on": False}, "tone": {"on": False}, "structure": {"protect": 0}})
        other = copy.deepcopy(p)
        other["mask_flags"].update(feather=2, features=False, colour=False)
        other.update(protect_mm=8, protect_thr=.9, tone_strength=.2)
        other["color_filter"].update(hue=70, saturation=.8)
        self.assertEqual(U.full_mask_key(p, self.img, U.STATE["masks"]),
                         U.full_mask_key(other, self.img, U.STATE["masks"]))

    def test_source_generation_profiles_components_and_in_place_changes_invalidate(self):
        mutations = [
            lambda: U.STATE.update(landmark_generation=42),
            lambda: U.STATE.update(img=self.img.copy()),
            lambda: self.img.__setitem__((0, 0, 0), .1),
            lambda: U.STATE.update(source_img=self.img + .001),
            lambda: U.STATE.update(source_icc=b"other-profile"),
            lambda: U.STATE["masks"]["A"].__setitem__((0, 0), .3),
            lambda: U.STATE["masks"].update(features=np.ones((48, 64), np.float32)),
            lambda: U.STATE.update(masks=None),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                U.STATE["full_mask_cache"] = None
                with patch.object(D, "combined_mask", wraps=D.combined_mask) as compute:
                    U.full_skin_mask(self.p)
                    mutate()
                    U.full_skin_mask(self.p)
                    self.assertEqual(compute.call_count, 2)

    def test_concurrent_requests_compute_once_and_inflight_source_change_is_rejected(self):
        with patch.object(D, "combined_mask", wraps=D.combined_mask) as compute, ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: U.full_skin_mask(self.p), range(4)))
            self.assertEqual(compute.call_count, 1)
            for a in values[1:]:
                np.testing.assert_array_equal(a, values[0])
        U.STATE["full_mask_cache"] = None
        started, release = threading.Event(), threading.Event()
        original = D.combined_mask
        def paused(*args):
            started.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        with patch.object(D, "combined_mask", side_effect=paused), ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(U.full_skin_mask, self.p)
            self.assertTrue(started.wait(5))
            U.STATE["landmark_generation"] += 1
            release.set()
            with self.assertRaisesRegex(ValueError, "changed during mask preparation"):
                job.result()
        self.assertIsNone(U.STATE["full_mask_cache"])

    def test_budget_empty_mask_and_progress_hit_recompute(self):
        token = "full-skin-cache-progress-test"
        with progress.operation(token):
            first = U.full_skin_mask(self.p)
            U.full_skin_mask(self.p)
        steps = progress.snapshot(token, consume=True)["steps"]
        self.assertIn("Recomputing full-resolution skin mask", [s["label"] for s in steps])
        self.assertIn("Reusing cached full-resolution skin mask", [s["label"] for s in steps])
        self.assertLessEqual(sum(a.nbytes for a in U.STATE["full_mask_cache"][1:] if a is not None), U.FULL_MASK_CACHE_LIMIT)
        U.STATE["full_mask_cache"] = None
        with patch.object(U, "FULL_MASK_CACHE_LIMIT", 1), patch.object(D, "combined_mask", wraps=D.combined_mask) as compute:
            np.testing.assert_array_equal(U.full_skin_mask(self.p), first)
            U.full_skin_mask(self.p)
            self.assertEqual(compute.call_count, 2)
            self.assertIsNone(U.STATE["full_mask_cache"])
        white = U.parse_params({**self.req, "mask": {"on": False}, "tone": {"on": False}, "structure": {"protect": 0}})
        total, structure = U.cached_final_skin_mask(white)
        self.assertIsNone(total)
        self.assertIsNone(structure)
        np.testing.assert_array_equal(U.full_skin_mask(white), np.ones((48, 64)))
