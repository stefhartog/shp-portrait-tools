"""Completed batch variations are published before subsequent generation completes."""

import io
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from shp_photocraft_tools.modules.crop import expand as X
from shp_photocraft_tools.modules.crop import server as S


class ComfyMemoryTests(unittest.TestCase):
    def test_idle_request_uses_fixed_endpoint_and_preserves_crop_state(self):
        state = {"img": object(), "job": object(), "imgs": {"xresult": b"preview"}, "seq": 7}
        before = dict(state)
        with patch.object(S, "STATE", state), patch.object(S, "running", return_value=False), \
                patch.object(X, "COMFY_URL", "http://127.0.0.1:19000"), \
                patch.object(X.Comfy, "_req", autospec=True, side_effect=[
                    b'{"queue_running":[],"queue_pending":[]}', b""]) as request:
            result = S.api_comfy_free({"url": "http://untrusted.invalid"})
            self.assertEqual(result, {"requested": True, "message": "Memory release requested"})
            self.assertEqual(state, before)
            self.assertEqual(request.call_count, 2)
            queue, free = request.call_args_list
            self.assertEqual(queue.args[0].url, "http://127.0.0.1:19000")
            self.assertEqual(queue.args[1:], ("GET", "/queue"))
            self.assertEqual(queue.kwargs, {"timeout": 5})
            self.assertEqual(free.args[1:3], ("POST", "/free"))
            self.assertEqual(json.loads(free.args[3]), {"unload_models": True, "free_memory": True})
            self.assertEqual(free.args[4], {"Content-Type": "application/json"})
            self.assertEqual(free.kwargs, {"timeout": 10})

    def test_local_generation_refuses_before_contacting_comfy(self):
        with patch.object(S, "running", return_value=True), patch.object(X, "Comfy") as client:
            with self.assertRaisesRegex(ValueError, "Crop is generating"):
                S.api_comfy_free({})
            client.assert_not_called()

    def test_running_and_pending_clients_are_both_protected(self):
        for running, pending in (([["another client"]], []), ([], [["another client"]])):
            with self.subTest(running=running, pending=pending), \
                    patch.object(S, "running", return_value=False), \
                    patch.object(X.Comfy, "_req", return_value=json.dumps({
                        "queue_running": running, "queue_pending": pending}).encode()) as request:
                with self.assertRaisesRegex(ValueError, "running or queued work"):
                    S.api_comfy_free({})
                self.assertEqual(request.call_count, 1)

    def test_unverifiable_queue_and_connection_or_free_errors_are_not_success(self):
        for response in (b"not json", b"null", b"{}", b'{"queue_running":[],"queue_pending":null}'):
            with self.subTest(response=response), patch.object(S, "running", return_value=False), \
                    patch.object(X.Comfy, "_req", return_value=response) as request:
                with self.assertRaisesRegex(ValueError, "Could not verify"):
                    S.api_comfy_free({})
                self.assertEqual(request.call_count, 1)
        for failure in ([X.ExpandError("not reachable")],
                        [b'{"queue_running":[],"queue_pending":[]}', X.ExpandError("HTTP 500")]):
            with self.subTest(failure=failure), patch.object(S, "running", return_value=False), \
                    patch.object(X.Comfy, "_req", side_effect=failure):
                with self.assertRaises(X.ExpandError):
                    S.api_comfy_free({})
        with patch.object(S, "running", side_effect=[False, True]), \
                patch.object(X.Comfy, "_req", return_value=b'{"queue_running":[],"queue_pending":[]}') as request:
            with self.assertRaisesRegex(ValueError, "Crop is generating"):
                S.api_comfy_free({})
            self.assertEqual(request.call_count, 1)

    def test_memory_route_requires_same_origin_json(self):
        for origin, content, status in ((None, "application/json", 403),
                ("http://untrusted.invalid", "application/json", 403),
                ("http://127.0.0.1:19001", "text/plain", 403),
                ("http://127.0.0.1:19001", "application/json", 200)):
            with self.subTest(origin=origin, content=content):
                handler = object.__new__(S.Handler)
                handler.path = "/api/comfy/free"
                handler.server = SimpleNamespace(server_port=19001)
                handler.headers = {"Origin": origin, "Content-Type": content, "Content-Length": "2"}
                handler.rfile, handler.send = io.BytesIO(b"{}"), Mock()
                function = Mock(return_value={"requested": True})
                with patch.dict(S.ROUTES, {handler.path: function}):
                    handler.do_api_POST()
                self.assertEqual(handler.send.call_args.args[0], status)
                self.assertEqual(function.call_count, int(status == 200))


