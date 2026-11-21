"""Intentional module stops, restarts and startup preferences; no processes or external services are used."""

import io
import ast
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from shp_photocraft_tools import supervisor as S
from shp_photocraft_tools import launcher as L
from shp_photocraft_tools.launcher import LaunchError
from shp_photocraft_tools.shared.lifecycle import BrowserLifetime
from shp_photocraft_tools.shared.supervision import ControlError


class ModuleIsolationTests(unittest.TestCase):
    def manager(self):
        manager = object.__new__(S.Modules)
        manager.modules = {
            name: S.Module(process=Mock(), instance=name + "-one", phase="running")
            for name in ("dnb",)}
        for module in manager.modules.values():
            module.process.poll.return_value = None
        manager.finished = threading.Event()
        manager.cache_lock = threading.Lock()
        manager.cache = {"external": {}}
        manager.config = {"ports": {"dnb": 19002}}
        manager.preferences_lock = threading.Lock()
        manager.config_path = Path("/unused")
        manager.owner = Mock(return_value={
            "busy": False, "loaded": True, "revision": 7, "stopping": False})
        return manager

    def test_startup_preferences_default_validate_persist_and_fail_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in L.MODEL_FILES:
                (root / filename).write_bytes(b"fixture")
            path = root / "launcher.json"
            original = {"models": str(root), "custom": "keep"}
            path.write_text(json.dumps(original))
            self.assertEqual(L.read_config(path)["startup"], {"dnb": True})
            L.set_startup(path, "dnb", False)
            self.assertEqual(L.read_config(path)["startup"], {"dnb": False})
            self.assertEqual(json.loads(path.read_text())["custom"], "keep")
            before = path.read_bytes()
            with patch.object(L.os, "fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    L.set_startup(path, "dnb", False)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in root.glob("launcher.json.*")), ["launcher.json.lock"])
            for invalid in (False, {"dnb": 1}, {"unknown": True}):
                path.write_text(json.dumps(dict(original, startup=invalid)))
                with self.subTest(invalid=invalid), self.assertRaises(L.LaunchError):
                    L.read_config(path)
            with self.assertRaises(L.LaunchError):
                L.set_startup(path, "crop", False)

    def test_retired_ai_fill_entries_in_existing_config_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in L.MODEL_FILES:
                (root / filename).write_bytes(b"fixture")
            path = root / "launcher.json"
            path.write_text(json.dumps({"models": str(root), "ports": {"crop": 18768, "dnb": 18765},
                                        "startup": {"crop": False, "dnb": True},
                                        "comfy": {"directory": str(root), "port": 18788}}))
            config = L.read_config(path)
            self.assertEqual(config["ports"], {"dnb": 18765})
            self.assertEqual(config["startup"], {"dnb": True})
            self.assertNotIn("comfy", config)

    def test_startup_toggle_changes_only_persisted_preference_not_work(self):
        manager = self.manager()
        skin = manager.modules["dnb"]
        with patch.object(S, "set_startup", return_value={"dnb": False}) as save, \
                patch.object(manager, "stop") as stop, patch.object(manager, "start") as start:
            result = manager.control("dnb", "startup", {"enabled": False})
            save.assert_called_once_with(manager.config_path, "dnb", False)
            self.assertFalse(result["startup_enabled"])
            self.assertEqual(skin.phase, "running")
            stop.assert_not_called()
            start.assert_not_called()
        with patch.object(S, "set_startup", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                manager.control("dnb", "startup", {"enabled": True})
        self.assertFalse(manager.config["startup"]["dnb"])
        handler = self.handler(manager, "/api/modules/dnb/startup", "POST")
        handler.rfile = io.BytesIO(b'{"enabled":true}')
        handler.headers["Content-Length"] = str(len(handler.rfile.getvalue()))
        with patch.object(S, "set_startup", side_effect=OSError("read-only")):
            handler.do_api_POST()
        self.assertEqual(handler.send.call_args.args[0], 503)
        self.assertIn("read-only", json.loads(handler.send.call_args.args[1])["error"])
        self.assertIsNone(skin.error)
        with patch.object(S, "set_startup", return_value={"dnb": True}):
            handler.rfile.seek(0)
            handler.do_api_POST()
        self.assertEqual(handler.send.call_args.args[0], 200)
        self.assertTrue(json.loads(handler.send.call_args.args[1])["startup_enabled"])

    def test_start_all_honors_startup_preference(self):
        for enabled, expected in (({}, ["dnb"]), ({"dnb": True}, ["dnb"]), ({"dnb": False}, [])):
            manager = self.manager()
            manager.config["startup"] = enabled
            with self.subTest(enabled=enabled), patch.object(manager, "control") as control:
                manager.start_all()
                self.assertEqual([call.args[0] for call in control.call_args_list], expected)

    def test_manual_start_does_not_change_startup_preference(self):
        manager = self.manager()
        manager.modules["dnb"].process.poll.return_value = 0
        manager.config["startup"] = {"dnb": False}
        with patch.object(S, "probe", side_effect=LaunchError("test stop")):
            with self.assertRaises(LaunchError):
                manager.start("dnb")
        self.assertFalse(manager.config["startup"]["dnb"])

    def test_native_notifications_are_allocated_above_webview_not_overlaid(self):
        source = Path(S.__file__).parent / "desktop/controller.py"
        tree = ast.parse(source.read_text())
        desktop = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Desktop")
        window = next(node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name == "create_window")
        code = ast.unparse(window)
        self.assertNotIn("Gtk.Overlay", code)
        self.assertNotIn("add_overlay", code)
        self.assertIn("layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)", code)
        self.assertIn("layout.pack_start(self.notice_frame, False, False, 0)", code)
        self.assertIn("layout.pack_start(self.view, True, True, 0)", code)
        self.assertLess(code.index("layout.pack_start(self.notice_frame"), code.index("layout.pack_start(self.view"))
        methods = [node for node in desktop.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("message", "render_modules")]
        namespace = {"GLib": Mock(SOURCE_REMOVE=False), "sys": __import__("sys"), "time": time}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
        host = SimpleNamespace(message_bar=Mock(), notice_frame=Mock(), reload_available=False,
            notice_key=None, cancel_notice_timer=Mock(), expire_notice=Mock(), notice_generation=0,
            quitting=False, module_items={"dnb": Mock()}, modules={"dnb": {"phase": "stopped", "startup_enabled": False}},
            control_pending=False, pending_module=None, dialog_open=False,
            module_actions={"dnb": {"startup": Mock(), "start": Mock(), "stop": Mock(), "restart": Mock()}})
        for text in ("Skin Tools stopped.", "Skin Tools ready.", "Skin Tools error"):
            namespace["message"](host, text)
            host.notice_frame.set_visible.assert_called_with(True)
        namespace["render_modules"](host)
        host.module_actions["dnb"]["startup"].set_active.assert_called_with(False)
        host.module_actions["dnb"]["start"].set_sensitive.assert_called_with(True)

    def test_notice_timers_replace_safely_preserve_recovery_and_cancel_on_destroy(self):
        source = Path(S.__file__).parent / "desktop/controller.py"
        desktop = next(node for node in ast.parse(source.read_text()).body
                       if isinstance(node, ast.ClassDef) and node.name == "Desktop")
        wanted = ("message", "cancel_notice_timer", "expire_notice", "destroy_after_quit")
        methods = [node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        glib = Mock(SOURCE_REMOVE=False)
        glib.timeout_add.side_effect = [11, 12, 13, 14]
        namespace = {"GLib": glib, "time": time, "sys": __import__("sys")}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
        host = SimpleNamespace(message_bar=Mock(), notice_frame=Mock(), reload_available=False,
            notice_key=None, notice_timer=None, notice_generation=0, quitting=False,
            indicator=None, window=Mock(), release=Mock(), quit=Mock())
        for name in wanted:
            setattr(host, name, lambda *args, _name=name, **kwargs: namespace[_name](host, *args, **kwargs))
        with patch("sys.stderr", new_callable=io.StringIO) as log:
            host.message("Crop stopped.")
            generation = host.notice_generation
            glib.timeout_add.assert_called_with(1000, host.expire_notice, generation)
            host.message("Crop error")
            glib.source_remove.assert_called_with(11)
            host.message_bar.reset_mock()
            host.expire_notice(generation)
            host.message_bar.set_text.assert_not_called()
            host.expire_notice(host.notice_generation)
            host.message_bar.set_text.assert_called_once_with("")
            host.notice_frame.set_visible.assert_called_with(False)
            self.assertIsNone(host.notice_timer)
            count = glib.timeout_add.call_count
            host.message("Crop error")
            self.assertEqual(glib.timeout_add.call_count, count, "Do not repeatedly flash the same monitor notice")
            self.assertRegex(log.getvalue(), r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*Crop error")
            host.reload_available = True
            host.message("Reload UI required")
            self.assertIsNone(host.notice_timer)
            host.notice_frame.set_visible.assert_called_with(True)
            host.reload_available = False
            host.message("Shutdown failed; retry", persistent=True)
            self.assertIsNone(host.notice_timer)
            host.message("ordinary info")
            generation = host.notice_generation
            host.destroy_after_quit()
            glib.source_remove.assert_called_with(13)
            host.message_bar.reset_mock()
            host.expire_notice(generation)
            host.message_bar.set_text.assert_not_called()
            host.window.destroy.assert_called_once()
        shutdown = next(node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name == "do_shutdown")
        self.assertIn("self.cancel_notice_timer()", ast.unparse(shutdown))

    def test_stop_keeps_supervisor_alive_then_start_recovers(self):
        manager = self.manager()
        skin = manager.modules["dnb"]
        skin.process.wait.side_effect = lambda **_: setattr(skin.process.poll, "return_value", 0)
        stopped = manager.control("dnb", "stop", {"instance": skin.instance, "confirm": True, "revision": 7})
        self.assertEqual(stopped["phase"], "stopped")
        self.assertFalse(stopped["loaded"])
        self.assertFalse(stopped["busy"])
        manager.owner.assert_called_once_with("dnb", "stop", {"confirm": True, "revision": 7})
        self.assertFalse(manager.finished.is_set())
        def start(name):
            module = manager.modules[name]
            module.phase, module.instance = "running", "dnb-two"
            module.process.poll.return_value = None
        with patch.object(manager, "start", side_effect=start):
            result = manager.control("dnb", "start", {})
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["instance"], "dnb-two")

    def test_late_stop_completion_becomes_unloaded_not_permanently_busy(self):
        manager = self.manager()
        skin = manager.modules["dnb"]
        skin.phase, skin.error = "stopping", "still stopping"
        manager.owner.reset_mock()
        self.assertTrue(manager.snapshot("dnb")["busy"])
        skin.process.poll.return_value = 0
        state = manager.snapshot("dnb")
        self.assertEqual(state["phase"], "stopped")
        self.assertIsNone(state["error"])
        self.assertFalse(state["busy"])
        self.assertFalse(state["loaded"])
        manager.owner.assert_not_called()

    def test_child_failure_stays_visible_and_does_not_break_status(self):
        manager = self.manager()
        manager.owner.side_effect = LaunchError("Skin Tools unavailable")
        state = manager.status()["modules"]
        self.assertEqual(state["dnb"]["phase"], "unavailable")
        self.assertIn("Skin Tools unavailable", state["dnb"]["error"])
        self.assertFalse(manager.finished.is_set())
        manager.modules["dnb"].process.poll.return_value = 1
        self.assertEqual(manager.snapshot("dnb")["phase"], "error")

    def test_busy_or_unconfirmed_stop_is_rejected(self):
        for code in ("busy", "confirmation", "changed"):
            manager = self.manager()
            manager.owner.side_effect = ControlError("Cannot stop", code)
            with self.subTest(code=code), self.assertRaises(ControlError):
                manager.control("dnb", "stop", {"instance": "dnb-one"})
            self.assertEqual(manager.modules["dnb"].phase, "running")
            manager.modules["dnb"].process.wait.assert_not_called()

    def handler(self, manager, path, command="GET"):
        handler = object.__new__(S.ShellHandler)
        handler.server = SimpleNamespace(modules=manager, lifetime=BrowserLifetime(),
            server_port=19000, shutdown_error=None)
        handler.path, handler.command = path, command
        handler.headers = {"Content-Length": "2", "X-SHP-Control": "1"}
        handler.rfile, handler.send = io.BytesIO(b"{}"), Mock()
        return handler

    def test_stopped_routes_are_explicit_and_close_beacons_are_benign(self):
        manager = self.manager()
        manager.modules["dnb"].phase = "stopped"
        for path, method in (("/dnb/", "GET"), ("/dnb/api/run", "POST"),
                             ("/dnb/api/ui/close", "POST"), ("/dnb/api/ui/heartbeat", "POST")):
            handler = self.handler(manager, path, method)
            with self.subTest(path=path), patch.object(S.http.client, "HTTPConnection") as connect:
                handler.proxy()
                code, body, _ = handler.send.call_args.args
                if "/api/ui/" in path:
                    self.assertEqual(code, 200)
                    self.assertFalse(json.loads(body)["managed"])
                else:
                    self.assertEqual(code, 409)
                    self.assertEqual(json.loads(body)["code"], "module_stopped")
                connect.assert_not_called()
        manager.modules["dnb"].phase = "running"
        handler = self.handler(manager, "/dnb/api/job", "POST")
        manager.checked = Mock()
        with patch.object(S.http.client, "HTTPConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status, response.read.return_value = 200, b'{"ok":true}'
            response.getheader.return_value = "application/json"
            handler.proxy()
            self.assertEqual(handler.send.call_args.args[0], 200)

    def test_supervisor_health_and_status_stay_available_with_unloaded_module(self):
        manager = self.manager()
        manager.data = Path("/unused")
        manager.config = {"models": "/unused", "ports": {"dnb": 2}}
        manager.modules["dnb"].phase = "stopped"
        for path in ("/api/health", "/api/status"):
            handler = self.handler(manager, path)
            handler.headers["Host"] = "127.0.0.1:19000"
            handler.do_GET()
            code, body, _ = handler.send.call_args.args
            self.assertEqual(code, 200)
            data = json.loads(body)
            if path.endswith("status"):
                self.assertEqual(data["modules"]["dnb"]["phase"], "stopped")
            else:
                self.assertFalse(data["draining"])

    def test_shutdown_gate_blocks_module_start_before_cleanup_worker_runs(self):
        manager = self.manager()
        handler = self.handler(manager, "/api/modules/dnb/start", "POST")
        handler.server.lifetime.stopping = True
        with patch.object(manager, "control") as control:
            handler.do_api_POST()
            self.assertEqual(handler.send.call_args.args[0], 409)
            self.assertEqual(json.loads(handler.send.call_args.args[1])["code"], "stopping")
            control.assert_not_called()
        manager.finished.set()
        with self.assertRaises(ControlError):
            manager.control("dnb", "start", {})
