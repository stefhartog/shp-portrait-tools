"""Disposable watch-folder imports: no live PhotoCraft, models, or user's inbox."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2

import test_dnb as T
from shp_photocraft_tools.modules.dnb import ui_server as U
from shp_photocraft_tools.shared.watch_folder import WatchFolder
from shp_photocraft_tools.modules.crop import server as C


class WatchFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "watchfolder"
        self.inbox.mkdir()
        self.now = 0
        self.watch = WatchFolder(self.inbox, self.root / "retained", clock=lambda: self.now)
        self.addCleanup(self.watch.close)
        self.png = cv2.imencode(".png", U.u8(T.synthetic_image()))[1].tobytes()

    def ready(self):
        self.assertFalse(self.watch.status()["ready"])
        self.now += 3
        status = self.watch.status()
        self.assertTrue(status["ready"])
        return status["token"]

    def test_completed_png_load_cleanup_and_duplicate_tokens(self):
        source = self.inbox / "image.PNG"
        source.write_bytes(self.png)
        calls = []

        def load(snapshot):
            self.assertTrue(source.exists())  # Never deleted before confirmed load.
            self.assertEqual(snapshot.read_bytes(), self.png)
            self.assertNotEqual(snapshot.parent, self.inbox)
            calls.append(snapshot)
            return {"generation": 1}

        token = self.ready()
        result = self.watch.load(token, load)
        self.assertTrue(result["watch_cleanup"])
        self.assertFalse(source.exists())
        self.assertTrue(calls[0].is_file())
        self.assertEqual(self.watch.load(token, load), {"skipped": True})
        self.assertEqual(len(calls), 1)

    def test_partial_corrupt_and_failed_loads_retained_until_changed(self):
        source = self.inbox / "partial.png"
        source.write_bytes(self.png[:-12])
        self.assertFalse(self.watch.status()["ready"])
        self.now += 3
        self.assertFalse(self.watch.status()["ready"])
        self.assertTrue(source.exists())
        self.assertFalse(self.watch.status()["ready"])
        self.assertIn("incomplete", self.watch.status()["errors"][0])
        source.write_bytes(self.png)
        token = self.ready()
        with self.assertRaisesRegex(ValueError, "analysis failed"):
            self.watch.load(token, Mock(side_effect=ValueError("analysis failed")))
        self.assertTrue(source.exists())
        self.assertTrue(self.watch.status()["ready"])
        self.assertEqual(list((self.root / "retained").iterdir()), [])

    def test_changing_export_restarts_stability_wait(self):
        source = self.inbox / "image.png"
        source.write_bytes(self.png[:40])
        self.assertFalse(self.watch.status()["ready"])
        self.now += 1
        source.write_bytes(self.png)
        self.assertFalse(self.watch.status()["ready"])
        self.now += 1.5
        self.assertFalse(self.watch.status()["ready"])
        self.now += 1
        token = self.watch.status()["token"]
        source.write_bytes(self.png + b"new export")
        loader = Mock()
        self.assertEqual(self.watch.load(token, loader), {"skipped": True})
        loader.assert_not_called()
        self.assertTrue(source.exists())

    def test_replacement_during_load_is_not_deleted(self):
        source = self.inbox / "image.png"
        source.write_bytes(self.png)
        token = self.ready()

        def load(snapshot):
            source.unlink()
            source.write_bytes(b"new export")
            return {"generation": 1}

        result = self.watch.load(token, load)
        self.assertFalse(result["watch_cleanup"])
        self.assertIn("retained", result["watch_error"])
        self.assertEqual(source.read_bytes(), b"new export")

    def test_cleanup_failure_is_reported_without_loading_same_version_twice(self):
        source = self.inbox / "image.png"
        source.write_bytes(self.png)
        token = self.ready()
        loader = Mock(return_value={"generation":1})
        with patch("shp_photocraft_tools.shared.watch_folder.os.unlink",
                   side_effect=PermissionError("read-only inbox")):
            result = self.watch.load(token, loader)
        self.assertFalse(result["watch_cleanup"])
        self.assertIn("read-only inbox", result["watch_error"])
        self.assertTrue(source.exists())
        self.assertEqual(self.watch.load(token, loader), {"skipped":True})
        loader.assert_called_once()

    def test_folder_replaced_during_load_keeps_original_and_replacement_files(self):
        source = self.inbox / "image.png"
        source.write_bytes(self.png)
        token = self.ready()
        moved = self.root / "moved"

        def load(snapshot):
            self.inbox.rename(moved)
            self.inbox.mkdir()
            source.write_bytes(b"unrelated replacement")
            return {"generation":1}

        result = self.watch.load(token, load)
        self.assertFalse(result["watch_cleanup"])
        self.assertEqual((moved / "image.png").read_bytes(), self.png)
        self.assertEqual(source.read_bytes(), b"unrelated replacement")

    def test_closed_watcher_never_loads_or_removes_pending_input(self):
        source = self.inbox / "image.png"
        source.write_bytes(self.png)
        token = self.ready()
        self.watch.close()
        loader = Mock()
        with self.assertRaisesRegex(ValueError, "stopping"):
            self.watch.load(token, loader)
        loader.assert_not_called()
        self.assertTrue(source.exists())

    def test_only_direct_regular_pngs_and_pinned_directory(self):
        other = self.root / "other.png"
        other.write_bytes(self.png)
        (self.inbox / "linked.png").symlink_to(other)
        nested = self.inbox / "nested"
        nested.mkdir()
        (nested / "nested.png").write_bytes(self.png)
        (self.inbox / "not.png").mkdir()
        (self.inbox / "other.jpg").write_bytes(self.png)
        result = self.watch.status()
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["errors"]), 2)
        self.now += 3
        self.assertFalse(self.watch.status()["ready"])
        self.assertEqual(other.read_bytes(), self.png)
        self.inbox.rename(self.root / "moved")
        self.inbox.mkdir()
        self.assertIn("changed", self.watch.status()["error"])
        self.watch.close()
        self.assertEqual(self.watch.status(), {"ready": False})


class CropWatchTests(unittest.TestCase):
    def test_crop_inbox_separation_target_failure_retry_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skin, crop = root / "watchfolder", root / "watchfolder_crop"
            skin.mkdir(); crop.mkdir()
            pixels = cv2.imencode(".png", U.u8(T.synthetic_image()))[1].tobytes()
            (skin / "skin.png").write_bytes(pixels)
            source = crop / "crop.png"
            source.write_bytes(pixels)
            now = [0]
            watcher = WatchFolder(crop, root / "retained", clock=lambda:now[0])
            self.addCleanup(watcher.close)
            watcher.status(); now[0] = 3
            token = watcher.status()["token"]
            handler = object.__new__(C.Handler)
            handler.server = SimpleNamespace(watch_folder=watcher)
            state = {"img":None,"seq":0,"job":C.X.Job(),"thread":None,"imgs":{}}
            target = {"document":{"name":"Crop.psd","index":1}, "identity":{
                "id":"crop-doc","revision":1,"width":999,"height":96}}
            with patch.object(C,"STATE",state), patch.object(C,"capture_target",return_value=target):
                with self.assertRaisesRegex(ValueError,"export size"):
                    handler.load_watch_image({"token":token})
                self.assertIsNone(state["img"])
                self.assertTrue(source.exists())
                target["identity"]["width"] = 128
                loaded = handler.load_watch_image({"token":token})
                self.assertTrue(loaded["watch_cleanup"])
                self.assertEqual(loaded["source_kind"],"photocraft")
                self.assertEqual(state["doc"],target["document"])
                self.assertEqual(loaded["width"],128)
                self.assertTrue(Path(state["path"]).is_file())
                self.assertFalse(source.exists())
                self.assertTrue((skin / "skin.png").exists())
                self.assertEqual(handler.load_watch_image({"token":token}),{"skipped":True})
            self.assertNotEqual(C.WATCH_DIRECTORY,U.WATCH_DIRECTORY)


class WatchApiTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    get = T.DnbTests.get
    post = T.DnbTests.post

    def request(self, route, body):
        request = Request(self.base + route, json.dumps(body).encode(),
                          {"Content-Type":"application/json", "Origin":self.base})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def prepare(self):
        inbox = self.root / "watchfolder"
        inbox.mkdir()
        source = inbox / "photo.png"
        source.write_bytes(self.image.read_bytes())
        now = [0]
        self.server.watch_folder = WatchFolder(inbox, self.root / "retained", clock=lambda: now[0])
        self.addCleanup(self.server.watch_folder.close)
        self.get("/api/watch-folder")
        now[0] = 3
        ready = json.loads(self.get("/api/watch-folder")[0])
        return source, ready

    def test_same_origin_load_uses_normal_analysis_and_cleans_only_after_success(self):
        source, ready = self.prepare()
        target = {"document":{"index":2, "name":"Source.psd"}, "identity":{
            "id":"captured-doc", "revision":7, "width":128, "height":96}}
        with self.assertRaises(HTTPError) as error:
            self.post("/api/watch-folder/load", {"token":ready["token"]})
        self.assertEqual(error.exception.code, 403)
        self.assertTrue(source.is_file())
        with patch.object(U, "watch_target", return_value=target):
            loaded = self.request("/api/watch-folder/load", {"token":ready["token"]})
            self.assertEqual(self.request("/api/watch-folder/load", {"token":ready["token"]}), {"skipped":True})
        self.assertEqual(loaded["width"], 128)
        self.assertEqual(loaded["height"], 96)
        self.assertEqual(loaded["generation"], U.STATE["landmark_generation"])
        self.assertTrue(loaded["watch_cleanup"])
        self.assertFalse(source.exists())
        self.assertTrue(Path(U.STATE["path"]).is_file())
        self.assertEqual(U.STATE["source_kind"], "photocraft")
        self.assertEqual(U.BRIDGE["doc"], target["document"])
        self.assertEqual(loaded["pc_doc"], target["document"])

    def test_changed_target_or_size_retains_source_and_previous_editor(self):
        source, ready = self.prepare()
        self.post("/api/load", {"path":str(self.image)})
        previous = U.STATE.copy()
        target = {"document":{"index":2, "name":"Source.psd"}, "identity":{
            "id":"captured-doc", "revision":7, "width":128, "height":96}}
        changed = {**target, "identity":{**target["identity"], "revision":8}}
        with patch.object(U, "watch_target", side_effect=[target, target, changed]):
            with self.assertRaises(HTTPError) as error:
                self.request("/api/watch-folder/load", {
                    "token":ready["token"]})
            self.assertIn("changed during image analysis", error.exception.read().decode())
        self.assertTrue(source.exists())
        self.assertTrue(all(U.STATE[key] is value for key, value in previous.items()))
        self.assertEqual(list((self.root / "retained").iterdir()), [])
        self.assertTrue(json.loads(self.get("/api/watch-folder")[0])["ready"])
        with patch.object(U, "watch_target", return_value=target), patch.object(U, "load_watch_image") as load:
            self.assertEqual(self.request("/api/watch-folder/load", {"token":ready["token"]}), {"skipped":True})
            load.assert_not_called()
        with patch.object(U, "watch_target", return_value={**target, "identity":{
                **target["identity"], "width":999}}):
            with self.assertRaisesRegex(ValueError, "export size"):
                U.load_watch_image(self.image, U.watch_target())
        self.assertTrue(all(U.STATE[key] is value for key, value in previous.items()))
        with patch.object(U, "watch_target", return_value=changed):
            loaded = self.request("/api/watch-folder/load", {"token":ready["token"]})
        self.assertTrue(loaded["watch_cleanup"])
        self.assertFalse(source.exists())

    def test_missing_active_target_retains_file_then_retries_on_connection(self):
        source, ready = self.prepare()
        with patch.object(U, "watch_target", side_effect=ValueError("no active document")):
            with self.assertRaises(HTTPError):
                self.request("/api/watch-folder/load", {"token":ready["token"]})
        self.assertTrue(source.exists())
        target = {"document":{"index":2,"name":"Source.psd"}, "identity":{
            "id":"doc-id","revision":1,"width":999,"height":96}}
        with patch.object(U, "watch_target", return_value=target):
            with self.assertRaises(HTTPError):
                self.request("/api/watch-folder/load", {"token":ready["token"]})
        self.assertTrue(source.exists())
        target["identity"]["width"] = 128
        with patch.object(U, "watch_target", return_value=target):
            self.assertTrue(self.request("/api/watch-folder/load", {"token":ready["token"]})["watch_cleanup"])
        self.assertFalse(source.exists())

    def test_target_capture_requires_stable_document_identity_and_revision(self):
        doc = {"index":2,"name":"Source.psd"}
        identity = {"id":"source-id","revision":7,"width":128,"height":96,"name":doc["name"]}
        pc = SimpleNamespace(active_doc=Mock(return_value=doc),
                             client=SimpleNamespace(run=Mock(return_value=identity)))
        with patch.object(U, "bridge", return_value=pc):
            self.assertEqual(U.watch_target(), {"document":doc, "identity":{
                key:identity[key] for key in ("id","revision","width","height")}})
            pc.client.run.return_value = {key:value for key,value in identity.items() if key != "id"}
            self.assertEqual(U.watch_target()["identity"]["id"], {"index":2,"name":"Source.psd","path":None})
            pc.client.run.return_value = {**identity, "name":"Different.psd"}
            with self.assertRaisesRegex(ValueError, "changed while inspecting"):
                U.watch_target()
            pc.client.run.return_value = identity
            pc.active_doc.side_effect = [doc, {**doc,"index":3}]
            with self.assertRaisesRegex(ValueError, "changed"):
                U.watch_target()
            pc.active_doc.side_effect = None
            pc.client.run.return_value = {"width":128,"height":96}
            with self.assertRaisesRegex(ValueError, "identity/revision"):
                U.watch_target()


if __name__ == "__main__":
    unittest.main()