class ExpansionLimitTests(unittest.TestCase):
    def test_source_pixel_boundaries_and_signed_margins(self):
        self.assertLessEqual(X.EXPAND_LIMIT, 999)
        for width, height in ((3000, 2000), (6000, 4000), (300, 200)):
            for side in range(4):
                for amount in (999, 1000):
                    margins = [0, 0, 0, 0]
                    margins[side] = amount
                    left, top, right, bottom = margins
                    args = (width, height, None, -left, -top,
                            width + left + right, height + top + bottom)
                    with self.subTest(source=(width, height), side=side, amount=amount):
                        if amount == 999:
                            self.assertEqual(X.frame_margins(*args)[0], tuple(margins))
                        else:
                            with self.assertRaisesRegex(X.ExpandError, "999 px on one side"):
                                X.frame_margins(*args)
        margins, size = X.frame_margins(3000, 3000, (1, 1), -999, -999, 4998)
        self.assertEqual(margins, (999, 999, 999, 999))
        self.assertEqual(size, (4998, 4998))
        # Cropping more than 999 source pixels is still allowed.
        self.assertEqual(X.frame_margins(6000, 4000, None, 2000, -999, 4000, 4999)[0],
                         (-2000, 999, 0, 0))
        with self.assertRaisesRegex(X.ExpandError, "nothing to generate"):
            X.frame_margins(6000, 4000, None, 2000, 1000, 3000, 2000)

    def test_invalid_api_geometry_never_starts_either_workflow(self):
        for method in ("flux2", "flux1_fill"):
            state = {"img": Image.new("RGB", (200, 200)), "job": X.Job(),
                     "thread": None, "imgs": {"xresult": b"existing preview"}}
            old_job = state["job"]
            with self.subTest(method=method), patch.object(S, "STATE", state), \
                    patch.object(X, "load_settings", return_value=dict(X.DEFAULTS, method=method)) as settings, \
                    patch.object(S.threading, "Thread") as thread:
                with self.assertRaisesRegex(X.ExpandError, "999 px on one side"):
                    S.api_run({"ratio": None, "x": -1000, "y": 0, "w": 1200, "h": 200})
                settings.assert_not_called()
                thread.assert_not_called()
                self.assertIs(state["job"], old_job)
                self.assertEqual(state["imgs"]["xresult"], b"existing preview")


class IncrementalResultTests(unittest.TestCase):
    def test_published_previews_selection_and_partial_stop(self):
        for outcome in ("done", "cancelled", "error"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                waiting, release = threading.Event(), threading.Event()
                calls = []

                def generate(comfy, name, size, margins, prompt, job, seed, settings):
                    calls.append(seed)
                    if len(calls) == 3:
                        waiting.set()
                        if not release.wait(5):
                            raise AssertionError("test release timed out")
                        if outcome == "cancelled":
                            raise X.Cancelled()
                        if outcome == "error":
                            raise X.ExpandError("synthetic third variation failed")
                    canvas = Image.new("RGB", (200, 300), (seed, 20, 30))
                    return {"canvas": canvas, "layer": canvas.convert("RGBA"), "margins": margins}

                state = {"img": Image.new("RGB", (200, 200)), "name": "fixture",
                         "thread": None, "job": X.Job(), "imgs": {}}
                with patch.object(S, "STATE", state), patch.object(S, "SETTINGS_FILE", Path(tmp) / "settings.json"), \
                        patch.object(X, "status", return_value={"ok": True}), \
                        patch.object(X.Comfy, "upload", return_value="stub.png"), patch.object(X, "expand", generate):
                    S.api_run({"ratio": [2, 3], "x": 0, "y": -50, "w": 200, "seed": 10, "variations": 4})
                    try:
                        self.assertTrue(waiting.wait(5))
                        snapshot = S.api_job({})
                        self.assertEqual(snapshot["state"], "running")
                        self.assertEqual(snapshot["count"], 2)
                        self.assertIn("xvar0", state["imgs"])
                        self.assertIn("xvar1", state["imgs"])
                        self.assertNotIn("xvar2", state["imgs"])
                        S.api_select({"index": 1})
                        self.assertEqual(S.api_job({})["selected"], 1)
                        self.assertEqual(state["job"].result["canvas"].getpixel((0, 0)), (11, 20, 30))
                        if outcome == "cancelled":
                            S.api_cancel({})
                    finally:
                        release.set()
                        state["thread"].join(5)
                    self.assertFalse(state["thread"].is_alive())
                    snapshot = S.api_job({})
                    self.assertEqual(snapshot["state"], outcome)
                    self.assertEqual(snapshot["count"], 4 if outcome == "done" else 2)
                    self.assertEqual(snapshot["selected"], 1)
                    self.assertEqual(state["job"].result["canvas"].getpixel((0, 0)), (11, 20, 30))
                    if outcome == "cancelled":
                        self.assertIn("Kept 2", snapshot["message"])
                    if outcome == "error":
                        self.assertIn("synthetic third variation failed", snapshot["message"])
