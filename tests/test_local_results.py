"""Local-source exports and native chooser integration without GUI or live services."""

import ast
from concurrent.futures import ThreadPoolExecutor
import io
import itertools
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.dnb import ui_server as S
from shp_photocraft_tools.modules.crop import server as C
from shp_photocraft_tools import supervisor
from shp_photocraft_tools.shared import local_output
import test_dnb as T


class LocalResultTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    post = T.DnbTests.post

    def send(self, body):
        request = Request(self.base + "/api/send-result", json.dumps(body).encode(),
                          {"Content-Type": "application/json", "Origin": self.base})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_one_full_resolution_file_per_mode_with_profile_and_precision(self):
        pixels = np.round(T.synthetic_image() * 65535).astype(np.uint16)
        self.image.write_bytes(S.PCB.tagged_rgb16(pixels, S.SRGB_ICC))
        loaded = self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(loaded["source_kind"], "local")
        for mode, suffix in (("dnb", "shpdnb"), ("mask", "shpskinmask"), ("frequency", "shpfs")):
            params = {**T.PARAMS, "mode": mode, "view": "detail", "generation": loaded["generation"],
                      "path": str(self.root / "ignored" / "not-the-source.png")}
            p = S.parse_params(params)
            if mode == "dnb":
                _, expected, _ = S.dnb_full_result(p)
            else:
                _, total, _ = S.tool_mask(S.STATE["img"], S.STATE["masks"], p, p["S"])
                mask = np.ones(pixels.shape[:2], np.float32) if total is None else total
                if mode == "frequency":
                    layers = S.skintone_layers(S.STATE["source_img"], mask, p)
                    _, expected = S.ST.composite(layers, mask, p["skintone"])
                else:
                    expected = mask
            with self.subTest(mode=mode), patch.object(S, "api_pc_status") as pc:
                saved = self.send(params)
                pc.assert_not_called()  # A local source stays local even if PhotoCraft connects.
                output = Path(saved["path"])
                self.assertEqual(output, self.root / f"synthetic_{suffix}_001.png")
                decoded = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
                self.assertEqual(decoded.shape[:2], pixels.shape[:2])
                if mode == "mask":
                    self.assertEqual(decoded.ndim, 2)
                    self.assertEqual(decoded.dtype, np.uint8)
                    np.testing.assert_array_equal(decoded, S.u8(expected))
                else:
                    self.assertEqual(decoded.dtype, np.uint16)
                    np.testing.assert_array_equal(decoded[..., :3], S.FS.rgb16(expected))
                    self.assertEqual(decoded.shape[2], 3)
                    with Image.open(output) as image:
                        self.assertEqual(image.info["icc_profile"], S.SRGB_ICC)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()),
                         ["synthetic.png", "synthetic_shpdnb_001.png", "synthetic_shpfs_001.png",
                          "synthetic_shpskinmask_001.png"])
        np.testing.assert_array_equal(cv2.imread(str(self.image), cv2.IMREAD_UNCHANGED)[..., :3], pixels)

    def test_provenance_disconnect_stale_and_origin_guards(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        original_pixels = S.STATE["img"]
        with self.assertRaises(HTTPError):
            self.post("/api/load", {"path": str(self.root / "missing.png")})
        self.assertIs(S.STATE["img"], original_pixels)
        self.assertEqual(S.STATE["source_kind"], "local")
        with self.assertRaises(HTTPError) as error:
            self.post("/api/send-result", T.PARAMS)
        self.assertEqual(error.exception.code, 403)
        with patch.object(S, "write_local_result") as writer:
            with self.assertRaisesRegex(ValueError, "loaded image changed"):
                S.api_send_result({**T.PARAMS, "generation": loaded["generation"] - 1})
            writer.assert_not_called()
        with patch.object(S, "bridge") as bridge:
            bridge.return_value.fetch.return_value = (self.image, {"name": "PC source"})
            fetched = self.post("/api/pc/fetch", {})
        self.assertEqual(fetched["source_kind"], "photocraft")
        self.assertEqual(S.STATE["source_kind"], "photocraft")
        with patch.object(S, "api_pc_status", return_value={"connected": False}), \
                patch.object(S, "write_local_result") as writer:
            with self.assertRaisesRegex(ValueError, "disconnected"):
                S.api_send_result(T.PARAMS)
            writer.assert_not_called()
        with patch.object(S, "api_pc_status", return_value={"connected": True}), \
                patch.object(S, "api_pc_send", return_value={"sent": True}) as sender:
            self.assertEqual(S.api_send_result(T.PARAMS), {"sent": True})
            sender.assert_called_once_with(T.PARAMS)
        self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(S.STATE["source_kind"], "local")
        self.assertIsNone(S.BRIDGE["doc"])
        with patch.object(S.cv2, "imencode", return_value=(False, None)):
            with self.assertRaisesRegex(ValueError, "could not encode"):
                S.api_send_result({**T.PARAMS, "mode": "mask"})
        self.assertFalse(list(self.root.glob("*_shpskinmask_*")))

    def test_global_formats_compression_alpha_and_mask_exemption(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        result = T.synthetic_image()
        mask = np.ones(result.shape[:2], np.float32)
        with patch.object(S, "dnb_full_result", return_value=([], result, mask)), \
             patch.object(S, "full_skin_mask", return_value=mask), \
             patch.object(S, "combined_result", return_value=(None, None, result)):
            for fmt, compressed, alpha in itertools.product(("png", "tiff"), (False, True), (False, True)):
                options = {"format":fmt, "compressed":compressed, "opaque_alpha":alpha, "parallel":True}
                self.post("/api/global-settings", {"layer_transfer":options})
                for mode in ("dnb", "combine"):
                    with self.subTest(format=fmt, compressed=compressed, alpha=alpha, mode=mode):
                        params = {**T.PARAMS, "mode":mode, "generation":loaded["generation"],
                                  "frequency_transfer":{"format":"png"}}
                        output = Path(self.send(params)["path"])
                        self.assertEqual(output.suffix, "." + fmt)
                        decoded = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
                        self.assertEqual(decoded.dtype, np.uint16)
                        self.assertEqual(decoded.shape[2], 4 if alpha else 3)
                        np.testing.assert_array_equal(decoded[..., :3], S.FS.rgb16(result))
                        with Image.open(output) as image:
                            self.assertEqual(image.info["icc_profile"], S.SRGB_ICC)
                            if fmt == "tiff":
                                self.assertEqual(image.tag_v2[259], 8 if compressed else 1)
                output = Path(self.send({**T.PARAMS, "mode":"mask"})["path"])
                self.assertEqual(output.suffix, ".png")
                self.assertEqual(cv2.imread(str(output), -1).dtype, np.uint8)
            saved = self.post("/api/save", {**T.PARAMS, "mode":"combine"})
            self.assertIn("result.tiff", saved["files"])
            settings = json.loads((Path(saved["dir"]) / "settings.json").read_text())
            self.assertNotIn("frequency_transfer", settings)
            self.assertEqual(settings["output_settings"], options)

    def test_collision_symlink_concurrency_and_failed_write_cleanup(self):
        victim = self.root / "keep.png"
        victim.write_bytes(b"untouched")
        (self.root / "synthetic_shpdnb_001.png").symlink_to(victim)
        (self.root / "synthetic_shpdnb_002.png").write_bytes(b"also untouched")
        with ThreadPoolExecutor(max_workers=6) as pool:
            outputs = list(pool.map(lambda i: S.write_local_result(self.image, "shpdnb", str(i).encode()),
                                    range(6)))
        self.assertEqual(len(set(outputs)), 6)
        self.assertEqual(victim.read_bytes(), b"untouched")
        self.assertEqual((self.root / "synthetic_shpdnb_002.png").read_bytes(), b"also untouched")
        (self.root / "synthetic_shpdnb_001.tiff").symlink_to(victim)
        output = Path(S.write_local_result(self.image, "shpdnb", b"tiff", extension=".tiff"))
        self.assertEqual(output.name, "synthetic_shpdnb_002.tiff")
        self.assertEqual(victim.read_bytes(), b"untouched")
        with self.assertRaisesRegex(ValueError, "extension"):
            S.write_local_result(self.image, "shpdnb", b"bad", extension="/escape")
        before = set(self.root.iterdir())
        with patch.object(local_output.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                S.write_local_result(self.image, "shpfs", b"partial")
        self.assertEqual(set(self.root.iterdir()), before)
        with patch.object(local_output.os, "open", side_effect=PermissionError("read-only directory")):
            with self.assertRaisesRegex(PermissionError, "read-only directory"):
                S.write_local_result(self.image, "shpfs", b"data")
        self.assertEqual(set(self.root.iterdir()), before)
        other = self.root / "other"
        other.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(other, target_is_directory=True)
        original_directory = self.root.stat()
        with self.assertRaisesRegex(ValueError, "source folder changed"):
            S.write_local_result(alias / self.image.name, "shpfs", b"data",
                                 (original_directory.st_dev, original_directory.st_ino))
        self.assertEqual(list(other.iterdir()), [])


class CropLocalResultTests(unittest.TestCase):
    def test_crop_result_http_route_requires_same_origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            for origin in (None, "http://untrusted.invalid", base):
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                with patch.dict(C.ROUTES, {"/api/send-result": Mock(return_value={"saved": True})}):
                    request = Request(base + "/api/send-result", b"{}", headers)
                    if origin == base:
                        with urlopen(request, timeout=5) as response:
                            self.assertEqual(json.load(response), {"saved": True})
                        C.ROUTES["/api/send-result"].assert_called_once_with({})
                    else:
                        with self.assertRaises(HTTPError) as error:
                            urlopen(request, timeout=5)
                        self.assertEqual(error.exception.code, 403)
                        C.ROUTES["/api/send-result"].assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_selected_flattened_output_provenance_collisions_and_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "portrait.png"
            Image.new("RGB", (200, 200), (10, 20, 30)).save(source, icc_profile=S.SRGB_ICC)
            original = source.read_bytes()
            state = {"img": None, "seq": 0, "job": C.X.Job(), "thread": None, "imgs": {}}
            with patch.object(C, "STATE", state):
                loaded = C.api_load({"path": str(source)})
                self.assertEqual(loaded["source_kind"], "local")
                with self.assertRaisesRegex(ValueError, "generate a result first"):
                    C.api_send_result({})
                results = [{"canvas": Image.new("RGB", (240, 260), colour),
                            "layer": Image.new("RGBA", (240, 260)), "margins": (20, 30, 20, 30)}
                           for colour in ((50, 60, 70), (80, 90, 100))]
                state["job"].results = results
                state["job"].result = results[0]
                C.api_select({"index": 1})
                first = root / "portrait_shpcrop_001.png"
                first.symlink_to(source)
                with patch.object(C, "api_pc_status") as pc:
                    saved = C.api_send_result({"seq": loaded["seq"], "path": "/ignored.png"})
                    pc.assert_not_called()
                output = Path(saved["path"])
                self.assertEqual(output, root / "portrait_shpcrop_002.png")
                with Image.open(output) as image:
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.size, (240, 260))
                    self.assertEqual(image.getpixel((0, 0)), (80, 90, 100))
                    self.assertNotIn("icc_profile", image.info)  # Existing Crop output is untagged RGB8.
                self.assertEqual(source.read_bytes(), original)
                next_output = C.api_send_result({})["path"]
                self.assertTrue(next_output.endswith("_003.png"))
                with self.assertRaisesRegex(ValueError, "loaded image changed"):
                    C.api_send_result({"seq": loaded["seq"] - 1})
                with patch.object(C, "running", return_value=True):
                    with self.assertRaisesRegex(ValueError, "running expansion"):
                        C.api_send_result({})
                with patch.object(C, "bridge") as bridge:
                    bridge.return_value.fetch.return_value = (source, {"name": "PC image"})
                    fetched = C.api_fetch({})
                self.assertEqual(fetched["source_kind"], "photocraft")
                state["job"].result = results[1]
                with patch.object(C, "api_pc_status", return_value={"connected": False}), \
                        patch.object(C, "write_local_result") as writer:
                    with self.assertRaisesRegex(ValueError, "disconnected"):
                        C.api_send_result({})
                    writer.assert_not_called()
                with patch.object(C, "api_pc_status", return_value={"connected": True}), \
                        patch.object(C, "api_send", return_value={"sent": True}) as send:
                    self.assertEqual(C.api_send_result({}), {"sent": True})
                    send.assert_called_once_with({})
                C.api_load({"path": str(source)})
                self.assertEqual(state["source_kind"], "local")
                self.assertIsNone(state["doc"])
                self.assertIsNone(state["job"].result)
                before = state.copy()
                with self.assertRaisesRegex(ValueError, "not an image"):
                    C.api_load({"path": str(root / "absent.png")})
                self.assertEqual(state, before)


class NativeChooserTests(unittest.TestCase):
    def test_owned_proxy_sets_child_origin_for_guarded_result_route(self):
        manager = SimpleNamespace(comfy=None, modules={"dnb": SimpleNamespace(phase="running")},
                                  config={"ports": {"dnb": 18765}}, checked=Mock())
        handler = SimpleNamespace(path="/dnb/api/send-result", command="POST",
                                  server=SimpleNamespace(modules=manager), headers={"Content-Length": "2"},
                                  rfile=io.BytesIO(b"{}"), send=Mock(), fail=Mock())
        with patch.object(supervisor.http.client, "HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"saved":true}'
            response.getheader.return_value = "application/json"
            supervisor.ShellHandler.proxy(handler)
            factory.return_value.request.assert_called_once_with(
                "POST", "/api/send-result", b"{}",
                {"Content-Type": "application/json", "Origin": "http://127.0.0.1:18765"})
            handler.fail.assert_not_called()
            handler.send.assert_called_once_with(200, b'{"saved":true}', "application/json")
            handler.path = "/dnb/api/batch"
            handler.rfile = io.BytesIO(b"{}")
            supervisor.ShellHandler.proxy(handler)
            factory.assert_called_with("127.0.0.1", 18765, timeout=None)
            factory.return_value.request.assert_called_with(
                "POST", "/api/batch", b"{}",
                {"Content-Type": "application/json", "Origin": "http://127.0.0.1:18765"})

    def test_native_dialog_selection_cancel_and_frame_capability_without_gtk(self):
        source = Path(S.__file__).parents[2] / "desktop/controller.py"
        tree = ast.parse(source.read_text())
        desktop = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Desktop")
        method = next(node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name == "script_dialog")
        chooser = Mock()
        gtk = SimpleNamespace(FileChooserDialog=Mock(return_value=chooser), FileFilter=Mock(),
                              ResponseType=SimpleNamespace(CANCEL=0, ACCEPT=1),
                              FileChooserAction=SimpleNamespace(OPEN=1, SELECT_FOLDER=2))
        namespace = {"Gtk": gtk, "json": json, "WebKit2": SimpleNamespace(ScriptDialogType=SimpleNamespace(PROMPT=1))}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        host = SimpleNamespace(base="http://127.0.0.1:1234", window=object(), dialog_open=False)
        view = Mock()
        view.get_uri.return_value = host.base + "/"
        for response in (0, 1):
            chooser.reset_mock()
            chooser.run.return_value = response
            chooser.get_filename.return_value = "/chosen/portrait.png"
            script = Mock()
            script.get_dialog_type.return_value = 1
            script.get_message.return_value = "SHP_OPEN_IMAGE_V1"
            self.assertTrue(namespace["script_dialog"](host, view, script))
            if response:
                script.prompt_set_text.assert_called_once_with("/chosen/portrait.png")
            else:
                script.prompt_set_text.assert_not_called()
            chooser.destroy.assert_called_once()
            self.assertFalse(host.dialog_open)
        for request in ("SHP_OPEN_BATCH_FOLDER_V1", "SHP_OPEN_BATCH_IMAGES_V1"):
            chooser.reset_mock()
            gtk.FileChooserDialog.reset_mock()
            chooser.run.return_value = 1
            chooser.get_filenames.return_value = ["/chosen/a.jpg", "/chosen/b.png"]
            script = Mock()
            script.get_dialog_type.return_value = 1
            script.get_message.return_value = request
            self.assertTrue(namespace["script_dialog"](host, view, script))
            if request == "SHP_OPEN_BATCH_FOLDER_V1":
                self.assertEqual(gtk.FileChooserDialog.call_args.kwargs["action"], 2)
                chooser.set_select_multiple.assert_not_called()
                script.prompt_set_text.assert_called_once_with("/chosen/portrait.png")
            else:
                chooser.set_select_multiple.assert_called_once_with(True)
                script.prompt_set_text.assert_called_once_with(json.dumps(["/chosen/a.jpg", "/chosen/b.png"]))
            chooser.destroy.assert_called_once()
            self.assertFalse(host.dialog_open)
        mode = Mock()
        gtk.MessageDialog = Mock(return_value=mode)
        gtk.MessageType = SimpleNamespace(QUESTION=1)
        for response in (0, 1, 2):
            mode.reset_mock()
            chooser.reset_mock()
            mode.run.return_value = response
            chooser.run.return_value = 1
            script = Mock()
            script.get_dialog_type.return_value = 1
            script.get_message.return_value = "SHP_OPEN_BATCH_SOURCE_V1"
            self.assertTrue(namespace["script_dialog"](host, view, script))
            mode.destroy.assert_called_once()
            if response == 0:
                chooser.run.assert_not_called()
                script.prompt_set_text.assert_not_called()
            else:
                expected = {"folder": "/chosen/portrait.png"} if response == 1 else {"paths": ["/chosen/a.jpg", "/chosen/b.png"]}
                script.prompt_set_text.assert_called_once_with(json.dumps(expected))
                chooser.destroy.assert_called_once()
            self.assertFalse(host.dialog_open)
        self.assertIn("WebKit2.UserContentInjectedFrames.ALL_FRAMES", source.read_text())
        self.assertIn("location.origin ===", source.read_text())
