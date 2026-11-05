"""Completed batch variations are published before subsequent generation completes."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from shp_photocraft_tools.modules.crop import expand as X
from shp_photocraft_tools.modules.crop import server as S


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
