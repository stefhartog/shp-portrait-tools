"""Synthetic integration checks; never opens a real PhotoCraft or user file."""

import json
import os
from pathlib import Path
import runpy
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from shp_photocraft_tools import editor_handoff as E
from shp_photocraft_tools.shared.photocraft import ControlError


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"port": 1, "token_file": self.root / "control.token",
                       "read_root": self.root, "exchange": self.root / "exchange"}

    def test_paths_preserve_spaces_unicode_and_flag_names_and_validate_all(self):
        paths = []
        for name in ("two words.psd", "-flag.tif", "caf\u00e9.png", 'quote";$name.png'):
            path = self.root / name
            path.write_bytes(b"synthetic")
            paths.append(str(path))
        self.assertEqual(E.input_paths(paths, self.root), paths)
        with self.assertRaises(FileNotFoundError):
            E.input_paths([paths[0], str(self.root / "missing")], self.root)
        (self.root / "link").symlink_to("/etc/hosts")
        with self.assertRaisesRegex(ValueError, "outside"):
            E.input_paths([str(self.root / "link")], self.root)

    def test_real_fake_protocol_multiple_files_ack_errors_and_no_replay(self):
        calls = []
        mode = ["ok"]

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    request = json.loads(line)
                    method = request["method"]
                    calls.append((method, request["params"] if method != "auth" else {}))
                    if method == "auth":
                        reply = {"ok": True}
                    elif method == "app.open":
                        if mode[0] == "drop":
                            return
                        if mode[0] == "reject":
                            reply = {"ok": False, "error": "automation read authority is not configured"}
                        else:
                            reply = {"ok": True, "result": {"path": request["params"]["path"],
                                     "name": "synthetic.psd", "warnings": []}}
                    else:
                        reply = {"ok": True, "result": {}}
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        self.config["token_file"].write_text("synthetic-test-only")
        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                self.config["port"] = server.server_address[1]
                self.assertEqual(E.handoff(["/fake/one space.psd", "/fake/two.tif"], self.config), 2)
                self.assertEqual([method for method, _ in calls], ["auth", "app.open", "app.open", "ui.focus"])
                self.assertEqual(calls[1][1], {"path": "/fake/one space.psd"})
                for failure in ("drop", "reject"):
                    mode[0] = failure
                    calls.clear()
                    with self.assertRaisesRegex(ControlError, "0 of 1 files acknowledged"):
                        E.handoff(["/fake/one.psd"], self.config)
                    self.assertEqual([method for method, _ in calls], ["auth", "app.open"])
            finally:
                server.shutdown()
                thread.join()

    def test_launch_argv_original_grants_and_refuses_occupied_wrong_instance(self):
        with patch.object(E.socket, "create_connection", side_effect=ConnectionRefusedError), \
                patch.object(E.os, "execvpe") as execute, \
                patch.dict(os.environ, {"PHOTOCRAFT_BIN": "/fake/photocraft",
                                        "PHOTOCRAFT_CONTROL_TOKEN": "synthetic-secret",
                                        "PHOTOCRAFT_AUTOMATION_WRITE_ROOT": "/unwanted"}):
            E.launch(["/fake/a space.psd", "/fake/-file.tif"], self.config)
            binary, args, env = execute.call_args.args
            self.assertEqual(binary, "/fake/photocraft")
            self.assertEqual(args, [binary, "--control", "1", "--control-token-file",
                                   str(self.config["token_file"]), "--automation-read-root", str(self.root),
                                   "--automation-write-root", str(self.root / "exchange"),
                                   "--", "/fake/a space.psd", "/fake/-file.tif"])
            self.assertNotIn("PHOTOCRAFT_CONTROL_TOKEN", env)
            self.assertNotIn("PHOTOCRAFT_AUTOMATION_WRITE_ROOT", env)
        with patch.object(E.socket, "create_connection"), \
                patch.object(E, "handoff", side_effect=ControlError("bad authentication")), \
                patch.object(E.os, "execvpe") as execute:
            with self.assertRaisesRegex(ControlError, "bad authentication"):
                E.launch([], self.config)
            execute.assert_not_called()

    def test_invalid_args_and_protocol_failures_are_reported(self):
        with patch.object(E, "report_error") as report:
            self.assertEqual(E.main([]), 1)
            report.assert_called_once()
        path = self.root / "synthetic.psd"
        path.write_bytes(b"not opened")
        with patch.object(E, "detect_config", return_value=self.config), \
                patch.object(E, "handoff", side_effect=ControlError("auth refused")), \
                patch.object(E, "report_error") as report:
            self.assertEqual(E.main(["--", str(path)]), 1)
            self.assertIn("auth refused", report.call_args.args[0])

    def test_install_only_requested_fields_backups_and_refuses_customization(self):
        installer = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/install_editor_handoff.py"))
        executable = self.root / ".local/bin"
        executable.mkdir(parents=True)
        python = executable / "python"
        captured = self.root / "captured-argv.json"
        python.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                          f"Path({str(captured)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        python.chmod(0o755)
        (executable / "photocraft").touch()
        desktop = self.root / ".local/share/applications/ai.storyteller.photocraft.desktop"
        desktop.parent.mkdir(parents=True)
        old_desktop = f'[Desktop Entry]\nName=PhotoCraft\nExec="{executable}/photocraft" %F\nX-Custom=keep\n'
        desktop.write_text(old_desktop)
        settings = self.root / ".config/lightcraft/ui.json"
        settings.parent.mkdir(parents=True)
        old_settings = '{\n  "settings": {"externalEditor": ' + json.dumps(str(executable / "photocraft")) + ', "keep": 42}\n}\n'
        settings.write_text(old_settings)
        backup = self.root / "backup"
        installer["install"](self.root, backup, python)
        self.assertEqual(desktop.read_text(), old_desktop.replace('/photocraft" %F', '/photocraft-dnb" %F'))
        self.assertEqual(settings.read_text(), old_settings.replace('/photocraft"', '/photocraft-lightcraft"'))
        self.assertEqual((backup / desktop.relative_to(self.root)).read_text(), old_desktop)
        self.assertEqual((backup / settings.relative_to(self.root)).read_text(), old_settings)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((executable / "photocraft-lightcraft").stat().st_mode & 0o777, 0o755)
        self.assertIn(' -- "$@"', (executable / "photocraft-lightcraft").read_text())
        filenames = ["two words.psd", "-flag.tif", 'quote";$name.png']
        for wrapper, flags in (("photocraft-lightcraft", ["--"]), ("photocraft-dnb", ["--launch", "--"])):
            subprocess.run([str(executable / wrapper), *filenames], check=True)
            self.assertEqual(json.loads(captured.read_text()),
                             ["-m", "shp_photocraft_tools.editor_handoff", *flags, *filenames])
        with self.assertRaisesRegex(ValueError, "Existing launcher"):
            installer["install"](self.root, self.root / "another-backup", python)
        (executable / "photocraft-lightcraft").unlink()
        (executable / "photocraft-dnb").unlink()
        settings.write_text(old_settings)
        installer["install"](self.root, self.root / "deferred-backup", python, defer_editor=True)
        self.assertEqual(settings.read_text(), old_settings)

    def test_errors_notify_without_shell_or_success_shaped_return(self):
        with patch.object(E.shutil, "which", return_value="/fake/notify-send"), \
                patch.object(E.subprocess, "run") as notify, \
                patch("sys.stderr"):
            notify.return_value.returncode = 0
            E.report_error("authentication refused")
            args = notify.call_args.args[0]
            self.assertEqual(args[:4], ["/fake/notify-send", "--urgency=critical", "--",
                                       "PhotoCraft handoff failed"])
            self.assertIn("authentication refused", args[-1])
            self.assertNotIn("shell", notify.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
