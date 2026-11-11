"""Disposable Patch Fill graphs, masks, jobs and unchanged-pixel regressions."""

import base64
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.crop import expand as X, server as S
from shp_photocraft_tools.modules.crop.photocraft_bridge import Bridge
from test_photocraft import FakePhotoCraft


def mask_data(mask):
    return "data:image/png;base64," + base64.b64encode(X._png(mask)).decode()


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.source = Image.fromarray(np.random.default_rng(42).integers(0, 256, (96, 128, 3), dtype=np.uint8))
        self.mask = Image.new("L", self.source.size)
        self.mask.paste(255, (35, 25, 80, 65))
        self.generated = Image.new("RGB", self.source.size, (1, 150, 220))

    def test_mask_validation_coordinates_rgba_and_empty(self):
        rgba = Image.new("RGBA", self.source.size)
        rgba.paste((255, 60, 90, 255), (35, 25, 80, 65))
        np.testing.assert_array_equal(X.patch_mask(mask_data(rgba), self.source.size), self.mask)
        for invalid in (None, "", "data:image/png;base64,???",
                        mask_data(Image.new("L", self.source.size)),
                        mask_data(Image.new("L", (20, 20), 255))):
            with self.subTest(invalid=str(invalid)[:40]), self.assertRaises(X.ExpandError):
                X.patch_mask(invalid, self.source.size)

    def test_graph_reuses_native_fill_loaders_settings_and_real_mask(self):
        settings = {**X.DEFAULTS, "fill_steps": 31, "fill_guidance": 42, "fill_clip_device": "default"}
        graph = X.build_patch_graph("source.png", "mask.png", "replace with flowers", 123, settings)
        self.assertNotIn("2", graph)
        self.assertNotIn("ImagePadForOutpaint", [node["class_type"] for node in graph.values()])
        self.assertEqual(graph["patch_image"]["inputs"]["image"], "mask.png")
        self.assertEqual(graph["patch_mask"]["inputs"], {"image": ["patch_image", 0], "channel": "red"})
        self.assertEqual(graph["5"]["inputs"]["mask"], ["patch_mask", 0])
        self.assertEqual(graph["14"]["inputs"]["mask"], ["7", 0])
        self.assertEqual(graph["16"]["inputs"]["steps"], 31)
        self.assertEqual(graph["12"]["inputs"]["guidance"], 42)
        self.assertEqual(graph["9"]["inputs"]["device"], "default")
        self.assertIn("replace with flowers", graph["11"]["inputs"]["text"])
        self.assertEqual(graph["out"]["inputs"]["images"], ["27", 0])
        for node in graph.values():
            for value in node["inputs"].values():
                if isinstance(value, list):
                    self.assertIn(value[0], graph)

    def client(self):
        client = Mock()
        client.upload.side_effect = ["source.png", "mask.png"]
        client.queue.return_value = "prompt-id"
        client.wait.return_value = {"out": {"images": [{"filename": "result.png"}]}}
        client.view.return_value = X._png(self.generated)
        return client

    def test_preserves_dimensions_outside_and_feathers_only_inward(self):
        for feather in (0, 8, 64):
            client = self.client()
            job = X.Job()
            with self.subTest(feather=feather), patch.object(X, "status", return_value={"ok": True}), \
                    patch.object(X, "Comfy", return_value=client):
                results = X.patch_variations(self.source, self.mask, "", job, 10,
                                             {"fill_feathering": feather}, 2)
            self.assertEqual(len(results), 2)
            self.assertIs(job.result, results[0])
            self.assertEqual(client.upload.call_count, 2)
            for result in results:
                alpha = np.asarray(result["layer"])[..., 3]
                self.assertEqual(result["canvas"].size, self.source.size)
                self.assertEqual(result["margins"], (0, 0, 0, 0))
                outside = np.asarray(self.mask) == 0
                np.testing.assert_array_equal(np.asarray(result["canvas"])[outside], np.asarray(self.source)[outside])
                self.assertTrue((alpha[outside] == 0).all())
                if feather:
                    self.assertTrue(((alpha > 0) & (alpha < 255)).any())
                else:
                    np.testing.assert_array_equal(np.asarray(result["canvas"])[~outside], np.asarray(self.generated)[~outside])

    def test_context_bounds_endpoints_monotonic_and_all_mask_support(self):
        for box in ((500, 300, 520, 320), (0, 0, 1, 1), (1023, 767, 1024, 768),
                    (0, 250, 800, 700), (0, 0, 1024, 768)):
            mask = Image.new("L", (1024, 768))
            mask.paste(255, box)
            tight = X.patch_bounds(mask, 0)
            self.assertEqual(tight, (max(0, box[0] - max(64, (box[2] - box[0] + 3) // 4)),
                                     max(0, box[1] - max(64, (box[3] - box[1] + 3) // 4)),
                                     min(1024, box[2] + max(64, (box[2] - box[0] + 3) // 4)),
                                     min(768, box[3] + max(64, (box[3] - box[1] + 3) // 4))))
            previous = tight
            for context in range(101):
                bounds = X.patch_bounds(mask, context)
                self.assertTrue(0 <= bounds[0] <= previous[0] <= box[0])
                self.assertTrue(0 <= bounds[1] <= previous[1] <= box[1])
                self.assertTrue(box[2] <= previous[2] <= bounds[2] <= 1024)
                self.assertTrue(box[3] <= previous[3] <= bounds[3] <= 768)
                previous = bounds
            self.assertEqual(previous, (0, 0, 1024, 768))
        mask = Image.new("L", (1000, 800))
        mask.putpixel((300, 100), 1)
        mask.putpixel((650, 600), 255)
        self.assertEqual(X.patch_bounds(mask, 0), (212, 0, 739, 727))
        self.assertEqual(X.patch_bounds(Image.new("L", (17, 9), 255), 0), (0, 0, 17, 9))
        for context in (-1, 101, 2.5, True, "50", None):
            with self.assertRaises(X.ExpandError):
                X.patch_bounds(mask, context)
        with self.assertRaises(X.ExpandError):
            X.patch_bounds(Image.new("L", (20, 20)), 0)

    def test_local_context_upload_roundtrip_and_exact_composite_at_original_coordinates(self):
        picture = Image.fromarray(np.random.default_rng(44).integers(0, 256, (513, 777, 3), dtype=np.uint8))
        for box in ((301, 203, 315, 217), (0, 0, 10, 10), (760, 500, 777, 513)):
            mask = Image.new("L", picture.size)
            mask.paste(255, box)
            mask.putpixel((box[0], box[1]), 20)
            for context in (0, 42, 100):
                for feather in (0, 8):
                    with self.subTest(box=box, context=context, feather=feather):
                        bounds = X.patch_bounds(mask, context)
                        generated = Image.new("RGB", (bounds[2]-bounds[0], bounds[3]-bounds[1]), (23, 45, 67))
                        client = self.client()
                        client.view.return_value = X._png(generated)
                        settings = {"patch_context": context, "fill_feathering": feather, "fill_megapixels": 0.75}
                        with patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
                            result = X.patch_variations(picture, mask, "", X.Job(), settings=settings)[0]
                        for index, expected in ((0, picture.crop(bounds)), (1, mask.crop(bounds))):
                            with Image.open(io.BytesIO(client.upload.call_args_list[index].args[0])) as uploaded:
                                np.testing.assert_array_equal(uploaded, expected)
                        graph = client.queue.call_args.args[0]
                        self.assertEqual(graph["3"]["inputs"]["megapixels"], 0.75)
                        self.assertEqual(graph["3"]["inputs"]["resolution_steps"], 16)
                        self.assertEqual(graph["25"]["inputs"]["width"], ["24", 0])
                        self.assertEqual(graph["24"]["inputs"]["image"], ["1", 0])
                        expected = Image.composite(Image.new("RGB", picture.size, (23, 45, 67)), picture,
                                                   X.patch_alpha(mask, feather))
                        np.testing.assert_array_equal(result["canvas"], expected)
                        self.assertEqual(result["canvas"].size, picture.size)
                        self.assertEqual(result["layer"].size, picture.size)
                        np.testing.assert_array_equal(np.asarray(result["layer"])[..., 3], X.patch_alpha(mask, feather))
                        recomposite = Image.alpha_composite(picture.convert("RGBA"), result["layer"]).convert("RGB")
                        np.testing.assert_array_equal(recomposite, expected)

    def test_disconnected_strokes_context_preserves_intervening_pixels_and_partial_cancel(self):
        picture = Image.new("RGB", (900, 600), (2, 4, 6))
        mask = Image.new("L", picture.size)
        mask.paste(255, (300, 200, 320, 220))
        mask.paste(255, (450, 350, 475, 375))
        bounds = X.patch_bounds(mask, 0)
        client, job = self.client(), X.Job()
        client.view.return_value = X._png(Image.new("RGB", (bounds[2]-bounds[0], bounds[3]-bounds[1]), (100, 120, 140)))
        def finish(index, result):
            job.cancel = True
        with patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
            with self.assertRaises(X.Cancelled):
                X.patch_variations(picture, mask, "", job, settings={"patch_context": 0, "fill_feathering": 0},
                                   variations=2, on_result=finish)
        self.assertEqual(client.queue.call_count, 1)
        self.assertEqual(job.result["canvas"].getpixel((310, 210)), (100, 120, 140))
        self.assertEqual(job.result["canvas"].getpixel((460, 360)), (100, 120, 140))
        self.assertEqual(job.result["canvas"].getpixel((400, 300)), picture.getpixel((400, 300)))

    def test_wrong_size_failure_no_result_and_cancel_keeps_completed_variation(self):
        client, job = self.client(), X.Job()
        client.view.return_value = X._png(Image.new("RGB", (20, 20)))
        with patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
            with self.assertRaisesRegex(X.ExpandError, "dimensions"):
                X.patch_variations(self.source, self.mask, "", job)
        self.assertEqual(job.results, [])
        client, job = self.client(), X.Job()

        def finish(index, result):
            job.cancel = True

        with patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
            with self.assertRaises(X.Cancelled):
                X.patch_variations(self.source, self.mask, "", job, variations=2, on_result=finish)
        self.assertEqual(len(job.results), 1)
        self.assertEqual(client.queue.call_count, 1)
        client, job = self.client(), X.Job()
        client.wait.side_effect = X.Cancelled()
        with patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
            with self.assertRaises(X.Cancelled):
                X.patch_variations(self.source, self.mask, "", job)
        self.assertIsNone(job.result)

    def test_api_validation_atomic_job_preview_and_local_return(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            self.source.save(source)
            info = root.stat()
            state = {"img": self.source, "name": source.name, "seq": 7, "thread": None, "job": X.Job(),
                     "imgs": {"xresult": b"old"}, "path": str(source), "source_kind": "local",
                     "source_directory": (info.st_dev, info.st_ino), "doc": None}
            with patch.object(S, "STATE", state), patch.object(S, "SETTINGS_FILE", root / "settings.json"):
                old_job = state["job"]
                for request in ({"mode": "bad"}, {"mode": "patch", "seq": 6, "mask": mask_data(self.mask)},
                                {"mode": "patch", "seq": 7, "mask": mask_data(Image.new("L", self.source.size))}):
                    with self.assertRaises((ValueError, X.ExpandError)):
                        S.api_run(request)
                    self.assertIs(state["job"], old_job)
                    self.assertEqual(state["imgs"]["xresult"], b"old")
                client = self.client()
                with patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
                    response = S.api_run({"mode": "patch", "seq": 7, "mask": mask_data(self.mask), "prompt": "flowers"})
                    state["thread"].join(5)
                self.assertFalse(state["thread"].is_alive())
                self.assertEqual(S.api_job({})["state"], "done")
                self.assertEqual(response["size"], self.source.size)
                self.assertIn("xvar0", state["imgs"])
                sent = S.api_send_result({"seq": 7})
                self.assertTrue(sent["saved"])
                with Image.open(sent["path"]) as result:
                    np.testing.assert_array_equal(result, state["job"].result["canvas"])
                bridge = Mock()
                state.update(doc={"index": 1}, source_kind="photocraft")
                bridge.status.return_value = {"connected": True}
                bridge.send_patch.return_value = {"document": "source"}
                with patch.object(S, "bridge", return_value=bridge):
                    self.assertTrue(S.api_send_result({"seq": 7})["sent"])
                bridge.send_patch.assert_called_once()
                bridge.send_expand.assert_not_called()

    def test_patch_status_uses_fill_without_overwriting_expansion_method(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(S, "SETTINGS_FILE", Path(temp) / "settings.json"), \
                patch.object(X, "status", return_value={"ok": True}) as status:
            result = S.api_status({"mode": "patch"})
            self.assertEqual(S.api_settings({})["method"], "flux2")
        self.assertEqual(status.call_args.args[0]["method"], "flux1_fill")
        self.assertEqual(result["settings"]["method"], "flux1_fill")

    def test_patch_api_cancel_and_failure_follow_existing_job_states(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                state = {"img": self.source, "name": "source", "seq": 7, "thread": None,
                         "job": X.Job(), "imgs": {}}
                waiting, release = threading.Event(), threading.Event()

                def wait(prompt_id, client_id, graph, job):
                    waiting.set()
                    if not release.wait(5):
                        raise AssertionError("test release timed out")
                    if fail:
                        raise X.ExpandError("synthetic Comfy failure")
                    if job.cancel:
                        raise X.Cancelled()
                    return {"out": {"images": [{"filename": "result.png"}]}}

                client = self.client()
                client.wait.side_effect = wait
                with patch.object(S, "STATE", state), patch.object(S, "SETTINGS_FILE", root / "settings.json"), \
                        patch.object(X, "status", return_value={"ok": True}), patch.object(X, "Comfy", return_value=client):
                    S.api_run({"mode": "patch", "seq": 7, "mask": mask_data(self.mask)})
                    try:
                        self.assertTrue(waiting.wait(5))
                        self.assertTrue(S.running())
                        with self.assertRaisesRegex(ValueError, "already running"):
                            S.api_run({"mode": "patch", "seq": 7, "mask": mask_data(self.mask)})
                        if not fail:
                            S.api_cancel({})
                    finally:
                        release.set()
                        state["thread"].join(5)
                    self.assertFalse(S.running())
                    self.assertEqual(S.api_job({})["state"], "error" if fail else "cancelled")
                    self.assertIsNone(state["job"].result)
                    self.assertNotIn("xvar0", state["imgs"])

    def test_patch_photocraft_layer_does_not_resize_canvas(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge = Bridge(port=1, token_file=root / "unused", exchange=root / "exchange", read_root=root)
            bridge.client = FakePhotoCraft(bridge)
            bridge.send_patch(np.zeros((48, 64, 4), np.uint8), bridge.client.documents[0])
            self.assertFalse(any(command == "image.crop" for command, _ in bridge.client.calls))
            self.assertTrue(any(command == "layer.copyToDocument" for command, _ in bridge.client.calls))


class PatchPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.crop = self.root / "settings.json"
        self.presets = self.root / "presets.json"
        for name, value in (("SETTINGS_FILE", self.crop), ("PRESETS_FILE", self.presets)):
            guard = patch.object(S, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.request = {"mode": "patch"}

    def test_initial_snapshot_preserves_crop_and_migrates_existing_presets_once(self):
        original = X.fill_settings(self.crop, {"fill_steps": 39, "fill_guidance": 41,
                                               "steps": 13, "prompt": "original"})
        self.presets.write_text(json.dumps({"presets": {"existing": original, "legacy": {"steps": 12}},
                                           "last": "existing"}))
        before = (self.crop.read_bytes(), self.presets.read_bytes())
        initial = S.api_settings(self.request)
        copied = S.api_presets(self.request)
        self.assertEqual(initial["fill_steps"], 39)
        self.assertEqual(initial["prompt"], "original")
        self.assertEqual(initial["method"], "flux1_fill")
        self.assertEqual(copied["last"], "existing")
        self.assertEqual(copied["presets"]["existing"]["fill_steps"], 39)
        self.assertEqual(copied["presets"]["legacy"]["fill_steps"], X.DEFAULTS["fill_steps"])
        self.assertNotIn("fill_clip_device", copied["presets"]["existing"])
        self.assertEqual((self.crop.read_bytes(), self.presets.read_bytes()), before)
        S.api_settings({"values": {"fill_steps": 17}})
        S.api_preset_delete({"name": "existing"})
        self.assertEqual(S.api_settings(self.request)["fill_steps"], 39)
        self.assertIn("existing", S.api_presets(self.request)["presets"])
        S.api_settings({**self.request, "values": {"fill_steps": 29, "prompt": "patch"}})
        self.assertEqual(S.api_settings({})["fill_steps"], 17)
        self.assertEqual(S.api_settings({})["prompt"], "original")

    def test_first_crop_edit_does_not_change_initial_patch_values(self):
        X.fill_settings(self.crop, {"fill_steps": 35})
        S.api_settings({"values": {"fill_steps": 12}})
        self.assertEqual(S.api_settings(self.request)["fill_steps"], 35)
        self.assertEqual(S.api_settings({})["fill_steps"], 12)

    def test_independent_same_name_presets_load_reset_save_delete_and_restart(self):
        for mode, steps, prompt in (("expand", 19, "extend"), ("patch", 37, "replace")):
            S.api_settings({"mode": mode, "values": {"fill_steps": steps, "prompt": prompt}})
            S.api_preset_save({"mode": mode, "name": "my preset"})
        for mode, steps, prompt in (("expand", 19, "extend"), ("patch", 37, "replace")):
            S.api_preset_load({"mode": mode, "name": None})
            self.assertEqual(S.api_settings({"mode": mode})["fill_steps"], X.DEFAULTS["fill_steps"])
            S.api_preset_load({"mode": mode, "name": "my preset"})
            loaded = S.api_settings({"mode": mode})
            self.assertEqual((loaded["fill_steps"], loaded["prompt"]), (steps, prompt))
        # All state is read from disk, including the independent selected preset.
        self.assertEqual(S.api_presets(self.request)["last"], "my preset")
        S.api_preset_delete({**self.request, "name": "my preset"})
        self.assertIsNone(S.api_presets(self.request)["last"])
        self.assertIn("my preset", S.api_presets({})["presets"])
        self.assertEqual(S.api_settings({})["fill_steps"], 19)

    def test_shared_encoding_resource_is_not_reset_or_restored_by_presets(self):
        S.api_settings({"values": {"fill_clip_device": "default"}})
        S.api_preset_save({**self.request, "name": "patch"})
        S.api_preset_save({"name": "crop"})
        S.api_settings({**self.request, "values": {"fill_clip_device": "cpu"}})
        for mode, name in (("expand", "crop"), ("patch", "patch")):
            S.api_preset_load({"mode": mode, "name": name})
            self.assertEqual(S.api_settings({"mode": mode})["fill_clip_device"], "cpu")
            S.api_settings({"mode": mode, "reset": True})
            self.assertEqual(S.api_settings({"mode": mode})["fill_clip_device"], "cpu")
        with patch.object(X, "status", return_value={"ok": True}):
            self.assertEqual(S.api_status({})["comfy"], S.api_status(self.request)["comfy"])

    def test_generation_reads_only_the_requested_processing_settings_and_prompt(self):
        S.api_settings({"values": {"fill_steps": 17, "prompt": "expand prompt"}})
        S.api_settings({**self.request, "values": {"fill_steps": 39, "prompt": "patch prompt"}})
        picture = Image.new("RGB", (128, 96))
        state = {"img": picture, "seq": 7, "name": "synthetic", "thread": None, "job": X.Job(), "imgs": {}}
        with patch.object(S, "STATE", state), patch.object(X, "patch_variations") as generate_patch, \
                patch.object(X, "expand_variations") as generate_expand:
            S.api_run({"mode": "patch", "seq": 7, "mask": mask_data(Image.new("L", picture.size, 255))})
            state["thread"].join(5)
            self.assertEqual(generate_patch.call_args.args[2], "patch prompt")
            self.assertEqual(generate_patch.call_args.args[5]["fill_steps"], 39)
            generate_expand.assert_not_called()
            S.api_run({"ratio": None, "x": -5, "y": -5, "w": 138, "h": 106})
            state["thread"].join(5)
            self.assertEqual(generate_expand.call_args.args[2], "expand prompt")
            self.assertEqual(generate_expand.call_args.args[5]["fill_steps"], 17)

    def test_invalid_mode_settings_do_not_change_either_existing_file(self):
        S.api_settings(self.request)
        X.fill_settings(self.crop, {"fill_steps": 40})
        before = (self.crop.read_bytes(), (self.root / "patch-settings.json").read_bytes())
        for req in ({"mode": "wrong"}, {**self.request, "values": {"steps": 20}},
                    {**self.request, "values": {"fill_steps": 0}},
                    {**self.request, "values": {"method": "flux2"}}):
            with self.assertRaises((ValueError, X.ExpandError)):
                S.api_settings(req)
            self.assertEqual((self.crop.read_bytes(), (self.root / "patch-settings.json").read_bytes()), before)

    def test_context_defaults_legacy_settings_presets_and_crop_isolation(self):
        # A pre-slider Patch settings file and preset adopt the whole-image endpoint.
        X.fill_settings(self.root / "patch-settings.json", {"method": "flux1_fill", "fill_steps": 34})
        (self.root / "patch-presets.json").write_text(json.dumps({
            "presets": {"old": {"fill_steps": 34}}, "last": "old"}))
        self.assertEqual(S.api_settings(self.request)["patch_context"], 100)
        S.api_settings({**self.request, "values": {"patch_context": 15}})
        S.api_preset_save({**self.request, "name": "local"})
        self.assertEqual(json.loads((self.root / "patch-settings.json").read_text())["patch_context"], 15)
        S.api_preset_load({**self.request, "name": "old"})
        self.assertEqual(S.api_settings(self.request)["patch_context"], 100)
        S.api_preset_load({**self.request, "name": "local"})
        self.assertEqual(S.api_settings(self.request)["patch_context"], 15)
        S.api_settings({**self.request, "reset": True})
        self.assertEqual(S.api_settings(self.request)["patch_context"], 100)
        S.api_preset_save({"name": "crop"})
        self.assertNotIn("patch_context", S.api_settings({}))
        self.assertNotIn("patch_context", json.loads(self.crop.read_text()) if self.crop.exists() else {})
        self.assertNotIn("patch_context", S.api_presets({})["presets"]["crop"])
        for context in (-1, 101, "bad"):
            before = (self.root / "patch-settings.json").read_bytes()
            with self.assertRaises(X.ExpandError):
                S.api_settings({**self.request, "values": {"patch_context": context}})
            self.assertEqual((self.root / "patch-settings.json").read_bytes(), before)
        with self.assertRaises(X.ExpandError):
            S.api_settings({"values": {"patch_context": 50}})
