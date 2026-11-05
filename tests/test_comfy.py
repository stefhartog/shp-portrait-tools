"""Owned ComfyUI lifecycle, isolated stub runtime only."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from shp_photocraft_tools.comfy import ManagedComfy, listening, owns_listener
from shp_photocraft_tools.launcher import LaunchError, read_config
from shell_preview import fixture


def runtime(root):
    directory = root / "ComfyUI"
    (directory / "venv/bin").mkdir(parents=True)
    (directory / "venv/bin/python").symlink_to(sys.executable)
    (directory / "main.py").write_text(Path(__file__).with_name("comfy_child.py").read_text())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return {"directory": str(directory), "port": port}


class ComfyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = runtime(self.root)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.managed = ManagedComfy(self.config, self.logs)
        self.addCleanup(self.managed.close)

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.managed.status()
            if predicate(status):
                return status
            time.sleep(.05)
        self.fail(f"Timed out: {status}")

    def test_start_reuse_owned_listener_busy_drain_and_stop(self):
        (Path(self.config["directory"]) / "delay").write_text(".4")
        self.assertTrue(self.managed.start()["started"])
        self.assertFalse(self.managed.start()["started"])
        state = self.wait(lambda s: s["phase"] == "running")
        self.assertTrue(owns_listener(state["pid"], self.config["port"]))
        busy = Path(self.config["directory"]) / "busy"
        busy.touch()
        self.wait(lambda s: s["busy"])
        closing = threading.Thread(target=self.managed.close)
        closing.start()
        time.sleep(1)
        self.assertTrue(closing.is_alive(), "Shutdown killed active ComfyUI work")
        self.assertTrue(listening(self.config["port"]))
        busy.unlink()
        closing.join(10)
        self.assertFalse(closing.is_alive())
        self.assertFalse(listening(self.config["port"]))

    def test_collision_never_adopts_or_stops_listener(self):
        with socket.socket() as unrelated:
            unrelated.bind(("127.0.0.1", self.config["port"]))
            unrelated.listen()
            self.managed.start()
            state = self.wait(lambda s: s["phase"] == "error")
            self.assertIn("occupied", state["error"])
            self.managed.close()
            self.assertTrue(listening(self.config["port"]))

    def test_invalid_configuration_is_rejected(self):
        models = self.root / "models"
        models.mkdir()
        for name in ("yunet.onnx", "face_landmarks.onnx", "selfie_multiclass_256x256.onnx"):
            (models / name).touch()
        path = self.root / "launcher.json"
        for bad in (dict(self.config, port=8188), dict(self.config, port=18760),
                    dict(self.config, directory="relative/path")):
            path.write_text(json.dumps({"models": str(models), "comfy": bad}))
            with self.assertRaises(LaunchError):
                read_config(path)

    def test_startup_failure_and_retry(self):
        fail = Path(self.config["directory"]) / "fail"
        fail.touch()
        self.managed.start()
        state = self.wait(lambda s: s["phase"] == "error")
        self.assertIn("7", state["error"])
        self.managed.process.wait(timeout=5)
        fail.unlink()
        self.assertTrue(self.managed.start()["started"])
        self.wait(lambda s: s["phase"] == "running")

    def test_watchdog_cleans_child_after_parent_disappears(self):
        code = ("from pathlib import Path; import time; "
                "from shp_photocraft_tools.comfy import ManagedComfy; "
                f"m=ManagedComfy({self.config!r},Path({str(self.logs)!r}));m.start();"
                "print(str(m.path),flush=True);time.sleep(2)")
        parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        path = Path(parent.stdout.readline().strip())
        parent.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists() and json.loads(path.read_text())["phase"] == "stopped":
                break
            time.sleep(.1)
        else:
            self.fail("Parent loss did not clean up owned ComfyUI")
        self.assertFalse(listening(self.config["port"]))
        parent.stdout.close()

    def test_shell_routes_crop_to_owned_comfy_and_reuses_start(self):
        with fixture(comfy=self.config) as (server, root):
            base = f"http://127.0.0.1:{server.server_port}"
            managed = server.modules.comfy
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = server.modules.status()
                if all(s["phase"] == "running" for s in status["modules"].values()) and managed.status()["phase"] == "running":
                    break
                time.sleep(.1)
            else:
                self.fail(status)
            with urlopen(Request(base + "/crop/api/status", data=b"{}",
                                 headers={"Origin": base, "Content-Type": "application/json"})) as response:
                self.assertEqual(json.load(response)["comfy"], managed.url)
            req = Request(base + "/crop/api/comfy/start", data=b"{}",
                          headers={"Origin": base, "Content-Type": "application/json"})
            with urlopen(req) as response:
                self.assertFalse(json.load(response)["started"])
            self.assertTrue(listening(self.config["port"]))
        self.assertFalse(listening(self.config["port"]))
