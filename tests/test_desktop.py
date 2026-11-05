"""Packaged desktop launch, runtime diagnostics and actual isolated WebKit smoke."""

import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from shell_preview import fixture
from shp_photocraft_tools import launcher as L


class DesktopCommandTests(unittest.TestCase):
    def test_system_python_helper_and_runtime_check(self):
        with patch.object(L.subprocess, "run", return_value=Mock(returncode=0)) as run:
            command = L.desktop_command("http://127.0.0.1:18760/")
        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertTrue(command[1].endswith("desktop/controller.py"))
        self.assertIn("--data-dir", command)
        self.assertTrue(command[command.index("--tray-icon") + 1].endswith("shp-photocraft-tools-tray.svg"))
        self.assertEqual(command[command.index("--url") + 1], "http://127.0.0.1:18760/")
        self.assertEqual(run.call_args.args[0], [*command, "--check"])

    def test_missing_runtime_explains_manual_fallback(self):
        with patch.object(L.subprocess, "run", return_value=Mock(returncode=1, stderr="no WebKit2 typelib")):
            with self.assertRaisesRegex(L.LaunchError, "no WebKit2 typelib.*--browser"):
                L.desktop_command("http://127.0.0.1:18760/")

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
