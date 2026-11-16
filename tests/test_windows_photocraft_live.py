"""Opt-in disposable Windows PhotoCraft CLI integration test."""

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge
from shp_photocraft_tools.shared.photocraft import ControlError


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("PHOTOCRAFT_TEST_CLI"),
                     "set PHOTOCRAFT_TEST_CLI to the Windows PhotoCraft CLI")
class WindowsPhotoCraftIntegration(unittest.TestCase):
    def test_fetch_and_send_masked_soft_light_layer(self):
        executable = Path(os.environ["PHOTOCRAFT_TEST_CLI"])
        self.round_trip(executable, desktop=False)

    @unittest.skipUnless(os.environ.get("PHOTOCRAFT_TEST_GUI"), "set PHOTOCRAFT_TEST_GUI for isolated GUI test")
    def test_isolated_gui_fetch_and_send(self):
        self.round_trip(Path(os.environ["PHOTOCRAFT_TEST_GUI"]), desktop=True)

    def round_trip(self, executable, desktop):
        with tempfile.TemporaryDirectory(prefix="shp-pc-test-") as directory:
            root = Path(directory)
            exchange = root / "exchange"
            exchange.mkdir()
            token = root / "token"
            token.write_text(secrets.token_hex(32), encoding="ascii")
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            Image.new("RGB", (64, 48), (100, 120, 140)).save(exchange / "source.png", icc_profile=profile)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            args = ([str(executable), "--control", str(port)] if desktop
                    else [str(executable), "serve", "--port", str(port)])
            args += ["--control-token-file", str(token),
                    "--automation-read-root", str(exchange), "--automation-write-root", str(exchange)]
            env = os.environ.copy()
            env["PHOTOCRAFT_CONFIG_DIR"] = str(root / "config")
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
            bridge = Bridge(port=port, token_file=token, exchange=exchange, read_root=exchange)
            bridge.client.timeout = 10
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        self.fail(f"PhotoCraft CLI exited: {process.stderr.read().decode(errors='replace')}")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("PhotoCraft CLI did not start listening")
                        time.sleep(0.1)
                if desktop:
                    # The listener starts before the GUI's command loop.
                    deadline = time.monotonic() + 60
                    while True:
                        try:
                            bridge.client.call_once("ui.inspect")
                            break
                        except ControlError:
                            if process.poll() is not None or time.monotonic() >= deadline:
                                raise
                            time.sleep(0.5)
                    bridge.client.call_once("app.open", path=str(exchange / "source.png"))
                else:
                    bridge.client.call("doc.open", path="source.png")
                path, document = bridge.fetch(root / "output")
                with Image.open(path) as image:
                    self.assertEqual(image.size, (64, 48))
                    self.assertTrue(image.info.get("icc_profile"))
                    self.assertEqual(image.convert("RGB").getpixel((0, 0)), (100, 120, 140))
                layer = np.full((48, 64, 3), 32768, dtype=np.uint16)
                mask = np.full((48, 64), 192, dtype=np.uint8)
                sent = bridge.send(layer, mask, document)
                self.assertIsNotNone(sent["layer"])
                inspection = bridge.client.run("document.inspect")
                inserted = next(layer for layer in inspection["layers"] if layer["id"] == sent["layer"])
                self.assertEqual(inserted["blend"], "Soft Light")
                self.assertTrue(inserted.get("hasMask") or inserted.get("mask"))
            finally:
                bridge.client.close()
                process.terminate()
                process.wait(timeout=15)
                process.stderr.close()
