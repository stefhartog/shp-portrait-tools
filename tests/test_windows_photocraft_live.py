"""Opt-in disposable Windows PhotoCraft CLI integration test."""

import os
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from shp_portrait_tools.modules.skin.photocraft_bridge import Bridge
from shp_portrait_tools.shared.photocraft import ControlError
from shp_portrait_tools.shared.photocraft.bridge import AuthenticationError


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("PHOTOCRAFT_TEST_CLI"),
                     "set PHOTOCRAFT_TEST_CLI to the Windows PhotoCraft CLI")
class WindowsPhotoCraftIntegration(unittest.TestCase):
    def test_fetch_and_send_masked_soft_light_layer(self):
        executable = Path(os.environ["PHOTOCRAFT_TEST_CLI"])
        self.round_trip(executable, desktop=False)

    @unittest.skipUnless(os.environ.get("PHOTOCRAFT_TEST_GUI"), "set PHOTOCRAFT_TEST_GUI for isolated GUI test")
    def test_isolated_gui_fetch_and_send(self):
        self.round_trip(Path(os.environ["PHOTOCRAFT_TEST_GUI"]), desktop=True)

    @unittest.skipUnless(os.environ.get("PHOTOCRAFT_TEST_GUI"), "set PHOTOCRAFT_TEST_GUI for isolated GUI test")
    def test_launch_then_reuse_gui(self):
        with tempfile.TemporaryDirectory(prefix="shp-pc-launch-") as directory:
            root = Path(directory)
            exchange = root / "exchange"
            exchange.mkdir()
            token = root / "token"
            token.write_text(secrets.token_hex(32), encoding="ascii")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            bridge = Bridge(port=port, token_file=token, exchange=exchange, read_root=exchange)
            bridge.config_dir = root / "bridge"
            (root / "editor").mkdir()
            spawned = []
            real_popen = subprocess.Popen
            def start(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process
            with patch.dict(os.environ, {"PHOTOCRAFT_BIN": os.environ["PHOTOCRAFT_TEST_GUI"],
                                         "PHOTOCRAFT_CONFIG_DIR": str(root / "editor")}), \
                 patch("shp_portrait_tools.shared.photocraft.bridge.subprocess.Popen", side_effect=start):
                try:
                    self.assertIn("launched", bridge.launch())
                    self.assertTrue(bridge.launch()["reused"])
                    self.assertEqual(len(spawned), 1)
                finally:
                    for process in spawned:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=15)

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
                    bridge.config_dir = root / "bridge-config"
                    self.assertTrue(bridge.launch()["reused"])
                    wrong_token = root / "wrong-token"
                    wrong_token.write_text(secrets.token_hex(32), encoding="ascii")
                    wrong = Bridge(port=port, token_file=wrong_token, exchange=exchange, read_root=exchange)
                    wrong.config_dir = root / "wrong-bridge"
                    with self.assertRaises(AuthenticationError):
                        wrong.launch()
                    self.assertIsNone(process.poll())
                    self._check_service_startup(root, exchange, token, port, executable)
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

    def _check_service_startup(self, root, exchange, token, port, executable):
        models = root / "models"
        models.mkdir()
        for name in ("yunet.onnx", "face_landmarks.onnx", "selfie_multiclass_256x256.onnx"):
            (models / name).touch()
        ports = []
        reservations = []
        try:
            for _ in range(3):
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                reservations.append(sock)
                ports.append(sock.getsockname()[1])
        finally:
            for sock in reservations:
                sock.close()
        config = root / "launcher.json"
        config.write_text(json.dumps({"models": str(models), "ports": {"skin": ports[0]},
            "shell_port": ports[2], "startup": {"skin": True},
            "photocraft": {"port": port, "token_file": str(token), "exchange": str(exchange),
                "read_root": str(exchange), "executable": str(executable), "editor_config": str(root / "config")}}))
        code = """
import json, sys, urllib.error, urllib.request
from pathlib import Path
from shp_portrait_tools.launcher import read_config, apply_photocraft_env
from shp_portrait_tools.supervisor import SkinWorker
config = read_config(Path(sys.argv[1])); apply_photocraft_env(config)
root = Path(sys.argv[2])
modules = SkinWorker(config, root / 'service-data', root / 'service-logs', 'test-instance')
try:
    modules.start()
    url = 'http://127.0.0.1:' + str(config['ports']['skin'])
    request = urllib.request.Request(url + '/api/pc/launch', data=b'{}', method='POST',
        headers={'Origin':url,'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get('reused'): raise RuntimeError('existing authenticated editor was not reused')
    request = urllib.request.Request(url + '/api/pc/launch', data=b'{}', method='POST',
        headers={'Origin':'http://evil.invalid','Content-Type':'application/json'})
    try:
        urllib.request.urlopen(request, timeout=20)
        raise RuntimeError('cross-origin launch accepted')
    except urllib.error.HTTPError as error:
        if error.code != 403: raise
    with urllib.request.urlopen(url + '/api/global-settings', timeout=20) as response:
        if not json.load(response).get('photocraft_launch_available'): raise RuntimeError('missing Windows action')
finally:
    modules.close(strict=True)
"""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SHP_PC_", "DNB_PC_", "SHPC_PC_", "PHOTOCRAFT_"))}
        env["APPDATA"] = str(root / "appdata")
        (root / "appdata").mkdir()
        result = subprocess.run([sys.executable, "-c", code, str(config), str(root)], cwd=root,
                                env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
