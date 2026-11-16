"""Host-free Photoshop protocol/fidelity tests. Never connects to desktop Photoshop."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.shared.photoshop import (
    Broker, CHUNK, CredentialStore, PROTOCOL, Request, handle_http,
)
from shp_photocraft_tools.shared.photocraft import ControlError
from shp_photocraft_tools.modules.dnb.photoshop_bridge import Bridge
from shp_photocraft_tools.modules.dnb import frequency_separation as FS, skintone as ST

ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
PROFILE = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(ICC))).strip()


class MemoryStore:
    def __init__(self):
        self.value = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def clear(self):
        self.value = None


def paired(**kwargs):
    broker = Broker(MemoryStore(), **kwargs)
    token = broker.pair(broker.pairing_code()["code"])["token"]
    broker.hello("test-session", PROTOCOL)
    return broker, token


class BrokerTests(unittest.TestCase):
    def test_pairing_expires_is_rate_limited_and_revoke_invalidates(self):
        clock = [0]
        broker, token = paired(clock=lambda: clock[0])
        self.assertTrue(broker.authorize("Bearer " + token))
        code = broker.pairing_code()["code"]
        clock[0] = 121
        with self.assertRaisesRegex(ValueError, "fresh pairing"):
            broker.pair(code)
        code = broker.pairing_code()["code"]
        for _ in range(5):
            with self.assertRaisesRegex(ValueError, "Incorrect"):
                broker.pair("wrong")
        with self.assertRaisesRegex(ValueError, "fresh pairing"):
            broker.pair(code)
        broker.revoke()
        self.assertFalse(broker.authorize("Bearer " + token))
        self.assertIsNone(broker.session)
        self.assertIsNone(broker.store.value)

    def poll(self, broker):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            result = broker.poll()
            if result:
                return result
            time.sleep(.002)
        self.fail("No Photoshop command was queued")

    def test_bounded_ordered_upload_and_no_replay_after_delivery(self):
        broker, _ = paired(timeout=2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.execute, "capture", {},
                                 finish=lambda job, result: job.blob("pixels").read_bytes())
            command = self.poll(broker)
            self.assertIsNone(broker.poll())
            rid = command["request"]
            with self.assertRaisesRegex(ControlError, "already"):
                broker.execute("capture", {})
            broker.upload(rid, "pixels", 0, b"abc")
            for name, offset, data in (("../outside", 0, b"x"), ("pixels", 0, b"x"),
                                       ("pixels", 3, b"x" * (CHUNK + 1))):
                with self.assertRaises(ValueError):
                    broker.upload(rid, name, offset, data)
            broker.upload(rid, "pixels", 3, b"def")
            broker.reply(rid, {"result": {}})
            self.assertEqual(future.result(), b"abcdef")
        self.assertEqual(broker.request_status(rid)["state"], "completed")
        self.assertIsNone(broker.poll())
        with self.assertRaises(ValueError):
            broker.reply(rid, {"result": {}})

    def test_timeout_and_session_restart_are_uncertain_not_requeued(self):
        broker, _ = paired(timeout=.03)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.execute, "apply", {})
            rid = self.poll(broker)["request"]
            with self.assertRaisesRegex(ControlError, "NOT replayed"):
                future.result()
        self.assertEqual(broker.request_status(rid)["state"], "uncertain")
        self.assertIsNone(broker.poll())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.execute, "capture", {})
            self.poll(broker)
            broker.hello("new-session", PROTOCOL)
            with self.assertRaisesRegex(ControlError, "restarted"):
                future.result()

    def test_http_rejects_browser_origin_bad_host_and_missing_auth(self):
        broker, token = paired()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, body, content_type):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                handle_http(self, broker)

            def do_POST(self):
                handle_http(self, broker)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(**headers):
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                try:
                    connection.request("GET", "/api/photoshop/poll", headers=headers)
                    response = connection.getresponse()
                    body = response.read()
                    return response.status, body
                finally:
                    connection.close()

            headers = {"Authorization": "Bearer " + token, "X-SHP-Session": "test-session"}
            self.assertEqual(get()[0], 401)
            self.assertEqual(get(**headers, Origin="http://evil.example")[0], 403)
            self.assertEqual(get(**headers, Host="evil.example")[0], 403)
            self.assertEqual(get(Authorization="Bearer " + token)[0], 400)
            self.assertEqual(get(**headers), (200, b"null"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_credential_is_encrypted_and_user_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "credential")
            token = "secret-token-test"
            store.save(token)
            self.assertNotIn(token.encode(), store.path.read_bytes())
            self.assertEqual(store.load(), token)
            store.path.write_bytes(b"corrupt")
            with self.assertRaises(ControlError):
                store.load()
            store.clear()
            self.assertIsNone(store.load())


class RecordingBroker:
    session = "test-session"

    def __init__(self):
        self.captured = None
        self.inputs = None

    def execute(self, kind, params, *, prepare=None, finish=None):
        with tempfile.TemporaryDirectory() as directory:
            job = Request("r", kind, params, self.session, Path(directory))
            if prepare:
                prepare(job)
                self.captured = (job.params, {p.name: p.read_bytes() for p in job.directory.iterdir()})
            if finish:
                doc, pixels, icc = self.inputs
                job.blob("pixels").write_bytes(pixels)
                job.blob("icc").write_bytes(icc)
                return finish(job, {"doc": doc})
            return {"document": "Disposable", "sent": True}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.broker = RecordingBroker()
        self.bridge = Bridge(self.broker)
        self.doc = {"index": 1, "name": "Disposable", "width": 8, "height": 3, "depth": 16,
                    "mode": "RGB", "alpha": "opaque", "profile": PROFILE, "capture": "c", "marker": "m",
                    "protocol": 1, "session": self.broker.session, "editor": "photoshop",
                    "profile_hash": hashlib.sha256(ICC).hexdigest()}
        ramp = np.array([0, 1, 255, 256, 32767, 32768, 65534, 65535], np.uint16)
        self.pixels = np.tile(ramp[None, :, None], (3, 1, 3))
        self.pixels[..., 2] = 65535 - self.pixels[..., 0]
        self.mask = np.tile(np.array([0, 1, 64, 127, 128, 192, 254, 255], np.uint8), (3, 1))

    def test_capture_rgb16_ramp_and_icc_are_lossless_no_channel_swap(self):
        self.broker.inputs = (self.doc, self.pixels[..., ::-1].astype("<u2").tobytes(), ICC)
        with tempfile.TemporaryDirectory() as directory:
            path, doc = self.bridge.fetch(directory)
            np.testing.assert_array_equal(cv2.imread(str(path), -1), self.pixels)
            with Image.open(path) as image:
                self.assertEqual(image.info["icc_profile"], ICC)
            self.assertEqual(doc["profile_hash"], hashlib.sha256(ICC).hexdigest())

    def test_capture_rgb8_is_expanded_exactly_and_wrong_icc_rejected(self):
        pixels = ((self.pixels.astype(np.uint32) + 128) // 257).astype(np.uint8)
        self.broker.inputs = ({**self.doc, "depth": 8}, pixels[..., ::-1].tobytes(), ICC)
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.bridge.fetch(directory)
            np.testing.assert_array_equal(cv2.imread(str(path), -1), pixels.astype(np.uint16) * 257)
            self.broker.inputs = ({**self.doc, "profile": "Wrong profile"}, self.pixels.tobytes(), ICC)
            with self.assertRaisesRegex(ControlError, "ICC identity"):
                self.bridge.fetch(directory)
            self.broker.inputs = (self.doc, self.pixels.tobytes()[:-1], ICC)
            with self.assertRaisesRegex(ControlError, "byte count"):
                self.bridge.fetch(directory)

    def test_result_numeric_rgb_quantization_and_profile_guard(self):
        for depth in (8, 16):
            self.bridge.send_result(self.pixels, {**self.doc, "depth": depth}, ICC)
            params, blobs = self.broker.captured
            name = params["layers"][0]["pixels"]
            pixels = np.frombuffer(blobs[name], dtype="<u2" if depth == 16 else np.uint8).reshape(3, 8, 3)
            expected = self.pixels[..., ::-1] if depth == 16 else (
                (self.pixels[..., ::-1].astype(np.uint32) + 128) // 257).astype(np.uint8)
            np.testing.assert_array_equal(pixels, expected)
        with self.assertRaisesRegex(ControlError, "ICC identity"):
            self.bridge.send_result(self.pixels, self.doc, b"other")
        with self.assertRaisesRegex(ControlError, "session changed"):
            self.bridge.send_result(self.pixels, {**self.doc, "session": "old"}, ICC)

    def test_fs_combine_layer_order_modes_and_both_mask_placements(self):
        for placement in ("folder", "layers"):
            self.bridge.send_skintone(
                {name: self.pixels for name in ("color", "blur", "detail")}, ST.settings({}),
                self.mask, self.doc, ICC, mask_placement=placement, blur_strength=43,
                directional=(self.pixels, self.mask, 37),
                bands=[(1, self.pixels), (2, self.pixels)], band_mask8=self.mask, band_mix=65)
            params, blobs = self.broker.captured
            group = params["layers"][0]
            self.assertEqual([node["name"] for node in group["children"]],
                             ["D&B band 2", "D&B band 1", "Detail", "Hue", "Directional Blur", "Blur", "Color"])
            self.assertEqual([node["blend"] for node in group["children"]],
                             ["Soft Light", "Soft Light", "Linear Light", "Normal", "Normal", "Normal", "Normal"])
            self.assertEqual([node["opacity"] for node in group["children"][:3]], [65, 65, 100])
            self.assertEqual("mask" in group, placement == "folder")
            for node in group["children"]:
                self.assertEqual("mask" in node, placement == "layers" and node["name"] not in ("Color", "Detail"))
            for name, metadata in params["blobs"].items():
                self.assertEqual(len(blobs[name]), metadata["bytes"])
                if metadata["components"] == 1:
                    np.testing.assert_array_equal(np.frombuffer(blobs[name], np.uint8).reshape(3, 8), self.mask)

    def test_rendered_hue_matches_shared_lut_math_at_captured_stack_values(self):
        settings = ST.settings({})
        layers = {"color": self.pixels, "detail": np.full_like(self.pixels, 32768)}
        self.bridge.send_skintone(layers, settings, self.mask, self.doc, ICC, include_blur=False, mask_placement="layers")
        params, blobs = self.broker.captured
        hue = params["layers"][0]["children"][1]
        actual = np.frombuffer(blobs[hue["pixels"]], "<u2").reshape(3, 8, 3)[..., ::-1]
        table, = ST.tables(dict(settings, hue_amount=100))
        expected = FS.rgb16(ST.sample(self.pixels[..., ::-1].astype(np.float32) / 65535, table)[..., ::-1])
        np.testing.assert_array_equal(actual, expected)

    def test_bands_group_and_bad_metadata_fail_before_transfer(self):
        self.bridge.send_skin_bands([(1, self.pixels)], self.mask, self.doc, source_icc=ICC)
        self.assertEqual(self.broker.captured[0]["layers"][0]["blend"], "Pass Through")
        with self.assertRaisesRegex(ControlError, "unique"):
            self.bridge.send_skin_bands([(1, self.pixels), (1, self.pixels)], self.mask, self.doc, source_icc=ICC)
        with self.assertRaisesRegex(ControlError, "opacity"):
            self.bridge.send_skin_bands([(1, self.pixels)], self.mask, self.doc, source_icc=ICC, band_mix=float("nan"))
        with self.assertRaisesRegex(ControlError, "RGB16"):
            self.bridge.send_result(self.pixels.astype(np.uint8), self.doc, ICC)

    def test_editor_selection_does_not_replace_photocraft_transport(self):
        from shp_photocraft_tools.modules.dnb import ui_server as U
        old = object()
        broker, _ = paired()
        with patch.dict(U.BRIDGE, {"b": old, "doc": {"index": 3}}), patch.dict(
                U.EDITOR, {"selected": "photocraft", "photoshop": None, "broker": broker}):
            self.assertIs(U.bridge(), old)
            U.api_editor({"editor": "photoshop"})
            self.assertIsNone(U.BRIDGE["doc"])
            self.assertIsInstance(U.bridge(), Bridge)
            U.api_editor({"editor": "photocraft"})
            self.assertIs(U.bridge(), old)
            with patch.dict(U.STATE, {"source_kind": "photoshop"}):
                with self.assertRaisesRegex(ValueError, "differs"):
                    U.validate_editor_source({"mode": "frequency"})

    def test_connector_can_answer_while_tools_editor_call_holds_processing_lock(self):
        from shp_photocraft_tools.modules.dnb import ui_server as U
        broker, token = paired(timeout=2)
        with patch.dict(U.EDITOR, {"selected": "photoshop", "photoshop": None, "broker": broker}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"

            def request(method, route, value=None, headers=None):
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    try:
                        body = json.dumps(value).encode() if value is not None else None
                        connection.request(method, route, body, headers or {})
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        connection.close()

            try:
                    self.assertEqual(request("POST", "/api/pc/status", {}, {"Content-Type": "application/json"})[0], 403)
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(request, "POST", "/api/pc/status", {},
                                             {"Content-Type": "application/json", "Origin": origin})
                        headers = {"Authorization": "Bearer " + token, "X-SHP-Session": "test-session"}
                        deadline = time.monotonic() + 1
                        job = None
                        while job is None and time.monotonic() < deadline:
                            code, job = request("GET", "/api/photoshop/poll", headers=headers)
                            self.assertEqual(code, 200)
                            if job is None:
                                time.sleep(.002)
                        self.assertIsNotNone(job)
                        self.assertEqual(job["kind"], "status")
                        self.assertTrue(U.LOCK.locked())
                        code, response = request("POST", "/api/photoshop/reply",
                                                 {"request": job["request"], "value": {"result": {"doc": None}}},
                                                 {**headers, "Content-Type": "application/json"})
                        self.assertEqual(code, 200)
                        self.assertTrue(response["accepted"])
                        code, response = future.result()
                        self.assertEqual(code, 200)
                        self.assertTrue(response["connected"])
            finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
            U.api_editor({"editor": "photoshop"})
            with patch.dict(U.STATE, {"source_kind": "photoshop", "icc": b"converted", "source_icc": ICC}):
                with self.assertRaisesRegex(ValueError, "native sRGB"):
                    U.validate_editor_source({"mode": "dnb"})
                U.validate_editor_source({"mode": "frequency"})


if __name__ == "__main__":
    unittest.main()
