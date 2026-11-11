"""Desktop lifecycle checks: temporary roots, fake weights, no inference or browser profile changes."""

import io
import json
import os
import runpy
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from shp_photocraft_tools import launcher as L


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / "external-models"
        self.models.mkdir()
        for filename in L.MODEL_FILES:
            (self.models / filename).write_bytes(b"test fixture, never loaded")
        self.config = self.root / "launcher.json"
        self.ports = {}
        held = []
        for module in L.MODULES:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            held.append(sock)
            self.ports[module] = sock.getsockname()[1]
        for sock in held:
            sock.close()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.shell_port = sock.getsockname()[1]
        self.write_config()
        env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.pids = set()
        self.addCleanup(self.stop_servers)

    def write_config(self, **overrides):
        self.config.write_text(json.dumps(dict(models=str(self.models), ports=self.ports,
                                              shell_port=self.shell_port, **overrides)))

    def stop_servers(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

    def launch(self):
        result = L.launch(self.config, no_browser=True)
        self.pids.update(result["servers"].values())
        return result

    def status(self):
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            with urlopen(f"http://127.0.0.1:{self.shell_port}/api/status", timeout=10) as response:
                status = json.load(response)["modules"]
            if all(s["phase"] != "starting" for s in status.values()):
                return status
            time.sleep(.1)
        self.fail("Modules did not finish starting")

    def test_start_reuse_identity_and_read_only_weights(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.models.iterdir()}
        first = self.launch()
        second = self.launch()
        self.assertEqual(first["servers"], second["servers"])
        status = self.status()
        self.assertTrue(all(s["phase"] == "running" for s in status.values()), status)
        for module, port in self.ports.items():
            with urlopen(f"http://127.0.0.1:{port}/api/health") as response:
                health = json.load(response)
            self.assertEqual(health["module"], module)
            self.assertEqual(health["data"], str(self.root / ".local/share/shp-photocraft-tools" / module))
            self.assertEqual(health["instance"], status[module]["instance"])
            self.assertEqual(health["owner_protocol"], 1)
            self.assertEqual(health["ui_lifecycle"], 0)
            if module == "dnb":
                self.assertEqual(health["models"], str(self.models))
                with urlopen(f"http://127.0.0.1:{port}/api/presets") as response:
                    self.assertEqual(json.load(response), {"default": None, "presets": {}})
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.models.iterdir()})
        self.assertEqual(list((self.root / ".local/share/shp-photocraft-tools").iterdir()), [])

    def test_concurrent_clicks_start_only_one_pair(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.launch(), range(2)))
        self.assertEqual(results[0]["servers"], results[1]["servers"])

    def test_occupied_old_server_refused_and_not_stopped(self):
        class OldHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), OldHandler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                self.ports["crop"] = server.server_port
                self.write_config()
                self.launch()
                status = self.status()
                self.assertEqual(status["crop"]["phase"], "error")
                self.assertIn("occupied", status["crop"]["error"])
                self.assertTrue(thread.is_alive())
            finally:
                server.shutdown()
                thread.join()

    def test_wrong_new_module_or_data_directory_refused(self):
        found = L.identity("crop", self.root / "wrong")
        expected = L.identity("crop", self.root / "correct")
        with self.assertRaisesRegex(L.LaunchError, "data mismatch"):
            L.verify(found, expected, 12345)
        with self.assertRaisesRegex(L.LaunchError, "module mismatch"):
            L.verify(dict(expected, module="dnb"), expected, 12345)

    def test_missing_model_and_invalid_ports_reported(self):
        (self.models / L.MODEL_FILES[0]).unlink()
        with self.assertRaisesRegex(L.LaunchError, "Missing or unreadable"):
            L.read_config(self.config)
        (self.models / L.MODEL_FILES[0]).write_bytes(b"fixture")
        self.ports["crop"] = self.ports["dnb"]
        self.write_config()
        with self.assertRaisesRegex(L.LaunchError, "different ports"):
            L.read_config(self.config)

    def test_chromium_command_has_one_shell_app_window(self):
        with patch.object(L.shutil, "which", return_value="/usr/bin/ungoogled-chromium"):
            command = L.chromium_command({}, ["http://127.0.0.1:18760/"])
            self.assertEqual(command[0], "/usr/bin/ungoogled-chromium")
            self.assertIn("--app=http://127.0.0.1:18760/", command)
            self.assertIn("--class=shp-photocraft-tools", command)
            self.assertTrue(any("shp-photocraft-tools/ungoogled-chromium-profile" in arg for arg in command))
            self.assertNotIn("--new-tab", command)
        with patch.object(L.shutil, "which", return_value=None):
            with self.assertRaisesRegex(L.LaunchError, "not found"):
                L.chromium_command({}, ["http://127.0.0.1:18760/"])

    def test_flatpak_ungoogled_chromium_and_invalid_configuration(self):
        with patch.object(L.shutil, "which", side_effect=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None):
            with patch.object(L.subprocess, "run", return_value=unittest.mock.Mock(returncode=0)):
                command = L.chromium_command({}, ["http://127.0.0.1:18760/"])
                self.assertEqual(command[:3], ["/usr/bin/flatpak", "run", L.CHROMIUM_FLATPAK])
                self.assertTrue(any(f".var/app/{L.CHROMIUM_FLATPAK}/shp-photocraft-tools-profile" in arg for arg in command))
                L.subprocess.run.assert_called_once_with(["/usr/bin/flatpak", "info", L.CHROMIUM_FLATPAK],
                                                         capture_output=True, timeout=10)
            with patch.object(L.subprocess, "run", return_value=unittest.mock.Mock(returncode=1)):
                with self.assertRaisesRegex(L.LaunchError, "not installed"):
                    L.chromium_command({}, ["http://127.0.0.1:18760/"])
        with self.assertRaisesRegex(L.LaunchError, "'ungoogled_chromium'"):
            L.chromium_command({"ungoogled_chromium": []}, ["http://127.0.0.1:18760/"])
        with patch.object(L.shutil, "which", return_value=None):
            with self.assertRaisesRegex(L.LaunchError, "not found"):
                L.chromium_command({"ungoogled_chromium": "/missing/browser"}, ["http://127.0.0.1:18760/"])

    def test_child_startup_failure_points_to_log(self):
        fake = unittest.mock.Mock()
        fake.poll.return_value = 1
        fake.returncode = 1
        logs = self.root / "logs"
        logs.mkdir()
        with patch.object(L.subprocess, "Popen", return_value=fake):
            data = self.root / "data"
            data.mkdir()
            with self.assertRaisesRegex(L.LaunchError, "shell.log"):
                L.ensure_shell(self.config, data, L.read_config(self.config), logs)

    def test_desktop_install_is_distinct_idempotent_and_collision_safe(self):
        repository = Path(__file__).resolve().parent.parent
        desktop = self.root / "Desktop"
        desktop.mkdir()
        original = desktop / "photocraft.desktop"
        original.write_text("existing launcher, must not change")
        command = [str(repository / ".venv/bin/python"), str(repository / "scripts/install_desktop.py"),
                   "--models", str(self.models), "--desktop-dir", str(desktop)]
        env = dict(os.environ, XDG_DATA_HOME=str(self.root / ".local/share"),
                   XDG_CONFIG_HOME=str(self.root / ".config"))
        first = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        paths = json.loads(first.stdout)
        self.assertEqual(Path(paths["launcher"]).parent, self.root / ".local/bin")
        self.assertIn("Name=SHP Portrait Tools", Path(paths["desktop"]).read_text())
        self.assertIn("Terminal=false", Path(paths["desktop"]).read_text())
        self.assertIn("StartupWMClass=shp-photocraft-tools\n", Path(paths["desktop"]).read_text())
        self.assertIn("StartupNotify=true\n", Path(paths["desktop"]).read_text())
        self.assertEqual(Path(paths["desktop"]).read_text(), Path(paths["application"]).read_text())
        self.assertEqual(json.loads(Path(paths["config"]).read_text())["models"], str(self.models))
        self.assertTrue(os.access(paths["launcher"], os.X_OK))
        self.assertEqual(original.read_text(), "existing launcher, must not change")
        second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        Path(paths["launcher"]).write_text("user-edited wrapper")
        third = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(third.returncode, 0)
        self.assertIn("Refusing to overwrite", third.stderr)
        self.assertEqual(Path(paths["launcher"]).read_text(), "user-edited wrapper")

    def test_display_refresh_retains_launch_identity_and_custom_fields(self):
        repository = Path(__file__).resolve().parent.parent
        refresh = runpy.run_path(str(repository / "scripts/install_desktop.py"))["refresh_display_metadata"]
        template = (repository / "desktop/shp-photocraft-tools.desktop.in").read_text()
        old = template.replace("Name=SHP Portrait Tools", "Name=SHP PhotoCraft Tools").replace(
            "Comment=Show the Portrait Tools", "Comment=Show the PhotoCraft Tools")
        old = old.replace("@LAUNCHER@", '"/custom/unchanged/launcher"') + "X-Custom=keep me\n"
        paths = [self.root / "application.desktop", self.root / "shortcut.desktop"]
        for path in paths:
            path.write_text(old)
            path.chmod(0o755)
        refresh(paths)
        for path in paths:
            self.assertEqual(path.read_text(), old.replace("Name=SHP PhotoCraft Tools", "Name=SHP Portrait Tools").replace(
                "Comment=Show the PhotoCraft Tools", "Comment=Show the Portrait Tools"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        expected = paths[0].read_bytes()
        refresh(paths)
        self.assertEqual(paths[0].read_bytes(), expected)
        paths[0].write_text(old)
        paths[1].write_text(old.replace("Name=SHP PhotoCraft Tools", "Name=Custom name"))
        with self.assertRaisesRegex(ValueError, "Custom Name"):
            refresh(paths)
        self.assertEqual(paths[0].read_text(), old)
        self.assertIn("Name=Custom name", paths[1].read_text())
        for invalid, message in (
                (old + "Name=SHP PhotoCraft Tools\n", "Duplicate desktop field"),
                (old.replace("Icon=shp-photocraft-tools", "Icon=custom"), "Not a recognized"),
                (old.replace("Comment=Show", "Comment=Custom Show"), "Custom Comment")):
            paths[1].write_text(invalid)
            with self.assertRaisesRegex(ValueError, message):
                refresh(paths)
            self.assertEqual(paths[0].read_text(), old)
        paths[1].unlink()
        paths[1].symlink_to(self.root / "missing.desktop")
        with self.assertRaisesRegex(ValueError, "symlink"):
            refresh(paths)
        self.assertEqual(paths[0].read_text(), old)

    def test_desktop_trust_failure_reports_manual_action(self):
        script = Path(__file__).resolve().parent.parent / "scripts/install_desktop.py"
        installer = runpy.run_path(str(script))
        arguments = [str(script), "--models", str(self.models),
                     "--desktop-dir", str(self.root / "Desktop")]
        with patch.dict(os.environ, XDG_DATA_HOME=str(self.root / ".local/share"),
                        XDG_CONFIG_HOME=str(self.root / ".config")), \
                patch("sys.argv", arguments), \
                patch("shutil.which", side_effect=lambda name: "/usr/bin/gio" if name == "gio" else None), \
                patch("subprocess.run", return_value=unittest.mock.Mock(
                    returncode=1, stderr="fixture desktop trust failure")), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            installer["main"]()
            self.assertIn("Desktop trust could not be set: fixture desktop trust failure", stderr.getvalue())
            self.assertIn("Allow Launching", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
