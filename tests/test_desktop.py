"""Packaged desktop launch command and runtime diagnostics."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

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

