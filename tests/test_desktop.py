"""Packaged desktop launch, runtime diagnostics and actual isolated WebKit smoke."""

import json
import ast
from types import SimpleNamespace
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from shell_preview import fixture
from shp_portrait_tools import launcher as L


class DesktopCommandTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Linux Qt desktop host")
    def test_qt_host_command_and_runtime_check(self):
        with patch.object(L.subprocess, "run", return_value=Mock(returncode=0)) as run:
            command = L.desktop_command("http://127.0.0.1:18760/", Path("/unused"))
        self.assertEqual(command[0], sys.executable)  # the project environment, not /usr/bin/python3
        self.assertEqual(command[1:3], ["-m", "shp_portrait_tools.desktop.qt_controller"])
        self.assertEqual(command[command.index("--data-dir") + 1], str(L.data_root() / "webview"))
        self.assertEqual(command[command.index("--url") + 1], "http://127.0.0.1:18760/")
        self.assertEqual(run.call_args.args[0], [*command, "--check"])

    @unittest.skipIf(sys.platform == "win32", "Linux Qt desktop host")
    def test_missing_runtime_explains_manual_fallback(self):
        with patch.object(L.subprocess, "run", return_value=Mock(returncode=1, stderr="No module named 'qtpy'", stdout="")):
            with self.assertRaisesRegex(L.LaunchError, "No module named 'qtpy'.*uv sync.*--no-browser"):
                L.desktop_command("http://127.0.0.1:18760/", Path("/unused"))

    @unittest.skipUnless(sys.platform == "win32", "Windows WebView2 desktop host")
    def test_windows_host_command_and_runtime_check(self):
        data = Path("C:/unused-data")
        with patch.object(L.subprocess, "run", return_value=Mock(returncode=0)) as run:
            command = L.desktop_command("http://127.0.0.1:18760/", data)
        self.assertEqual(command[1:3], ["-m", "shp_portrait_tools.desktop.windows_controller"])
        self.assertEqual(command[command.index("--url") + 1], "http://127.0.0.1:18760/")
        self.assertEqual(command[command.index("--data-dir") + 1], str(data / "webview"))
        self.assertEqual(run.call_args.args[0], [*command, "--check"])
        with patch.object(L.subprocess, "run", return_value=Mock(returncode=1, stderr="no WebView2", stdout="")):
            with self.assertRaisesRegex(L.LaunchError, "no WebView2.*--no-browser"):
                L.desktop_command("http://127.0.0.1:18760/", data)

    @unittest.skipUnless(os.environ.get("SHP_DESKTOP_SMOKE") == "1", "opt-in real Wayland window smoke")
    def test_real_webkit_single_instance_hide_quit_and_independent_browser_lease(self):
        helper = Path(L.__file__).parent / "desktop/controller.py"
        with fixture() as (server, root):
            base = f"http://127.0.0.1:{server.server_port}/"
            # Represents an ordinary browser retaining its independent lease when desktop quits.
            server.lifetime.touch("other-browser")
            command = ["/usr/bin/python3", str(Path(__file__).with_name("desktop_smoke.py")),
                       str(helper), base, str(root)]
            result = subprocess.run(command, env=dict(os.environ, WAYLAND_DEBUG="client"),
                                    capture_output=True, text=True, timeout=115)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if os.environ.get("WAYLAND_DISPLAY"):
                app_ids = set(re.findall(r'set_app_id\("([^"]+)"\)', result.stderr))
                self.assertEqual(app_ids, {"shp-photocraft-tools"}, "Wayland windows must match the pinned desktop basename")
            output = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertFalse(output["failures"])
            self.assertGreaterEqual(len(output["steps"]), 8)
            print("Actual WebKit:", "; ".join(output["steps"]), flush=True)
            restarted = subprocess.run([*command, "storage-only"], capture_output=True, text=True, timeout=30)
            self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)
            self.assertIn("persisted across process restart", restarted.stdout)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with server.lifetime.lock:
                    clients = set(server.lifetime.clients)
                if clients == {"other-browser"}:
                    break
                time.sleep(.1)
            self.assertEqual(clients, {"other-browser"}, "Native desktop did not release its own lease cleanly")
            self.assertFalse(server.lifetime.should_stop(server.modules.busy))
            self.assertTrue(all(s["phase"] == "running" for s in server.modules.status()["modules"].values()))
            server.lifetime.close("other-browser")
            server.lifetime.grace = .1
            server.lifetime.deadline = time.monotonic() + .1
            self.assertTrue(server.finished.wait(5), "Final independent browser lease did not stop supervisor")
