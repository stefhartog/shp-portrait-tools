"""Host-free Photoshop protocol/fidelity tests. Never connects to desktop Photoshop."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

import cv2
import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.shared.photoshop import (
    Broker, CHUNK, CredentialStore, PROTOCOL, Request, handle_http,
)
from shp_photocraft_tools.shared.photocraft import ControlError
from shp_photocraft_tools.modules.dnb.photoshop_bridge import Bridge
from shp_photocraft_tools.modules.dnb import frequency_separation as FS, skintone as ST, photoshop_lut, progress

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
    def test_connector_capabilities_are_optional_and_reset_on_new_session(self):
        broker, _ = paired()
        self.assertEqual(broker.capabilities, frozenset())
        broker.hello("test-session", PROTOCOL, ["hue-lut-device-link-v1"])
        self.assertIn("hue-lut-device-link-v1", broker.capabilities)
        with self.assertRaisesRegex(ValueError, "capabilities"):
            broker.hello("test-session", PROTOCOL, ["invalid capability"])
        broker.hello("next-session", PROTOCOL)
        self.assertEqual(broker.capabilities, frozenset())

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
            def get(connect_host="127.0.0.1", **headers):
                connection = http.client.HTTPConnection(connect_host, server.server_port, timeout=2)
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
            self.assertEqual(get(**headers, Host=f"localhost:{server.server_port + 1}")[0], 403)
            self.assertEqual(get(**headers, Host=f"localhost.evil.example:{server.server_port}")[0], 403)
            self.assertEqual(get(**headers, Host=f"localhost:{server.server_port}",
                                 Origin="http://localhost:18765")[0], 403)
            self.assertEqual(get(**headers, Host=f"localhost:{server.server_port}"), (200, b"null"))
            self.assertEqual(get("localhost", **headers), (200, b"null"))
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
    capabilities = frozenset()

    def __init__(self):
        self.captured = None
        self.inputs = None
        self.capture_details = {}

    def execute(self, kind, params, *, prepare=None, finish=None):
        with tempfile.TemporaryDirectory() as directory:
            job = Request("r", kind, params, self.session, Path(directory))
            if prepare:
                prepare(job)
                self.captured = (job.params, {p.name: p.read_bytes() for p in job.directory.iterdir()})
            if finish:
                doc, pixels, icc = self.inputs
                if params.get("format") == "png":
                    job.blob("png").write_bytes(pixels)
                    return finish(job, {"doc": doc, "png_bytes": len(pixels)})
                else:
                    job.blob("pixels").write_bytes(pixels)
                    job.blob("icc").write_bytes(icc)
                    return finish(job, {"doc": doc, "timing": {"capture": 0, "serialize": 0, "upload": 0},
                                        **self.capture_details})
            return {"document": "Disposable", "sent": True}


class DeviceLinkTests(unittest.TestCase):
    def test_numeric_identity_and_hue_link_have_correct_axes(self):
        axis = np.linspace(0, 1, ST.LUT_SIZE, dtype=np.float32)
        b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
        identity = np.stack((r, g, b), axis=-1)
        hue, = ST.tables(ST.settings({"hue_amount": 100}))
        for table in (identity, hue):
            raw = photoshop_lut.device_link(table)
            self.assertEqual(struct.unpack_from(">I", raw)[0], len(raw))
            self.assertEqual(raw[12:24], b"linkRGB RGB ")
            count, = struct.unpack_from(">I", raw, 128)
            tags = {struct.unpack_from(">4sII", raw, 132 + i * 12)[0]:
                    struct.unpack_from(">4sII", raw, 132 + i * 12)[1:] for i in range(count)}
            offset, length = tags[b"A2B0"]
            self.assertEqual(raw[offset:offset + 4], b"mft2")
            self.assertGreater(length, 200000)
            grid = np.frombuffer(raw, dtype=">u2", count=33 ** 3 * 3, offset=offset + 64).reshape(33, 33, 33, 3)
            for red, green, blue in ((0, 0, 0), (32, 0, 0), (0, 32, 0), (0, 0, 32), (3, 17, 26), (32, 32, 32)):
                expected = np.rint(table[blue, green, red] * 65535).astype(np.uint16)
                np.testing.assert_array_equal(grid[red, green, blue], expected)
            # LittleCMS parsing and execution are independent of the Photoshop host.
            link = ImageCms.ImageCmsProfile(io.BytesIO(raw))
            identity_link = ImageCms.ImageCmsProfile(io.BytesIO(photoshop_lut.device_link(identity)))
            transform = ImageCms.buildTransform(link, identity_link, "RGB", "RGB")
            sample = Image.fromarray(np.array([[[0, 0, 0], [255, 0, 0], [80, 120, 200], [255, 255, 255]]], np.uint8))
            actual = np.asarray(ImageCms.applyTransform(sample, transform))
            if table is identity:
                np.testing.assert_array_equal(actual, np.asarray(sample))
            else:
                self.assertEqual(actual.shape, (1, 4, 3))

    def test_invalid_lookup_values_rejected(self):
        invalid = np.zeros((ST.LUT_SIZE,) * 3 + (3,), np.float32)
        for value in (float("nan"), -0.01, 1.01):
            invalid[0, 0, 0, 0] = value
            with self.assertRaisesRegex(ValueError, "finite RGB"):
                photoshop_lut.device_link(invalid)
        with self.assertRaises(ValueError):
            photoshop_lut.device_link(np.zeros((2, 2, 2, 3)))


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
            capture, doc = self.bridge.fetch(directory)
            np.testing.assert_array_equal(capture.pixels, self.pixels)
            self.assertTrue(capture.pixels.flags.owndata and capture.pixels.flags.c_contiguous)
            self.assertEqual(capture.icc, ICC)
            self.assertFalse(list(Path(directory).iterdir()))
            self.assertEqual(doc["profile_hash"], hashlib.sha256(ICC).hexdigest())

    def test_capture_reports_separate_transfer_and_input_preparation_timings(self):
        self.broker.inputs = (self.doc, self.pixels[..., ::-1].astype("<u2").tobytes(), ICC)
        token = "photoshop-capture-timing-test"
        with tempfile.TemporaryDirectory() as directory, progress.operation(token):
            capture, _ = self.bridge.fetch(directory)
            np.testing.assert_array_equal(capture.pixels, self.pixels)
        steps = progress.snapshot(token, consume=True)["steps"]
        self.assertEqual([step["label"] for step in steps], [
            "Photoshop capture and transfer", "Tools input validation and preparation"])
        self.assertTrue(all(step["seconds"] >= 0 and step["error"] is None for step in steps))
        self.assertEqual(steps[0]["bytes"], self.pixels.nbytes + len(ICC))
        self.assertEqual([d["label"] for d in steps[0]["details"]], [
            "Photoshop pixel capture", "Photoshop pixel serialization", "Connector upload and Tools receipt"])

    def test_send_reports_serial_encoding_bytes_and_separate_transfer(self):
        token = "photoshop-send-timing-test"
        document = {**self.doc, "editor": "photoshop", "profile_hash": hashlib.sha256(ICC).hexdigest()}
        from shp_photocraft_tools.modules.dnb.photoshop_bridge import layer_preparation
        with progress.operation(token), layer_preparation():
            self.bridge.send_result(self.pixels, document, ICC)
        prep, encode, transfer = progress.snapshot(token, consume=True)["steps"]
        self.assertIn("layer preparation", prep["label"])
        self.assertEqual(encode["bytes"], self.pixels.nbytes)
        self.assertEqual(transfer["bytes"], encode["bytes"])
        self.assertEqual(encode["details"][0]["label"], "Corrected Result")
        self.assertLessEqual(encode["details"][0]["seconds"], encode["seconds"])

    def test_raw_tile_metadata_splits_serialization_or_preserves_legacy_timing(self):
        self.broker.inputs = (self.doc, self.pixels[..., ::-1].astype("<u2").tobytes(), ICC)
        for tiles, valid in (([[4, 0, 0]], True), ([[3, 0, 0], [3, 0, 0]], False), ([[4, "0", 0]], False)):
            with self.subTest(tiles=tiles):
                self.broker.capture_details = {"pixel_tiles": tiles}
                token = "photoshop-tile-timing-test"
                with tempfile.TemporaryDirectory() as directory, progress.operation(token):
                    self.bridge.fetch(directory)
                step = progress.snapshot(token, consume=True)["steps"][0]
                labels = [d["label"] for d in step["details"]]
                self.assertEqual("Photoshop pixel serialization" in labels, not valid)
                self.assertEqual("Photoshop getData() readback" in labels, valid)
                self.assertEqual("Connector tile validation and RGB packing" in labels, valid)
                if valid:
                    self.assertEqual(step["pixel_tiles"]["tiles"], tiles)
                    self.assertEqual(step["pixel_tiles"]["pixel_bytes"], self.pixels.nbytes)
                    self.assertEqual(step["pixel_tiles"]["icc_bytes"], len(ICC))
                else:
                    self.assertNotIn("pixel_tiles", step)

    def test_capture_rgb8_is_expanded_exactly_and_wrong_icc_rejected(self):
        pixels = ((self.pixels.astype(np.uint32) + 128) // 257).astype(np.uint8)
        self.broker.inputs = ({**self.doc, "depth": 8}, pixels[..., ::-1].tobytes(), ICC)
        with tempfile.TemporaryDirectory() as directory:
            capture, _ = self.bridge.fetch(directory)
            np.testing.assert_array_equal(capture.pixels, pixels.astype(np.uint16) * 257)
            self.broker.inputs = ({**self.doc, "profile": "Wrong profile"}, self.pixels.tobytes(), ICC)
            with self.assertRaisesRegex(ControlError, "ICC identity"):
                self.bridge.fetch(directory)
            self.broker.inputs = (self.doc, self.pixels.tobytes()[:-1], ICC)
            with self.assertRaisesRegex(ControlError, "byte count"):
                self.bridge.fetch(directory)

    def test_png_capture_preserves_depth_pixels_and_embedded_icc(self):
        from shp_photocraft_tools.modules.dnb.photocraft_bridge import tagged_rgb16
        self.broker.capabilities = frozenset(["png-capture-v1"])
        for depth in (8, 16):
            pixels = self.pixels if depth == 16 else ((self.pixels.astype(np.uint32) + 128) // 257).astype(np.uint8)
            if depth == 16:
                png = tagged_rgb16(pixels, ICC, {"opaque_alpha": False})
            else:
                out = io.BytesIO()
                Image.fromarray(pixels[..., ::-1]).save(out, format="PNG", icc_profile=ICC)
                png = out.getvalue()
            self.broker.inputs = ({**self.doc, "depth": depth}, png, b"")
            with tempfile.TemporaryDirectory() as directory:
                path, doc = self.bridge.fetch(directory, capture_format="png")
                np.testing.assert_array_equal(cv2.imread(str(path), -1),
                                              pixels if depth == 16 else pixels.astype(np.uint16) * 257)
                with Image.open(path) as image:
                    self.assertEqual(image.info["icc_profile"], ICC)
                self.assertEqual(doc["profile_hash"], hashlib.sha256(ICC).hexdigest())

    def test_png_capture_rejects_missing_icc_corruption_transparency_and_old_connector(self):
        from shp_photocraft_tools.modules.dnb.photocraft_bridge import tagged_rgb16
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ControlError, "updated Photoshop connector"):
                self.bridge.fetch(directory, capture_format="png")
            self.broker.capabilities = frozenset(["png-capture-v1"])
            valid = tagged_rgb16(self.pixels, ICC, {"opaque_alpha": False})
            missing = io.BytesIO()
            Image.fromarray(np.zeros((3, 8, 3), np.uint8)).save(missing, format="PNG")
            invalid_size = io.BytesIO()
            Image.fromarray(np.zeros((3, 8, 3), np.uint8)).save(invalid_size, format="PNG", icc_profile=b"x")
            chunk = b"iCCP" + b"bad\0\0not-zlib"
            malformed_icc = (missing.getvalue()[:33] + struct.pack(">I", len(chunk) - 4) + chunk
                             + struct.pack(">I", zlib.crc32(chunk)) + missing.getvalue()[33:])
            alpha = io.BytesIO()
            rgba = np.full((3, 8, 4), 255, np.uint8)
            rgba[0, 0, 3] = 0
            Image.fromarray(rgba).save(alpha, format="PNG", icc_profile=ICC)
            cases = [
                ({**self.doc, "depth": 8}, missing.getvalue(), "no embedded ICC"),
                ({**self.doc, "depth": 8}, malformed_icc, "embedded ICC profile is corrupt or unreadable"),
                ({**self.doc, "depth": 8}, invalid_size.getvalue(), "embedded ICC profile has an invalid size"),
                (self.doc, valid[:-4], "cannot be verified"),
                (self.doc, b"", "empty, oversized or incomplete"),
                ({**self.doc, "depth": 8}, alpha.getvalue(), "non-opaque"),
                ({**self.doc, "profile": "Wrong profile"}, valid, "ICC identity"),
                ({**self.doc, "depth": 8}, valid, "full-depth"),
            ]
            for doc, data, message in cases:
                with self.subTest(message=message):
                    self.broker.inputs = (doc, data, b"")
                    with self.assertRaisesRegex(ControlError, message):
                        self.bridge.fetch(directory, capture_format="png")

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
                bands=[(1, self.pixels), (2, self.pixels)], band_mask8=self.mask, band_mix=65, hue_output="rendered")
            params, blobs = self.broker.captured
            group = params["layers"][0]
            self.assertEqual([node["name"] for node in group["children"]],
                             ["Detail", "Corrections", "Color"])
            detail, corrections, color = group["children"]
            self.assertEqual(detail["blend"], "Linear Light")
            self.assertEqual(color["blend"], "Normal")
            self.assertEqual(corrections["blend"], "Pass Through")
            self.assertEqual([node["name"] for node in corrections["children"]],
                             ["D&B band 2", "D&B band 1", "Hue", "Directional Blur", "Blur"])
            self.assertEqual([node["blend"] for node in corrections["children"]],
                             ["Soft Light", "Soft Light", "Normal", "Normal", "Normal"])
            self.assertEqual([node["opacity"] for node in corrections["children"][:2]], [65, 65])
            self.assertNotIn("mask", group)
            self.assertEqual("mask" in corrections, placement == "folder")
            for node in corrections["children"]:
                self.assertEqual("mask" in node, placement == "layers")
            for name, metadata in params["blobs"].items():
                self.assertEqual(len(blobs[name]), metadata["bytes"])
                if metadata["components"] == 1:
                    np.testing.assert_array_equal(np.frombuffer(blobs[name], np.uint8).reshape(3, 8), self.mask)

    def test_rendered_hue_matches_shared_lut_math_at_captured_stack_values(self):
        settings = ST.settings({})
        layers = {"color": self.pixels, "detail": np.full_like(self.pixels, 32768)}
        self.bridge.send_skintone(layers, settings, self.mask, self.doc, ICC, include_blur=False,
                                  mask_placement="layers", hue_output="rendered")
        params, blobs = self.broker.captured
        hue = params["layers"][0]["children"][1]["children"][0]
        actual = np.frombuffer(blobs[hue["pixels"]], "<u2").reshape(3, 8, 3)[..., ::-1]
        table, = ST.tables(dict(settings, hue_amount=100))
        expected = FS.rgb16(ST.sample(self.pixels[..., ::-1].astype(np.float32) / 65535, table)[..., ::-1])
        np.testing.assert_array_equal(actual, expected)

    def test_native_hue_requires_capability_and_preserves_layer_placement(self):
        settings = ST.settings({"skintone": {"hue_amount": 47}})
        layers = {"color": self.pixels, "detail": np.full_like(self.pixels, 32768)}
        with self.assertRaisesRegex(ControlError, "updated Photoshop connector"):
            self.bridge.send_skintone(layers, settings, self.mask, self.doc, ICC, include_blur=False)
        self.broker.capabilities = frozenset(["hue-lut-device-link-v1"])
        self.bridge.send_skintone(layers, settings, self.mask, self.doc, ICC, include_blur=False)
        default_hue = self.broker.captured[0]["layers"][0]["children"][1]["children"][0]
        self.assertIn("lookup", default_hue)
        for placement in ("folder", "layers"):
            with patch.object(ST, "sample", side_effect=AssertionError("rendered unexpectedly")):
                self.bridge.send_skintone(layers, settings, self.mask, self.doc, ICC, include_blur=False,
                                          mask_placement=placement, hue_output="native")
            params, blobs = self.broker.captured
            group = params["layers"][0]
            self.assertEqual([node["name"] for node in group["children"]], ["Detail", "Corrections", "Color"])
            corrections = group["children"][1]
            self.assertEqual([node["name"] for node in corrections["children"]], ["Hue"])
            hue = corrections["children"][0]
            self.assertEqual((hue["blend"], hue["opacity"]), ("Normal", 47))
            self.assertNotIn("pixels", hue)
            self.assertNotIn("mask", group)
            self.assertEqual("mask" in corrections, placement == "folder")
            self.assertEqual("mask" in hue, placement == "layers")
            self.assertEqual(params["blobs"][hue["lookup"]]["kind"], "icc-lut")
            self.assertEqual(blobs[hue["lookup"]], photoshop_lut.device_link(ST.tables(dict(settings, hue_amount=100))[0]))

    def test_hue_output_is_only_forwarded_to_photoshop(self):
        from shp_photocraft_tools.modules.dnb import ui_server as U
        self.assertEqual(U.parse_params({"mode": "frequency", "S": 181})["photoshop_hue"], "native")
        self.assertEqual(U.parse_params({"mode": "frequency", "S": 181, "photoshop_hue": "rendered"})["photoshop_hue"], "rendered")
        with self.assertRaisesRegex(ValueError, "Hue output"):
            U.parse_params({"mode": "frequency", "S": 181, "photoshop_hue": "unexpected"})
        with patch.dict(U.EDITOR, {"selected": "photocraft"}):
            self.assertEqual(U.photoshop_hue_options({"photoshop_hue": "native"}), {})
        with patch.dict(U.EDITOR, {"selected": "photoshop"}):
            self.assertEqual(U.photoshop_hue_options({"photoshop_hue": "native"}), {"hue_output": "native"})

    def test_bands_group_and_bad_metadata_fail_before_transfer(self):
        self.bridge.send_skin_bands([(1, self.pixels)], self.mask, self.doc, source_icc=ICC)
        params, blobs = self.broker.captured
        group = params["layers"][0]
        self.assertEqual(group["blend"], "Pass Through")
        self.assertNotIn("mask", group)
        corrections = group["children"][0]
        self.assertEqual(corrections["name"], "Corrections")
        self.assertEqual(corrections["blend"], "Pass Through")
        mask_blob = corrections["mask"]
        self.assertEqual(params["blobs"][mask_blob]["components"], 1)
        np.testing.assert_array_equal(np.frombuffer(blobs[mask_blob], np.uint8).reshape(3, 8), self.mask)
        self.assertEqual([node["name"] for node in corrections["children"]], ["D&B band 1"])
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
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp, \
                patch.object(U, "GLOBAL_SETTINGS", Path(temp) / "settings.json"), \
                patch.object(U, "PRESETS", Path(temp) / "presets.json"), \
                patch.dict(U.BRIDGE, {"b": old, "doc": {"index": 3}}), patch.dict(
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
                        self.assertTrue(U.CACHE_COORDINATOR.preview_lock.locked())
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
            U.api_editor({})
            with patch.dict(U.STATE, {"source_kind": "photoshop", "icc": b"converted", "source_icc": ICC}):
                with self.assertRaisesRegex(ValueError, "native sRGB"):
                    U.validate_editor_source({"mode": "dnb"})
                U.validate_editor_source({"mode": "frequency"})


if __name__ == "__main__":
    unittest.main()
