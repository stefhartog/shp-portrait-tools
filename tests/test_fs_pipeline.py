"""Bounded FS compute overlap, exact RGB16 output and serialized control."""

from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import ImageCms

from shp_photocraft_tools.modules.dnb import frequency_separation as FS, progress, skintone as ST
from shp_photocraft_tools.modules.dnb import ui_server as U
from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge
from shp_photocraft_tools.shared.photocraft import ControlError
from test_photocraft import FakePhotoCraft


class SkinFake(FakePhotoCraft):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.control_threads = []

    def call(self, method, **params):
        self.control_threads.append(threading.get_ident())
        return super().call(method, **params)

    def run(self, command, **params):
        self.control_threads.append(threading.get_ident())
        if command in ("layer.groupLayers", "layer.newAdjustmentLayer.colorLookup"):
            self.calls.append((command, params))
            return {"layer": 77}
        return super().run(command, **params)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = np.random.default_rng(22).uniform(.1, .9, (48, 64, 3)).astype(np.float32)
        self.mask = np.random.default_rng(23).uniform(0, 1, (48, 64)).astype(np.float32)
        self.icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        self.old_state, self.old_bridge = U.STATE.copy(), U.BRIDGE.copy()
        self.addCleanup(lambda: (U.STATE.clear(), U.STATE.update(self.old_state),
                                 U.BRIDGE.clear(), U.BRIDGE.update(self.old_bridge)))
        U.STATE.update(img=self.source, source_img=self.source, source_icc=self.icc,
                       source_opaque=True, skintone_error=None)
        U.BRIDGE["doc"] = {"index": 1, "name": "portrait:one.png", "width": 64, "height": 48}

    def bridge(self, name):
        b = Bridge(port=1, token_file=self.root/"unused",
                   exchange=self.root/name, read_root=self.root)
        b.client = SkinFake(b)
        return b

    def params(self, method="gaussian"):
        return U.parse_params({"mode": "frequency", "S": 181,
                               "frequency": {"method": method, "radius": 2, "blur_radius": 4}})

    def test_mask_placement_defaults_migrates_and_persists(self):
        import json
        path = self.root / "settings.json"
        with patch.object(U, "GLOBAL_SETTINGS", path), patch.object(U, "PRESETS", self.root / "presets.json"):
            self.assertEqual(U.read_global_settings()["mask_placement"], "folder")
            settings = U.read_global_settings()
            settings.pop("mask_placement")
            path.write_text(json.dumps(settings))
            self.assertEqual(U.read_global_settings()["mask_placement"], "folder")
            U.api_global_settings({"layer_transfer": settings["layer_transfer"], "mask_placement": "layers"})
            self.assertEqual(U.read_global_settings()["mask_placement"], "layers")
            U.api_global_settings({"layer_transfer": settings["layer_transfer"]})
            self.assertEqual(U.read_global_settings()["mask_placement"], "layers")
            with self.assertRaises(ValueError):
                U.api_global_settings({"layer_transfer": settings["layer_transfer"], "mask_placement": "invalid"})

    def test_folder_and_legacy_layer_masks_include_empty_masks(self):
        layers = {key: FS.rgb16(value) for key, value in FS.separate(
            self.source, self.mask, "gaussian", 2, 4).items()}
        for placement in ("folder", "layers"):
            for empty in (False, True):
                with self.subTest(placement=placement, empty=empty):
                    b = self.bridge(f"{placement}-{empty}")
                    mask = U.u8(np.zeros_like(self.mask) if empty else self.mask)
                    with patch.object(b, "_group_skin_mask", wraps=b._group_skin_mask) as attach:
                        b.send_skintone(layers, ST.settings({}), mask, U.BRIDGE["doc"],
                                        self.icc, mask_placement=placement)
                    self.assertEqual(attach.call_count, 1 if placement == "folder" else 2)
                    if placement == "folder":
                        self.assertEqual(attach.call_args.args[1], 77)
                    self.assertEqual(list(b.exchange.iterdir()), [])

    def test_disabled_skintone_send_omits_hue_but_preserves_fs_and_blur_masks(self):
        p = self.params()
        p["skintone"]["on"] = False
        for mask in (self.mask, np.zeros_like(self.mask)):
            b = self.bridge("correction-off")
            with patch.object(U, "bridge", return_value=b), \
                    patch.object(U, "full_skin_mask", return_value=mask), \
                    patch.object(ST, "tables", side_effect=AssertionError("no LUT")):
                U.send_skintone_parallel(p)
            self.assertFalse(any(cmd == "layer.newAdjustmentLayer.colorLookup" for cmd, _ in b.client.calls))
            names = [args.get("name") for cmd, args in b.client.calls if cmd == "layer.setProps"]
            self.assertIn("Color", names)
            self.assertIn("Blur", names)
            self.assertIn("Detail", names)
            self.assertNotIn("Hue", names)
            if not mask.any():
                self.assertEqual(sum(cmd == "layer.layerMask.hideAll" for cmd, _ in b.client.calls), 1)
            self.assertEqual(list(b.exchange.iterdir()), [])
            self.assertEqual(len(b.client.documents), 1)
            self.assertEqual(set(b.client.control_threads), {threading.get_ident()})

    def test_rgb16_masks_profiles_recipe_and_control_calls_match_serial_baseline(self):
        for method in ("gaussian", "median"):
            for mask in (self.mask, np.zeros_like(self.mask), np.ones_like(self.mask)):
                with self.subTest(method=method, mask=float(mask.mean())):
                    p = self.params(method)
                    original = self.bridge("serial")
                    layers = ST.separate(self.source, mask, method, 2, 4, native=True)
                    baseline = original.send_skintone(layers, p["skintone"], U.u8(mask),
                                                     U.BRIDGE["doc"], self.icc)
                    optimized = self.bridge("parallel")
                    with patch.object(U, "bridge", return_value=optimized), \
                            patch.object(U, "full_skin_mask", return_value=mask):
                        result = U.send_skintone_parallel(p)
                    self.assertEqual(result, baseline)
                    def commands(client):
                        return [(cmd, {**args, "path": Path(args["path"]).name} if "path" in args else args)
                                for cmd, args in client.calls]
                    self.assertEqual(commands(optimized.client), commands(original.client))
                    self.assertEqual(optimized.client.file_profiles, original.client.file_profiles)
                    for a, b in zip(optimized.client.files, original.client.files):
                        np.testing.assert_array_equal(a, b)
                    self.assertEqual(set(optimized.client.control_threads), {threading.get_ident()})
                    self.assertEqual(list(optimized.exchange.iterdir()), [])
                    self.assertEqual(len(optimized.client.documents), 1)

    def test_mask_and_split_overlap_then_blur_overlaps_color_encoding(self):
        b = self.bridge("overlap")
        mask_started, split_started, color_encoded = (threading.Event() for _ in range(3))
        split, skin_filter, write = FS.split, FS.skin_filter, b._write_rgb

        def mask(p):
            mask_started.set()
            self.assertTrue(split_started.wait(5))
            return self.mask

        def separate(*args):
            split_started.set()
            self.assertTrue(mask_started.wait(5))
            return split(*args)

        def filter_(img, mask, radius, method="gaussian"):
            if radius == 4:
                self.assertTrue(color_encoded.wait(5))
            return skin_filter(img, mask, radius, method)

        def encode(rel, *args):
            if "Color" in rel:
                color_encoded.set()
            return write(rel, *args)

        with patch.object(U, "bridge", return_value=b), patch.object(U, "full_skin_mask", side_effect=mask), \
                patch.object(FS, "split", side_effect=separate), patch.object(FS, "skin_filter", side_effect=filter_), \
                patch.object(b, "_write_rgb", side_effect=encode):
            U.send_skintone_parallel(self.params())
        self.assertTrue(color_encoded.is_set())
        self.assertEqual(set(b.client.control_threads), {threading.get_ident()})

    def test_worker_failures_never_insert_and_leave_no_files_or_threads(self):
        for stage in ("mask", "split", "blur", "encode"):
            with self.subTest(stage=stage):
                b = self.bridge(stage)
                skin_filter = FS.skin_filter
                def filter_(img, mask, radius, method="gaussian"):
                    if stage == "blur" and radius == 4:
                        raise ValueError("broken blur")
                    return skin_filter(img, mask, radius, method)
                with patch.object(U, "bridge", return_value=b), \
                        patch.object(U, "full_skin_mask", side_effect=ValueError("broken mask") if stage == "mask" else None,
                                     return_value=self.mask), \
                        patch.object(FS, "skin_filter", side_effect=filter_):
                    if stage == "split":
                        with patch.object(FS, "split", side_effect=ValueError("broken split")), self.assertRaisesRegex(ValueError, "broken"):
                            U.send_skintone_parallel(self.params())
                    elif stage == "encode":
                        with patch.object(b, "_write_rgb", side_effect=ValueError("broken encode")), self.assertRaisesRegex(ValueError, "broken"):
                            U.send_skintone_parallel(self.params())
                    else:
                        with self.assertRaisesRegex(ValueError, "broken"):
                            U.send_skintone_parallel(self.params())
                self.assertFalse(any(cmd == "layer.copyToDocument" and params["document"] == 1
                                     for cmd, params in b.client.calls))
                self.assertEqual(len(b.client.documents), 1)
                self.assertEqual(list(b.exchange.iterdir()), [])
                self.assertFalse(any(t.name.startswith("skin-fs") for t in threading.enumerate()))

    def test_color_and_detail_encode_concurrently_and_detail_failure_never_inserts(self):
        b = self.bridge("png-overlap")
        color_started, detail_started = threading.Event(), threading.Event()
        write = b._write_rgb
        def encode(rel, *args):
            if "Color" in rel:
                color_started.set()
                self.assertTrue(detail_started.wait(5))
            elif "Detail" in rel:
                detail_started.set()
                self.assertTrue(color_started.wait(5))
            return write(rel, *args)
        with patch.object(U, "bridge", return_value=b), patch.object(U, "full_skin_mask", return_value=self.mask), \
                patch.object(b, "_write_rgb", side_effect=encode):
            U.send_skintone_parallel(self.params())
        self.assertTrue(color_started.is_set() and detail_started.is_set())
        b = self.bridge("png-failure")
        write = b._write_rgb
        def broken(rel, *args):
            if "Detail" in rel:
                raise ValueError("broken Detail PNG")
            return write(rel, *args)
        with patch.object(U, "bridge", return_value=b), patch.object(U, "full_skin_mask", return_value=self.mask), \
                patch.object(b, "_write_rgb", side_effect=broken), self.assertRaisesRegex(ValueError, "broken Detail PNG"):
            U.send_skintone_parallel(self.params())
        self.assertFalse(any(cmd == "layer.copyToDocument" and params["document"] == 1
                             for cmd, params in b.client.calls))
        self.assertEqual(len(b.client.documents), 1)
        self.assertEqual(list(b.exchange.iterdir()), [])
        self.assertFalse(any(t.name.startswith("skin-fs") for t in threading.enumerate()))

    def test_invalid_future_shape_and_cleanup_failure_are_explicit(self):
        b = self.bridge("cleanup")
        layers = {k: FS.rgb16(v) for k, v in FS.split(self.source).items()}
        job = Future()
        job.set_result(np.zeros((2, 3, 3), np.uint16))
        run = b.client.run
        def fail(command, **params):
            if command == "file.close":
                raise ControlError("close failed")
            return run(command, **params)
        with patch.object(b.client, "run", side_effect=fail), \
                self.assertRaisesRegex(ControlError, "matching RGB16 Blur.*could not close FS scratch"):
            b.send_skintone(layers, ST.settings({}), U.u8(self.mask), U.BRIDGE["doc"],
                            self.icc, blur_future=job)
        self.assertEqual(list(b.exchange.iterdir()), [])

    def test_concurrent_progress_does_not_restore_a_finished_stage(self):
        worker_started, end_worker = threading.Event(), threading.Event()
        token = "test-concurrent-fs-progress"
        def worker():
            worker_started.set()
            self.assertTrue(end_worker.wait(5))
        with progress.operation(token), ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(copy_context().run, progress.call, "mask worker", worker)
            self.assertTrue(worker_started.wait(5))
            progress.call("main split", lambda: None)
            self.assertEqual(progress.snapshot(token)["active"], "mask worker")
            end_worker.set()
            job.result()
            self.assertIsNone(progress.snapshot(token)["active"])
        result = progress.snapshot(token, consume=True)
        self.assertTrue(result["done"])
        self.assertNotIn("_running", result)
        self.assertEqual({s["label"] for s in result["steps"]}, {"main split", "mask worker"})
