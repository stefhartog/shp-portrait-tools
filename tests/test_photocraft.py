"""Shared transport and module-specific send contracts. All PhotoCraft services are fakes."""

import json
import io
import os
import socketserver
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.modules.crop.photocraft_bridge import Bridge as CropBridge
from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge as DnbBridge, tagged_rgb16
from shp_photocraft_tools.shared.photocraft import Client, ControlError, detect_config


class FakePhotoCraft:
    def __init__(self, bridge, headless=False, fail_copy=False, retry_mask=False):
        self.bridge = bridge
        self.headless = headless
        self.fail_copy = fail_copy
        self.retry_mask = retry_mask
        self.calls = []
        self.active = 1
        self.documents = [{"index": 1, "name": "portrait:one.png", "width": 64, "height": 48}]
        self.files = []
        self.history = ["Original"]
        self.icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        self.file_profiles = []

    def session(self):
        return {"active": self.active, "documents": self.documents.copy()}

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "ui.inspect":
            if self.headless:
                raise ControlError("unknown method: ui.inspect")
            return {"session": self.session()}
        if method == "session.list":
            return self.session()
        if method in ("app.save", "doc.save"):
            Image.new("RGB", (64, 48), "red").save(self.bridge.exchange / params["path"], icc_profile=self.icc)
        if method in ("app.open", "doc.open"):
            filename = self.bridge.read_root / params["path"]
            self.files.append(cv2.imread(str(filename), cv2.IMREAD_UNCHANGED))
            with Image.open(filename) as image:
                self.file_profiles.append(image.info.get("icc_profile"))
            self.active = max(d["index"] for d in self.documents) + 1
            self.documents.append({"index": self.active, "name": filename.name})
        return {}

    def run(self, command, **params):
        self.calls.append((command, params))
        if command == "document.activate":
            self.active = params["document"]
        if command == "image.duplicate":
            self.active = max(d["index"] for d in self.documents) + 1
            self.documents.append({"index": self.active, "name": params["name"]})
        if command == "file.close":
            self.documents = [d for d in self.documents if d["index"] != params["document"]]
        if command == "layer.list":
            return {"layers": [{"id": self.active * 10}]}
        if command == "document.inspect":
            return {"history": self.history.copy()}
        if command == "select.loadSelection" and self.retry_mask:
            self.retry_mask = False
            raise ControlError("selection was cleared")
        if command == "layer.copyToDocument":
            if self.fail_copy:
                raise ControlError("copy failed")
            self.history += ["Resize", "Layer"]
            return {"layers": [99]}
        if command == "history.newSnapshot":
            return {"snapshot": 7}
        return {}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bridge(self, cls, **fake_options):
        b = cls(port=1, token_file=self.root / "no-token", exchange=self.root / "exchange", read_root=self.root)
        b.client = FakePhotoCraft(b, **fake_options)
        return b

    def test_open_uses_absolute_path_in_gui_and_relative_when_headless(self):
        for headless in (False, True):
            with self.subTest(headless=headless):
                b = self.bridge(CropBridge, headless=headless)
                b.headless = headless
                b.client.calls.clear()
                rel = "scratch.png"
                Image.new("RGB", (4, 4)).save(b.exchange / rel)
                b._open(rel)
                method, params = next(c for c in b.client.calls if c[0] in ("app.open", "doc.open"))
                if headless:
                    self.assertEqual((method, params["path"]), ("doc.open", "exchange/scratch.png"))
                else:
                    self.assertEqual((method, params["path"]), ("app.open", str(b.exchange / rel)))

    def test_open_refuses_exchange_outside_read_root(self):
        b = self.bridge(CropBridge)
        b.read_root = self.root / "elsewhere"
        with self.assertRaises(ControlError):
            b._open("scratch.png")
        self.assertFalse(any(c[0] == "app.open" for c in b.client.calls))

    def test_status_preserves_authentication_failure_code(self):
        from shp_photocraft_tools.modules.crop import server as crop
        from shp_photocraft_tools.modules.dnb import ui_server as skin
        from shp_photocraft_tools.shared.photocraft.bridge import AuthenticationError

        for module in (crop, skin):
            with self.subTest(module=module.__name__), patch.object(module, "bridge") as bridge:
                bridge.return_value.status.side_effect = AuthenticationError("test authentication refused")
                result = module.api_pc_status({})
                self.assertFalse(result["connected"])
                self.assertEqual(result["code"], "authentication")
                self.assertEqual(result["message"], "test authentication refused")

    def test_config_compatible_aliases_and_precedence(self):
        shared = {"SHP_PC_PORT": "1234", "SHP_PC_TOKEN_FILE": str(self.root / "token"),
                  "SHP_PC_EXCHANGE": str(self.root / "exchange"), "SHP_PC_READ_ROOT": str(self.root)}
        with patch.dict(os.environ, shared, clear=True):
            for prefix, module in (("SHPC", "crop"), ("DNB", "dnb")):
                cfg = detect_config(prefix, module)
                self.assertEqual(cfg["port"], 1234)
                self.assertEqual(cfg["token_file"], self.root / "token")
                self.assertEqual(cfg["exchange"], self.root / "exchange")
                self.assertEqual(cfg["read_root"], self.root)
                self.assertEqual(cfg["config_dir"].parts[-2:], ("shp-photocraft-tools", module))
                with patch.dict(os.environ, {f"{prefix}_PC_PORT": "5678", f"{prefix}_PC_EXCHANGE": str(self.root / module)}):
                    self.assertEqual(detect_config(prefix, module)["port"], 5678)
                    self.assertEqual(detect_config(prefix, module)["exchange"], self.root / module)

    def test_guarded_launch_uses_existing_launcher_without_automation_flags(self):
        launcher = self.root / "guarded-launcher"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o700)
        bridge = self.bridge(DnbBridge)
        with patch.dict(os.environ, {"SHP_PC_VERIFY_FLATPAK": "1",
                                     "SHP_PC_FLATPAK_LAUNCHER": str(launcher)}), \
             patch("shp_photocraft_tools.shared.photocraft.bridge.subprocess.Popen") as popen:
            bridge.launch()
            self.assertEqual(popen.call_args.args[0], [str(launcher)])
        with patch.dict(os.environ, {"SHP_PC_VERIFY_FLATPAK": "1",
                                     "SHP_PC_FLATPAK_LAUNCHER": "/missing"}), \
             patch("shp_photocraft_tools.shared.photocraft.bridge.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ControlError, "guarded PhotoCraft"):
                bridge.launch()
            popen.assert_not_called()

    def test_fetch_both_bridges_desktop_and_headless(self):
        self.assertIs(CropBridge.fetch, DnbBridge.fetch)
        self.assertIs(CropBridge.launch, DnbBridge.launch)
        for cls in (CropBridge, DnbBridge):
            for headless in (False, True):
                with self.subTest(module=cls.module, headless=headless):
                    b = self.bridge(cls, headless=headless)
                    path, doc = b.fetch(self.root / "inbox")
                    self.assertEqual(path.name, "portrait_one.png")
                    with Image.open(path) as image:
                        self.assertEqual(image.size, (64, 48))
                    self.assertEqual(doc["index"], 1)
                    self.assertEqual(b.client.active, 1)
                    self.assertEqual(len(b.client.documents), 1)
                    self.assertEqual(list(b.exchange.iterdir()), [])
                    commands = [c for c, p in b.client.calls]
                    self.assertIn("doc.save" if headless else "app.save", commands)
                    self.assertEqual(b.headless, headless)

    def test_dnb_send_exact_pixels_blend_and_mask_retry(self):
        b = self.bridge(DnbBridge, retry_mask=True)
        mask = np.arange(64 * 48, dtype=np.uint8).reshape(48, 64)
        layer = np.full((48, 64, 3), 32768, np.uint16)
        result = b.send(layer, mask, b.client.documents[0])
        self.assertEqual(result["layer"], 99)
        rgba, imported_mask = b.client.files
        self.assertEqual(rgba.dtype, np.uint16)
        np.testing.assert_array_equal(rgba[..., :3], layer)
        np.testing.assert_array_equal(rgba[..., 3], 65535)
        np.testing.assert_array_equal(imported_mask, mask)
        props = [p for c, p in b.client.calls if c == "layer.setProps"]
        self.assertEqual(props[0]["blend"], "Soft Light")
        self.assertEqual(b.client.file_profiles, [b.client.icc, None])
        self.assertEqual(sum(c == "select.loadSelection" for c, p in b.client.calls), 2)
        self.assertEqual(sum(c == "layer.copyToDocument" for c, p in b.client.calls), 1)
        self.assertFalse(any(c in ("image.crop", "history.newSnapshot") for c, p in b.client.calls))
        self.assertEqual(b.client.active, 1)
        self.assertEqual(len(b.client.documents), 1)
        self.assertEqual(list(b.exchange.iterdir()), [])

    def test_tagged_rgb16_is_byte_identical_to_original_encoder(self):
        rng = np.random.default_rng(42)
        layer = rng.integers(0, 65536, (48, 64, 3), dtype=np.uint16)
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        rgba = np.dstack([layer, np.full(layer.shape[:2], 65535, np.uint16)])
        ok, encoded = cv2.imencode(".png", rgba, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        self.assertTrue(ok)
        png = encoded.tobytes()
        chunk = b"iCCP" + b"Destination\0\0" + zlib.compress(icc)
        original = (png[:33] + struct.pack(">I", len(chunk) - 4) + chunk
                    + struct.pack(">I", zlib.crc32(chunk)) + png[33:])
        actual = tagged_rgb16(layer, icc)
        self.assertEqual(actual, original)
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(actual, np.uint8), -1), rgba)
        with Image.open(io.BytesIO(actual)) as image:
            self.assertEqual(image.info["icc_profile"], icc)

    def test_dnb_refuses_missing_profile_before_copy_and_cleans_scratch(self):
        b = self.bridge(DnbBridge)
        b.client.icc = None
        with self.assertRaisesRegex(ControlError, "destination ICC profile"):
            b.send(np.full((48, 64, 3), 32768, np.uint16),
                   np.full((48, 64), 255, np.uint8), b.client.documents[0])
        self.assertFalse(any(c == "layer.copyToDocument" for c, p in b.client.calls))
        self.assertEqual(b.client.active, 1)
        self.assertEqual(len(b.client.documents), 1)
        self.assertEqual(list(b.exchange.iterdir()), [])

    def test_crop_signed_margins_and_grouped_undo(self):
        b = self.bridge(CropBridge)
        layer = np.zeros((58, 60, 4), np.uint8)
        b.send_expand(layer, b.client.documents[0], (-4, 5, 0, 5))
        self.assertIn(("image.crop", {"x": 4, "y": -5, "width": 60, "height": 58}), b.client.calls)
        self.assertIn(("history.selectSnapshot", {"snapshot": 7}), b.client.calls)
        self.assertFalse(any(c.startswith("select.") or "layerMask" in c for c, p in b.client.calls))
        self.assertEqual(b.client.active, 1)
        self.assertEqual(list(b.exchange.iterdir()), [])

    def test_failed_send_cleans_scratch_and_reports_error(self):
        for cls in (CropBridge, DnbBridge):
            with self.subTest(module=cls.module):
                b = self.bridge(cls, fail_copy=True)
                with self.assertRaisesRegex(ControlError, "copy failed"):
                    if cls is CropBridge:
                        b.send_expand(np.zeros((58, 64, 4), np.uint8), b.client.documents[0], (0, 5, 0, 5))
                    else:
                        b.send(np.zeros((48, 64, 3), np.uint16), np.ones((48, 64), np.uint8), b.client.documents[0])
                self.assertEqual(b.client.active, 1)
                self.assertEqual(len(b.client.documents), 1)
                self.assertEqual(list(b.exchange.iterdir()), [])

    def test_mismatch_refused_before_any_document_mutation(self):
        b = self.bridge(DnbBridge)
        with self.assertRaisesRegex(ControlError, "layer and mask sizes differ"):
            b.send(np.zeros((49, 64, 3), np.uint16), np.zeros((48, 64), np.uint8), b.client.documents[0])
        with self.assertRaisesRegex(ControlError, "fetch the picture again"):
            b.send(np.zeros((49, 64, 3), np.uint16), np.zeros((49, 64), np.uint8), b.client.documents[0])
        self.assertFalse(any(c == "app.open" for c, p in b.client.calls))
        self.assertEqual(list(b.exchange.iterdir()), [])


