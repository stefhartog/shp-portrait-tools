"""Local Combine batch analysis, output safety and mixed-outcome regressions."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np
from PIL import Image

import test_combine as T
from test_dnb import synthetic_image
from shp_photocraft_tools.modules.dnb import ui_server as U, progress
from shp_photocraft_tools.shared.local_output import batch_output


class BatchApiTests(unittest.TestCase):
    setUp = T.CombineApiTests.setUp
    close_server = T.CombineApiTests.close_server
    get = T.CombineApiTests.get
    post = T.CombineApiTests.post
    request = T.CombineApiTests.request

    def target(self, path, *args, **kwargs):
        if path.name == "no-scale.png":
            return None
        return {"S": 362 if path.suffix == ".tif" else 90.5, "conf": "high", "desc": "synthetic scale",
                "centre": (64, 48), "face_info": None, "seen": [],
                "kp": np.full((17, 2), 10 if path.suffix == ".tif" else 20)}

    def components(self, raw, *args):
        ones = np.ones(raw.shape[:2], np.float32)
        return {"A": ones, "features": ones * 0, "colour": ones, "skin": ones}

    def test_same_stem_output_collision_is_a_manifest_failure_and_batch_continues(self):
        second = self.root / "synthetic.tif"
        cv2.imwrite(str(second), U.FS.rgb16(synthetic_image()))
        third = self.root / "other.png"
        third.write_bytes(self.image.read_bytes())
        with patch.object(U.M, "pick_target", side_effect=self.target), \
             patch.object(U.M, "person_box", return_value=(0, 0, 128, 96)), \
             patch.object(U.K, "skin_mask", side_effect=self.components):
            result = self.post("/api/batch", {"paths":[str(self.image), str(second), str(third)],
                                               "settings":self.request()})
        self.assertEqual((result["succeeded"], result["failed"]), (2, 1))
        self.assertEqual(result["items"][0]["output"], "synthetic_001.png")
        self.assertEqual(result["items"][1]["status"], "failed")
        self.assertIn("FileExistsError", result["items"][1]["error"])
        self.assertNotIn("output", result["items"][1])
        self.assertEqual(result["items"][2]["output"], "other_001.png")
        self.assertEqual(json.loads(Path(result["preset"]).read_text())["batch"]["items"], result["items"])
        self.assertEqual(sorted(p.name for p in Path(result["dir"]).glob("*.png")),
                         ["other_001.png", "synthetic_001.png"])

    def test_folder_direct_children_and_same_folder_selection(self):
        nested = self.root / "nested"
        nested.mkdir()
        cv2.imwrite(str(nested / "nested.png"), U.u8(synthetic_image()))
        (self.root / "folder.jpg").mkdir()
        (self.root / "not-image.txt").write_text("ignored")
        selected = self.post("/api/batch/images", {"folder": str(self.root)})
        self.assertEqual(selected["items"], [{"name": self.image.name, "path": str(self.image)}])
        for body in ({"paths": [str(self.image), str(nested / "nested.png")]},
                     {"paths": [str(self.image), str(self.image)]},
                     {"folder": str(self.root), "paths": [str(self.image)]},
                     {"paths": []}, {"paths": [str(self.root / "not-image.txt")]},
                     {"folder": str(self.root / "folder.jpg")}):
            with self.assertRaises(HTTPError):
                self.post("/api/batch/images", body)
        self.assertEqual(list(self.root.glob("shpcombine_batch_*")), [])

    def test_mixed_failures_per_image_scale_settings_pixels_and_editor_preservation(self):
        second = self.root / "synthetic_second.tif"
        cv2.imwrite(str(second), U.FS.rgb16(synthetic_image()))
        (self.root / "broken.png").write_bytes(b"not an image")
        cv2.imwrite(str(self.root / "no-scale.png"), U.u8(synthetic_image()))
        self.post("/api/load", {"path": str(self.image)})
        editor = U.STATE.copy()
        U.BRIDGE["doc"] = {"name": "keep loaded document identity"}
        bridge_doc = U.BRIDGE["doc"]
        source_bytes = {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        req = self.request()
        req["analysis"] = {"skin": True, "detect": False, "structure": True}
        req["frequency"].update(radius_at_s181=6, blur_percent=200)
        expected = []
        real_combine = U.combined_result

        def capture(source, mask, params, **kwargs):
            result = real_combine(source, mask, params, **kwargs)
            expected.append((source.copy(), params, kwargs["state"], result[2].copy()))
            return result

        with patch.object(U.M, "pick_target", side_effect=self.target) as analyze, \
             patch.object(U.M, "person_box", return_value=(0, 0, 128, 96)), \
             patch.object(U.K, "skin_mask", side_effect=self.components), \
             patch.object(U, "combined_result", side_effect=capture):
            result = self.post("/api/batch", {"folder": str(self.root), "settings": req,
                                              "_progress": "batch-mixed-results-1234"})
        self.assertEqual((result["succeeded"], result["failed"]), (2, 2))
        self.assertEqual(analyze.call_count, 3)
        self.assertEqual([row["status"] for row in result["items"]], ["failed", "failed", "success", "success"])
        self.assertIn("cannot read", result["items"][0]["error"])
        self.assertIn("detected scale", result["items"][1]["error"])
        self.assertEqual([entry[1]["S"] for entry in expected], [90.5, 362])
        self.assertEqual([entry[1]["frequency"]["radius"] for entry in expected], [3, 12])
        self.assertEqual([entry[2]["pose"]["kp"][0, 0] for entry in expected], [20, 10])
        saved = json.loads(Path(result["preset"]).read_text())
        self.assertEqual(saved["combine"], req["combine"])
        self.assertEqual(saved["analysis"], req["analysis"])
        self.assertNotIn("S", saved)
        self.assertEqual(saved["batch"]["items"], result["items"])
        self.assertEqual(list(Path(result["dir"]).glob("*.json")), [Path(result["preset"])])
        for item, (_, params, state, pixels) in zip(result["items"][2:], expected):
            image = Path(result["dir"]) / item["output"]
            np.testing.assert_array_equal(cv2.imread(str(image), -1)[..., :3], U.FS.rgb16(pixels))
            with Image.open(image) as encoded:
                self.assertEqual(encoded.info["icc_profile"], state["source_icc"] or U.SRGB_ICC)
            self.assertEqual(item["effective_frequency"], params["frequency"])
            self.assertEqual(item["source_analysis"]["S"], params["S"])
            self.assertTrue(image.is_relative_to(self.root / "shpcombine_batch_001"))
        for key, value in editor.items():
            self.assertIs(U.STATE[key], value)
        self.assertIs(U.BRIDGE["doc"], bridge_doc)
        for path, data in source_bytes.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(result["_progress"]["batch"]["items"], result["items"])

    def test_settings_captured_and_progress_snapshots_are_independent(self):
        other = self.root / "other.png"
        other.write_bytes(self.image.read_bytes())
        settings = self.request()
        snapshots = []
        real_combine = U.combined_result
        transfer = {"format":"tiff", "compressed":False, "opaque_alpha":False, "parallel":False}
        U.api_global_settings({"layer_transfer": transfer})

        def capture(*args, **kwargs):
            snapshots.append(progress.snapshot("batch-snapshot-12345"))
            settings["combine"]["fs_mix"] = 0
            U.api_global_settings({"layer_transfer": {"format":"png"}})
            self.assertEqual(args[2]["combine"]["fs_mix"], 55)
            return real_combine(*args, **kwargs)

        with U.LOCK, progress.operation("batch-snapshot-12345"), \
             patch.object(U.M, "pick_target", side_effect=self.target), \
             patch.object(U.M, "person_box", return_value=(0, 0, 128, 96)), \
             patch.object(U.K, "skin_mask", side_effect=self.components), \
             patch.object(U, "combined_result", side_effect=capture):
            result = U.api_batch({"paths": [str(self.image), str(other)], "settings": settings})
        self.assertEqual(result["succeeded"], 2)
        manifest = json.loads(Path(result["preset"]).read_text())
        self.assertNotIn("frequency_transfer", manifest)
        self.assertEqual(manifest["batch"]["output_settings"], transfer)
        for item in result["items"]:
            output = Path(result["dir"]) / item["output"]
            self.assertEqual(output.suffix, ".tiff")
            self.assertEqual(cv2.imread(str(output), -1).shape[2], 3)
            with Image.open(output) as encoded:
                self.assertEqual(encoded.tag_v2[259], 1)
                self.assertEqual(encoded.info["icc_profile"], U.SRGB_ICC)
        self.assertEqual([item["status"] for item in snapshots[0]["batch"]["items"]], ["running", "pending"])
        self.assertEqual([item["status"] for item in snapshots[1]["batch"]["items"]], ["success", "running"])
        progress.snapshot("batch-snapshot-12345", consume=True)
        for mode in ("crop", "dnb", "frequency"):
            with self.assertRaises(HTTPError):
                self.post("/api/batch", {"folder": str(self.root), "settings": {**self.request(), "mode": mode}})
        request = Request(self.base + "/api/batch", b"{}", {"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_symbolic_input_is_reported_and_no_photocraft_fetch_is_used(self):
        link = self.root / "link.png"
        link.symlink_to(self.image)
        with patch.object(U, "bridge", side_effect=AssertionError("batch must not contact PhotoCraft")):
            result = self.post("/api/batch", {"paths": [str(link)], "settings": self.request()})
        self.assertEqual((result["succeeded"], result["failed"]), (0, 1))
        self.assertIn("symbolic", result["items"][0]["error"])
        self.assertEqual([p.name for p in Path(result["dir"]).iterdir()], ["preset.json"])

    def test_transparency_fails_even_with_zero_fs_mix_and_later_image_succeeds(self):
        transparent = self.root / "transparent.png"
        pixels = np.full((96, 128, 4), 128, np.uint8)
        cv2.imwrite(str(transparent), pixels)
        settings = self.request()
        settings["combine"]["fs_mix"] = 0
        with patch.object(U.M, "pick_target", side_effect=self.target), \
             patch.object(U.M, "person_box", return_value=(0, 0, 128, 96)), \
             patch.object(U.K, "skin_mask", side_effect=self.components):
            result = self.post("/api/batch", {"paths": [str(transparent), str(self.image)], "settings": settings})
        self.assertEqual((result["succeeded"], result["failed"]), (1, 1))
        self.assertIn("opaque", result["items"][0]["error"])
        self.assertEqual(len(list(Path(result["dir"]).iterdir())), 2)

    def test_cancel_route_responds_during_processing_and_preserves_completed_results(self):
        other = self.root / "other.png"
        other.write_bytes(self.image.read_bytes())
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        original = U.combined_result
        calls = 0

        def combine(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("cancel test did not release")
            return original(*args, **kwargs)

        def run():
            try:
                results.append(self.post("/api/batch", {"paths": [str(self.image), str(other)],
                               "settings": self.request(), "_progress": "batch-cancel-test-12345"}))
            except Exception as error:
                errors.append(error)

        with patch.object(U.M, "pick_target", side_effect=self.target), \
             patch.object(U.M, "person_box", return_value=(0, 0, 128, 96)), \
             patch.object(U.K, "skin_mask", side_effect=self.components), \
             patch.object(U, "combined_result", side_effect=combine):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                response = self.post("/api/batch/cancel", {"token": "batch-cancel-test-12345"})
                self.assertTrue(response["cancelling"])
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())
        result = results[0]
        self.assertEqual((result["succeeded"], result["failed"], result["cancelled"]), (1, 0, 1))
        self.assertEqual([item["status"] for item in result["items"]], ["success", "cancelled"])
        self.assertEqual(len(list(Path(result["dir"]).glob("*.png"))), 1)
        self.assertEqual(json.loads(Path(result["preset"]).read_text())["batch"]["items"], result["items"])
        with self.assertRaises(HTTPError):
            self.post("/api/batch/cancel", {"token": "batch-cancel-test-12345"})

    def test_cancel_during_load_skips_analysis_and_marks_all_remaining_images(self):
        other = self.root / "other.png"
        other.write_bytes(self.image.read_bytes())
        original = U.prepare_loaded_image

        def load(*args, **kwargs):
            progress.cancel_batch("batch-cancel-load-12345")
            return original(*args, **kwargs)

        with progress.operation("batch-cancel-load-12345"), U.LOCK, \
             patch.object(U, "prepare_loaded_image", side_effect=load), \
             patch.object(U.M, "pick_target", side_effect=AssertionError("analysis after cancellation")):
            result = U.api_batch({"paths": [str(self.image), str(other)], "settings": self.request()})
        self.assertEqual((result["succeeded"], result["failed"], result["cancelled"]), (0, 0, 2))
        self.assertEqual([item["status"] for item in result["items"]], ["cancelled", "cancelled"])
        self.assertEqual(list(Path(result["dir"]).glob("*.png")), [])
        progress.snapshot("batch-cancel-load-12345", consume=True)


class BatchOutputTests(unittest.TestCase):
    def test_unique_directories_same_stem_and_existing_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "shpcombine_batch_001").symlink_to(root, target_is_directory=True)
            with batch_output(root) as (folder, write, write_preset):
                self.assertEqual(folder.name, "shpcombine_batch_002")
                first = write(1, "same.jpg", b"png1")
                with self.assertRaises(FileExistsError):
                    write(2, "same.png", b"png2")
                second = write(2, "different.png", b"png2")
                self.assertEqual(Path(first).name, "same_002.png")
                self.assertEqual(Path(second).name, "different_002.png")
                third = write(3, "same.tif", b"tiff", extension=".tiff")
                self.assertEqual(Path(third).name, "same_002.tiff")
                self.assertNotEqual(first, second)
                with self.assertRaises(FileExistsError):
                    write(1, "same.jpg", b"overwrite")
                self.assertEqual(Path(first).read_bytes(), b"png1")
                with self.assertRaises(FileExistsError):
                    write(3, "same.tif", b"overwrite", extension=".tiff")
                self.assertEqual(Path(third).read_bytes(), b"tiff")
                preset = Path(write_preset({"combine": {"fs_mix": 55}, "batch": {"items": []}}))
                write_preset({"combine": {"fs_mix": 55}, "batch": {"items": [{"output": Path(first).name}]}})
                self.assertEqual(json.loads(preset.read_text())["batch"]["items"], [{"output": Path(first).name}])
                self.assertFalse((folder / ".preset.json.tmp").exists())
                preset.write_text("external edit")
                with self.assertRaisesRegex(ValueError, "changed externally"):
                    write_preset({})
                self.assertEqual(preset.read_text(), "external edit")
            with batch_output(root) as (next_folder, _, _):
                self.assertEqual(next_folder.name, "shpcombine_batch_003")

    def test_same_stem_collision_is_reported_without_overwriting_or_item_renumbering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with batch_output(root) as (folder, write, _):
                first = Path(write(1, "portrait.jpg", b"first"))
                self.assertEqual(first.name, "portrait_001.png")
                with self.assertRaises(FileExistsError):
                    write(2, "portrait.tif", b"second")
                self.assertEqual(first.read_bytes(), b"first")
                self.assertEqual([path.name for path in folder.iterdir()], ["portrait_001.png"])

    def test_write_failures_and_replaced_folders_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "input"
            root.mkdir()
            with batch_output(root) as (folder, write, write_preset):
                with patch("shp_photocraft_tools.shared.local_output.os.fsync", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        write(1, "image.png", b"partial")
                self.assertEqual(list(folder.iterdir()), [])
                write_preset({"batch": {"items": []}})
                before = (folder / "preset.json").read_bytes()
                with patch("shp_photocraft_tools.shared.local_output.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        write_preset({"batch": {"items": [{"status": "success"}]}})
                self.assertEqual((folder / "preset.json").read_bytes(), before)
                self.assertFalse((folder / ".preset.json.tmp").exists())
                folder.rename(root / "moved-output")
                folder.mkdir()
                with self.assertRaisesRegex(ValueError, "output folder changed"):
                    write(1, "image.png", b"wrong place")
            with batch_output(root) as (_, write, _):
                root.rename(Path(temp) / "moved-input")
                root.mkdir()
                with self.assertRaisesRegex(ValueError, "input folder changed"):
                    write(1, "image.png", b"wrong place")

    def test_scoped_colour_does_not_change_editor_colour(self):
        original = U.D.COLOUR.copy()
        with U.D.colour_mode("adobe"):
            actual = U.D.to_linear(np.array([.5]))
            np.testing.assert_allclose(actual, .5 ** U.D.ADOBE_GAMMA)
            self.assertEqual(U.D.COLOUR, original)
        self.assertEqual(U.D.COLOUR, original)

    def test_preexisting_preset_or_temporary_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "keep.json"
            source.write_text("keep")
            with batch_output(root) as (folder, _, write_preset):
                (folder / "preset.json").symlink_to(source)
                with self.assertRaises(FileExistsError):
                    write_preset({})
                self.assertEqual(source.read_text(), "keep")
            with batch_output(root) as (folder, _, write_preset):
                preset = Path(write_preset({"state": "initial"}))
                temporary = folder / ".preset.json.tmp"
                temporary.write_text("external temporary file")
                with self.assertRaises(FileExistsError):
                    write_preset({"state": "updated"})
                self.assertEqual(json.loads(preset.read_text()), {"state": "initial"})
                self.assertEqual(temporary.read_text(), "external temporary file")


if __name__ == "__main__":
    unittest.main()
