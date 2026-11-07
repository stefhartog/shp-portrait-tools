"""Native FS test action contracts; no live PhotoCraft or model inference."""

import copy
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from PIL import ImageCms

from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge, tagged_rgb16
from shp_photocraft_tools.shared.photocraft import ControlError
from shp_photocraft_tools.shared.photocraft.bridge import Client


COMMANDS = {
    "filter.blur.gaussianBlur", "filter.noise.median", "image.duplicate", "layer.list",
    "layer.duplicate", "layer.select", "layer.setProps", "layer.arrange.bringToFront",
    "image.applyImage", "layer.groupLayers", "layer.copyToDocument",
    "channel.target.composite", "select.deselect", "layer.moveTo", "layer.delete",
    "file.close", "document.inspect", "channel.target", "channel.setVisible",
}


class NativeFake:
    def __init__(self, bridge, pixels, icc):
        self.bridge, self.pixels, self.icc = bridge, pixels, icc
        self.active = 1
        self.docs = [{"index": 1, "name": "portrait.png", "width": 8, "height": 6}]
        self.calls = []
        self.fail = None
        self.changed = False
        self.revision_changed = False
        self.mismatch = False
        self.target_inspections = 0
        self.commands = COMMANDS.copy()

    def call(self, method, **params):
        self.calls.append((self.active, method, params))
        if method == "engine.commands":
            return [{"id": command} for command in self.commands]
        if method == "ui.inspect":
            return {"session": {"active": self.active, "documents": copy.deepcopy(self.docs)}}
        if method == "app.save":
            pixels = self.pixels.copy()
            if self.mismatch:
                pixels[0, 0, 0] += 1
            (self.bridge.exchange / params["path"]).write_bytes(tagged_rgb16(pixels, self.icc))
        return {}

    def run(self, command, **params):
        self.calls.append((self.active, command, params))
        if command == self.fail:
            raise ControlError("injected " + command)
        if command == "document.activate":
            self.active = params["document"]
        elif command == "document.inspect":
            if self.active == 1:
                self.target_inspections += 1
            return {"mode": "Rgb", "depth": 16, "width": 8, "height": 6,
                    "revision": 2 if self.revision_changed and self.active == 1 and self.target_inspections > 1 else 1,
                    "activeLayer": 10 if self.active == 1 else 20,
                    "hasSelection": True,
                    "history": ["Original", "External edit"] if self.changed and self.active == 1 and self.target_inspections > 1 else ["Original"],
                    "channels": {"target": {"kind": "layerMask"}},
                    "layers": [{"id": 10 if self.active == 1 else 20,
                                "selected": True, "kind": "Pixel"}]}
        elif command == "image.duplicate":
            self.docs.append({"index": 2, "name": params["name"], "width": 8, "height": 6})
            self.active = 2
        elif command == "layer.list":
            return {"layers": [{"id": 20, "kind": "Pixel"}]}
        elif command == "layer.duplicate":
            return {"layer": 21}
        elif command == "layer.groupLayers":
            return {"layer": 30}
        elif command == "layer.copyToDocument":
            self.active = params["document"]
            return {"layers": [99]}
        elif command == "file.close":
            self.docs = [doc for doc in self.docs if doc["index"] != params["document"]]
            self.active = 1
        return {}


class NativeFSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        self.pixels = np.full((6, 8, 3), 25000, np.uint16)
        self.source = self.root / "fetched.png"
        self.source.write_bytes(tagged_rgb16(self.pixels, self.icc))
        self.bridge = Bridge(port=1, token_file=self.root / "unused",
                             exchange=self.root / "exchange", read_root=self.root)
        self.fake = NativeFake(self.bridge, self.pixels, self.icc)
        self.bridge.client = self.fake
        self.doc = self.fake.docs[0].copy()

    def run_test(self, method="gaussian", radius=8):
        return self.bridge.test_native_frequency(
            self.doc, self.source, method, radius,
            source_pixels=self.pixels.astype(np.float32) / np.float32(65535))

    def test_both_methods_exact_radius_targeting_and_cleanup(self):
        for method in ("gaussian", "median"):
            self.fake.calls.clear()
            result = self.run_test(method, 24)
            self.assertEqual(result["radius"], 24)
            self.assertEqual(result["layer"], 99)
            calls = self.fake.calls
            filtered = [(active, p) for active, command, p in calls
                        if command == ("filter.blur.gaussianBlur" if method == "gaussian" else "filter.noise.median")]
            self.assertEqual(filtered, [(2, {"radius": 24, "target": "pixels"})])
            applied = [p for _, command, p in calls if command == "image.applyImage"]
            self.assertEqual(applied[0]["source"], {"document": 2, "layer": 20, "channel": "composite", "invert": True})
            self.assertEqual((applied[0]["blending"], applied[0]["scale"], applied[0]["offset"]), ("add", 2, 0))
            self.assertTrue(all(active == 2 for active, command, _ in calls if command == "select.deselect"))
            self.assertTrue(any(command == "image.duplicate" and p["mergedOnly"] for _, command, p in calls))
            self.assertEqual(len(self.fake.docs), 1)
            self.assertEqual(self.fake.active, 1)
            self.assertEqual(list(self.bridge.exchange.iterdir()), [])
            restored = [p for active, command, p in calls if active == 1 and command == "layer.select"]
            self.assertEqual(restored[-1], {"layer": 10, "mode": "replace"})
            self.assertFalse(any("mask" in command.lower() or command == "app.open" for _, command, _ in calls))

    def test_stale_merged_source_and_destination_do_not_insert(self):
        for flag, message in (("mismatch", "merged pixels"), ("changed", "destination changed"),
                              ("revision_changed", "destination changed")):
            self.fake.target_inspections = 0
            setattr(self.fake, flag, True)
            with self.assertRaisesRegex(ControlError, message):
                self.run_test()
            self.assertFalse(any(c == "layer.copyToDocument" for _, c, _ in self.fake.calls))
            self.assertEqual(len(self.fake.docs), 1)
            self.assertEqual(list(self.bridge.exchange.iterdir()), [])
            setattr(self.fake, flag, False)
            self.fake.calls.clear()

    def test_failure_cleanup_and_compensation_are_explicit(self):
        self.fake.fail = "image.applyImage"
        with self.assertRaisesRegex(ControlError, "injected"):
            self.run_test()
        self.assertEqual(len(self.fake.docs), 1)
        self.assertFalse(any(c == "layer.copyToDocument" for _, c, _ in self.fake.calls))
        self.fake.fail = "layer.moveTo"
        with self.assertRaisesRegex(ControlError, "injected"):
            self.run_test()
        self.assertTrue(any(c == "layer.delete" and p["layer"] == 99 for _, c, p in self.fake.calls))
        self.fake.fail = "file.close"
        with self.assertRaisesRegex(ControlError, "could not close native FS snapshot"):
            self.run_test()

    def test_invalid_method_fractional_median_and_modified_fetch_fail_before_mutation(self):
        for method, radius in (("highpass", 8), ("median", 8.25), ("gaussian", float("nan"))):
            with self.assertRaises(ControlError):
                self.run_test(method, radius)
        self.assertEqual(self.fake.calls, [])
        with self.assertRaisesRegex(ControlError, "fetched source file changed"):
            self.bridge.test_native_frequency(self.doc, self.source, "median", 8,
                                               source_pixels=np.zeros((6, 8, 3), np.float32))
        self.assertFalse(any(c == "image.duplicate" for _, c, _ in self.fake.calls))

    def test_missing_capability_does_not_mutate(self):
        self.fake.commands.remove("filter.noise.median")
        with self.assertRaisesRegex(ControlError, "lacks native FS commands"):
            self.run_test("median")
        self.assertFalse(any(c == "image.duplicate" for _, c, _ in self.fake.calls))

    def test_native_test_ui_and_route_are_removed(self):
        from shp_photocraft_tools.modules.dnb import ui_server as server
        html = server.HTML.read_text()
        self.assertNotIn("pcNativeFS", html)
        self.assertNotIn("nativeFSTest", html)
        self.assertNotIn("/api/native-fs-test", Path(server.__file__).read_text())
        panel = html.split('id="g-frequency"', 1)[1].split("</details>", 1)[0]
        self.assertNotIn('<p class="hint">', panel)
        self.assertNotIn('id="frequencyOn"', html)
        self.assertIn('id="blurOn"', html)

    def test_endpoint_requires_same_origin_and_does_not_call_send(self):
        from shp_photocraft_tools.modules.dnb import ui_server as module
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(module, "api_send_result") as send:
                request = Request(origin + "/api/native-fs-test", b'{"confirmed":true}',
                                  {"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 404)
                request.add_header("Origin", origin)
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 404)
                send.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_native_mutation_transport_does_not_replay_a_lost_reply(self):
        client = Client(1, self.root / "unused")
        client._sock = Mock()
        with patch.object(client, "_exchange", side_effect=ConnectionError("lost reply")) as exchange:
            with self.assertRaisesRegex(ControlError, "may have executed; it was not retried"):
                client.run_once("layer.copyToDocument", document=1)
            exchange.assert_called_once()
        with patch.object(client, "_connect"), patch.object(client, "_exchange", side_effect=[
            ConnectionError("lost reply"), {"ok": True, "result": []},
        ]) as exchange:
            self.assertEqual(client.call("engine.commands"), [])
            self.assertEqual(exchange.call_count, 2)

    def test_lost_snapshot_reply_closes_only_owned_snapshot(self):
        def lost_reply(command, **params):
            result = self.fake.run(command, **params)
            if command == "image.duplicate":
                raise ControlError("command may have executed; it was not retried")
            return result
        self.fake.run_once = lost_reply
        with self.assertRaisesRegex(ControlError, "may have executed"):
            self.run_test()
        self.assertEqual(self.fake.docs, [self.doc])
        self.assertNotIn("layer.copyToDocument", [cmd for _, cmd, _ in self.fake.calls])

    def test_lost_insertion_reply_requires_inspection_not_replay_or_blind_delete(self):
        def lost_reply(command, **params):
            result = self.fake.run(command, **params)
            if command == "layer.copyToDocument":
                raise ControlError("lost reply")
            return result
        self.fake.run_once = lost_reply
        with self.assertRaisesRegex(ControlError, "inspect the destination before retrying"):
            self.run_test()
        commands = [cmd for _, cmd, _ in self.fake.calls]
        self.assertEqual(commands.count("layer.copyToDocument"), 1)
        self.assertNotIn("layer.delete", commands)
        self.assertEqual(self.fake.docs, [self.doc])


if __name__ == "__main__":
    unittest.main()