class TransportTests(unittest.TestCase):
    def test_flatpak_guard_checks_each_connection_before_authentication(self):
        requests = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    msg = json.loads(line)
                    requests.append(msg["method"])
                    reply = ({"ok": False, "error": "synthetic-token leaked"}
                             if msg["method"] == "error"
                             else {"ok": True, "result": {}})
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        from shp_photocraft_tools.shared.photocraft import flatpak_discovery, flatpak_peer
        with tempfile.TemporaryDirectory() as tmp, socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as srv:
            srv.daemon_threads = True
            thread = threading.Thread(target=srv.serve_forever)
            thread.start()
            token = Path(tmp) / "token"
            token.write_text("synthetic-token")
            client = Client(srv.server_address[1], token, timeout=2)
            try:
                with patch.dict(os.environ, {"SHP_PC_VERIFY_FLATPAK": "1"}), \
                     patch.object(flatpak_discovery, "pinned_user_deployment", return_value=(Path("/fake"), "commit")), \
                     patch.object(flatpak_discovery, "find_flatpak_process", return_value=123), \
                     patch.object(flatpak_peer, "verify_flatpak_peer") as verify:
                    verify.side_effect = flatpak_peer.PeerIdentityError("wrong peer")
                    with self.assertRaisesRegex(ControlError, "no token was sent") as failure:
                        client.call("status")
                    self.assertEqual(failure.exception.code, "authentication")
                    self.assertIsNone(client._sock)
                    self.assertEqual(requests, [])
                    verify.side_effect = None
                    client.call("status")
                    client.close()
                    client.call("status")
                    self.assertEqual(verify.call_count, 3)
                    self.assertEqual(requests, ["auth", "status", "auth", "status"])
                    with self.assertRaises(ControlError) as error:
                        client.call("error")
                    self.assertIn("[redacted]", str(error.exception))
                    self.assertNotIn("synthetic-token", str(error.exception))
            finally:
                client.close()
                srv.shutdown()
                thread.join()

    def test_flatpak_guard_rejects_bad_setting_and_never_replays_mutations(self):
        client = Client(1234, "/unused")
        with patch.dict(os.environ, {"SHP_PC_VERIFY_FLATPAK": "invalid"}):
            with self.assertRaisesRegex(ControlError, "verification setting"):
                client.call("status")
        with patch.dict(os.environ, {"SHP_PC_VERIFY_FLATPAK": "1"}), \
             patch.object(client, "_call") as call:
            client.call("app.open", path="/fake")
            call.assert_called_with("app.open", {"path": "/fake"}, 1)
            client.run("layer.copy")
            call.assert_called_with("engine.execute", {"command": "layer.copy", "params": {}}, 1)

    def test_disconnected_warning_uses_configured_control_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "control.token"
            token.write_text("test-only\n")
            for port in (7878, 17878):
                with self.subTest(port=port), patch(
                        "shp_photocraft_tools.shared.photocraft.bridge.socket.create_connection",
                        side_effect=ConnectionRefusedError("fixture disconnected")):
                    client = Client(port, token, timeout=2)
                    with self.assertRaises(ControlError) as raised:
                        client.call("anything")
                    self.assertEqual(str(raised.exception),
                                     f"PhotoCraft not running or contollable (port {port})")
                    self.assertIsNone(client._sock)

    def test_authentication_reconnect_and_refusal(self):
        requests = []
        drop = [True]

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    msg = json.loads(line)
                    requests.append(msg)
                    if msg["method"] == "auth":
                        reply = {"ok": msg["params"]["token"] == "test-only"}
                        if not reply["ok"]:
                            reply["error"] = "bad token"
                    elif msg["method"] == "retry" and drop[0]:
                        drop[0] = False
                        return
                    elif msg["method"] == "refuse":
                        reply = {"ok": False, "error": "test refusal"}
                    else:
                        reply = {"ok": True, "result": msg["params"]}
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        with tempfile.TemporaryDirectory() as tmp, socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as srv:
            srv.daemon_threads = True
            thread = threading.Thread(target=srv.serve_forever)
            thread.start()
            client = None
            try:
                token = Path(tmp) / "control.token"
                token.write_text("test-only\n")
                client = Client(srv.server_address[1], token, timeout=2)
                self.assertEqual(client.run("layer.list", document=1), {"command": "layer.list", "params": {"document": 1}})
                self.assertEqual(client.call("retry", value=42), {"value": 42})
                self.assertEqual(sum(m["method"] == "auth" for m in requests), 2)
                with self.assertRaisesRegex(ControlError, "refuse: test refusal"):
                    client.call("refuse")
                client.close()
                token.write_text("wrong")
                with self.assertRaisesRegex(ControlError, "refused the control token") as refused:
                    client.call("anything")
                self.assertEqual(refused.exception.code, "authentication")
                self.assertIsNone(client._sock)
                token.unlink()
                with self.assertRaisesRegex(ControlError, "no control token"):
                    client.call("anything")
            finally:
                if client:
                    client.close()
                srv.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
