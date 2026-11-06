"""Deterministic native quit/restart tests: no GUI, real signals or user services."""

import ast
import io
import json
from pathlib import Path
import signal
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from shp_photocraft_tools.shared.lifecycle import BrowserLifetime
from shp_photocraft_tools.shared.supervision import ControlError
from shp_photocraft_tools.shared import owned_process as O
from shp_photocraft_tools import launcher as L, supervisor as S


class NativeLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.life = BrowserLifetime(clock=lambda: self.now)
        self.life.touch("desktop-one")

    def test_quit_admission_is_immediate_and_cannot_be_revived(self):
        result = self.life.native_quit("desktop-one", lambda: False)
        self.assertEqual(result, {"draining": True, "others": 0})
        self.assertFalse(self.life.reserve())
        self.assertFalse(self.life.touch("desktop-one"))
        with self.assertRaises(RuntimeError):
            with self.life.work():
                self.fail("admitted while draining")
        with self.assertRaisesRegex(ControlError, "already draining"):
            self.life.native_quit("desktop-one", lambda: False)

    def test_browser_grace_and_other_windows_remain_protected(self):
        self.life.close("desktop-one")
        self.now = 14
        self.assertFalse(self.life.should_stop(lambda: False))
        self.now = 16
        self.assertTrue(self.life.should_stop(lambda: False))
        self.life = BrowserLifetime(clock=lambda: self.now)
        self.life.touch("desktop-one")
        self.life.touch("browser")
        with self.assertRaisesRegex(ControlError, "other Tools"):
            self.life.native_quit("desktop-one", lambda: False, restart=True)
        self.assertIn("desktop-one", self.life.clients)
        self.assertEqual(self.life.native_quit("desktop-one", lambda: False),
                         {"draining": False, "others": 1})
        self.assertTrue(self.life.touch("browser"))

    def test_busy_expired_and_force_admission(self):
        with self.assertRaisesRegex(ControlError, "busy"):
            self.life.native_quit("desktop-one", lambda: True)
        with self.life.work():
            with self.assertRaisesRegex(ControlError, "busy"):
                self.life.native_quit("desktop-one", lambda: False)
        self.life.touch("browser")
        with self.life.work():
            result = self.life.native_quit("desktop-one", lambda: True, force=True, restart=True)
        self.assertTrue(result["draining"])
        self.assertEqual(result["others"], 1)
        expired = BrowserLifetime(clock=lambda: self.now)
        expired.touch("desktop-one")
        self.now = 91
        with self.assertRaisesRegex(ControlError, "expired"):
            expired.native_quit("desktop-one", lambda: False)

    def test_heartbeat_and_admission_are_serialized(self):
        entered, release = threading.Event(), threading.Event()
        def busy():
            entered.set()
            release.wait(1)
            return False
        worker = threading.Thread(target=lambda: self.life.native_quit("desktop-one", busy))
        worker.start()
        self.assertTrue(entered.wait(1))
        result = []
        heartbeat = threading.Thread(target=lambda: result.append(self.life.touch("desktop-one")))
        heartbeat.start()
        release.set()
        worker.join(1)
        heartbeat.join(1)
        self.assertEqual(result, [False])


class EscalationTests(unittest.TestCase):
    def test_identity_change_or_non_session_leader_refuses_signals(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch.object(O, "process_identity", return_value=(123, 99)), \
                patch.object(O.os, "pidfd_open") as open_fd:
            with self.assertRaisesRegex(RuntimeError, "identity"):
                O.pin_session(process, (123, 98))
            open_fd.assert_not_called()
        with patch.object(O, "process_identity", return_value=(10, 99)):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                O.pin_session(process, (10, 99))

    def test_pins_only_owned_session_and_rechecks_start_ticks(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        entries = [SimpleNamespace(name=str(pid)) for pid in (123, 124, 999)]
        identities = {123: (123, 5), 124: (123, 6), 999: (999, 7)}
        with patch.object(O.Path, "iterdir", return_value=entries), \
                patch.object(O, "process_identity", side_effect=lambda pid: identities[pid]), \
                patch.object(O.os, "pidfd_open", side_effect=lambda pid: pid+1000) as opened:
            self.assertEqual(O.pin_session(process, (123, 5)), [1123, 1124])
            self.assertEqual([call.args[0] for call in opened.call_args_list], [123, 124])
        with patch.object(O.Path, "iterdir", return_value=entries[:1]), \
                patch.object(O, "process_identity", side_effect=[(123, 5), (123, 5), (123, 8)]), \
                patch.object(O.os, "pidfd_open", return_value=1123), patch.object(O.os, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "changed while pinning"):
                O.pin_session(process, (123, 5))
            close.assert_called_once_with(1123)

    def test_dead_session_leader_with_surviving_members_fails_closed(self):
        process = Mock(pid=123)
        process.poll.return_value = 1
        with patch.object(O.Path, "iterdir", return_value=[SimpleNamespace(name="124")]), \
                patch.object(O, "process_identity", return_value=(123, 8)), \
                patch.object(O.signal, "pidfd_send_signal") as send:
            with self.assertRaisesRegex(RuntimeError, "session still has processes"):
                O.pin_session(process, (123, 7))
            send.assert_not_called()

    def test_new_session_member_after_pinning_prevents_restart_success(self):
        process = Mock(pid=123)
        modules = SimpleNamespace(modules={"crop": SimpleNamespace(process=process, process_identity=(123, 7))},
                                  comfy=None, owner=Mock())
        with patch.object(S, "pin_session", return_value=[321]), \
                patch.object(S, "drain_pinned"), \
                patch.object(O.Path, "iterdir", return_value=[SimpleNamespace(name="125")]), \
                patch.object(O, "process_identity", return_value=(123, 8)):
            with self.assertRaisesRegex(RuntimeError, "session still has processes"):
                S.Modules.force_close(modules)

    def test_grace_term_kill_bounded_failure_and_descriptor_cleanup(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(O.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(O.time, "sleep", side_effect=sleep), \
                patch.object(O.select, "select", return_value=([], [], [])), \
                patch.object(O.signal, "pidfd_send_signal") as send, patch.object(O.os, "close") as close:
            graceful = Mock()
            with self.assertRaisesRegex(RuntimeError, "bounded escalation"):
                O.drain_pinned([100, 101], graceful)
            graceful.assert_called_once()
            self.assertEqual([call.args for call in send.call_args_list],
                             [(100, signal.SIGTERM), (101, signal.SIGTERM),
                              (100, signal.SIGKILL), (101, signal.SIGKILL)])
            self.assertLess(clock[0], 8.3)
            self.assertEqual(close.call_count, 2)
        with patch.object(O.select, "select", side_effect=lambda fds, *_: (fds, [], [])), \
                patch.object(O.signal, "pidfd_send_signal") as send, patch.object(O.os, "close") as close:
            O.drain_pinned([100], Mock())
            send.assert_not_called()
            close.assert_called_once_with(100)


class RestartTests(unittest.TestCase):
    def test_native_endpoint_checks_work_and_starts_one_drain_after_ack(self):
        life = BrowserLifetime()
        life.touch("desktop-one")
        modules = Mock()
        modules.status.return_value = {"modules": {"crop": {"instance": "c", "revision": 3, "loaded": True}}}
        modules.busy.return_value = False
        server = SimpleNamespace(lifetime=life, modules=modules, force_shutdown=False, shutdown_lock=threading.Lock(),
                                 shutdown_error=None, drain_thread=None, drain_client=None, native_drain=Mock())
        events = []
        def request(expected, confirm=True, force=False):
            payload = json.dumps({"client": "desktop-one", "confirm": confirm, "expected": expected,
                                  "force": force, "restart": force}).encode()
            handler = SimpleNamespace(server=server, path="/api/ui/native-quit", valid_host=lambda: True,
                origin="http://127.0.0.1:1234", headers={"Origin": "http://127.0.0.1:1234",
                    "Content-Type": "application/json", "Content-Length": str(len(payload)), "X-SHP-Control": "1"},
                rfile=io.BytesIO(payload), send=Mock(side_effect=lambda code, *_: events.append(code)), fail=Mock())
            S.ShellHandler.do_POST(handler)
            return handler
        with patch.object(S.threading, "Thread") as thread, \
                patch.object(S, "process_identity", return_value=(999, 44)):
            unconfirmed = request({}, confirm=False)
            self.assertEqual(unconfirmed.send.call_args.args[0], 400)
            self.assertFalse(life.stopping)
            invalid = request({})
            self.assertEqual(invalid.fail.call_args.args[0].code, "changed")
            self.assertFalse(life.stopping)
            thread.assert_not_called()
            handler = request({"crop": ["c", 3, True]})
            self.assertTrue(life.stopping)
            self.assertEqual(events[-1], 200)
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            handler = request({"crop": ["c", 3, True]})
            self.assertEqual(handler.fail.call_args.args[0].code, "stopping")
            self.assertEqual(thread.call_count, 1)
            server.shutdown_error = "normal shutdown timed out"
            server.drain_thread.is_alive.return_value = False
            handler = request({}, force=True)
            self.assertEqual(handler.send.call_args.args[0], 200)
            self.assertTrue(server.force_shutdown)
            self.assertIsNone(server.shutdown_error)
            self.assertEqual(thread.call_count, 2)

    def test_launcher_waits_for_draining_identity_before_starting_anything(self):
        config = {"shell_port": 1234, "ports": {"crop": 1235, "dnb": 1236}, "models": "/models"}
        found = {"pid": 99, "draining": True, "process_identity": [99, 7],
                 "shell_protocol": 1, "ports": config["ports"]}
        with patch.object(L, "probe", side_effect=[found, L.LaunchError("stop after wait")]), \
                patch.object(L, "verify"), patch.object(L, "wait_for_exit") as wait, \
                patch.object(O, "process_identity", return_value=(99, 7)), \
                patch.object(L.subprocess, "Popen") as spawn, patch.object(L, "build_opener") as opener:
            with self.assertRaisesRegex(L.LaunchError, "stop after wait"):
                L.ensure_shell(Path("/config"), Path("/data"), config, Path("/logs"))
            wait.assert_called_once_with(99, 7)
            opener.assert_not_called()
            spawn.assert_not_called()

    def test_reserve_race_waits_then_starts_fresh_supervisor(self):
        config = {"shell_port": 1234, "ports": {"crop": 1235, "dnb": 1236}, "models": "/models"}
        old = {"pid": 99, "process_identity": [99, 7], "shell_protocol": 1, "ports": config["ports"]}
        fresh = {**old, "pid": 100, "instance": "fresh"}
        process = Mock(pid=100)
        process.poll.return_value = None
        order = []
        with patch.object(L, "probe", side_effect=[old, None, fresh]), patch.object(L, "verify"), \
                patch.object(L, "wait_for_exit", side_effect=lambda *_: order.append("wait")), \
                patch.object(O, "process_identity") as identity, \
                patch.object(L, "build_opener") as opener, \
                patch.object(L.uuid, "uuid4", return_value=SimpleNamespace(hex="fresh")), \
                patch.object(Path, "open", return_value=io.BytesIO()), \
                patch.object(L.threading, "Thread"), \
                patch.object(L.subprocess, "Popen", side_effect=lambda *a, **k: order.append("spawn") or process):
            opener.return_value.open.side_effect = HTTPError("http://local", 409, "draining", {}, None)
            self.assertEqual(L.ensure_shell(Path("/config"), Path("/data"), config, Path("/logs")), fresh)
            self.assertEqual(order, ["wait", "spawn"])
            identity.assert_not_called()

    def test_late_busy_normal_cleanup_times_out_without_force(self):
        modules = SimpleNamespace(finished=threading.Event(), comfy=None,
            modules={"crop": SimpleNamespace(lock=threading.Lock(), process=None)},
            stop=Mock(side_effect=ControlError("busy", "busy")))
        with patch.object(S.time, "monotonic", side_effect=[0, 31]), \
                patch.object(S, "pin_session") as pin:
            with self.assertRaisesRegex(RuntimeError, "normal shutdown did not force-stop"):
                S.Modules.close(modules, strict=True)
            pin.assert_not_called()
            self.assertTrue(modules.finished.is_set())

    def test_partial_force_failure_is_reported_without_relaunch_or_unrelated_targets(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        modules = SimpleNamespace(modules={"crop": SimpleNamespace(process=process, process_identity=(123, 7))},
                                  comfy=None, owner=Mock())
        with patch.object(S, "pin_session", return_value=[321]) as pin, \
                patch.object(S, "drain_pinned", side_effect=RuntimeError("not exited")) as drain:
            with self.assertRaisesRegex(RuntimeError, "crop: not exited"):
                S.Modules.force_close(modules)
            pin.assert_called_once_with(process, (123, 7))
            drain.assert_called_once()
            process.wait.assert_not_called()

    def test_wait_requires_exact_old_process_exit_not_pid_reuse(self):
        with patch.object(O, "process_identity", side_effect=[(20, 7), (20, 8)]), \
                patch.object(L.time, "sleep") as sleep:
            L.wait_for_exit(20, 7)
            sleep.assert_called_once()
        with patch.object(O, "process_identity", return_value=(20, 7)), \
                patch.object(L.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaisesRegex(L.LaunchError, "no duplicate"):
                L.wait_for_exit(20, 7)

    def test_native_cleanup_failure_does_not_report_success_or_release_port(self):
        server = SimpleNamespace(modules=Mock(), force_shutdown=True, native_cleaned=False,
                                 shutdown_error=None, shutdown=Mock())
        server.modules.close.side_effect = RuntimeError("identity changed")
        S.ShellServer.native_drain(server)
        self.assertIn("identity changed", server.shutdown_error)
        self.assertFalse(server.native_cleaned)
        server.shutdown.assert_not_called()

    def test_native_controller_pending_failure_and_restart_handoff(self):
        source = Path(S.__file__).parent / "desktop/controller.py"
        tree = ast.parse(source.read_text())
        desktop = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Desktop")
        methods = [node for node in desktop.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("native_shutdown", "request_quit")]
        glib = Mock()
        namespace = {"GLib": glib, "Path": Path, "os": SimpleNamespace(getpid=lambda: 123),
                     "subprocess": Mock(), "threading": Mock()}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
        host = SimpleNamespace(quitting=False, dialog_open=True, control_pending=False, message=Mock())
        namespace["request_quit"](host)
        self.assertIn("pending", host.message.call_args.args[0])
        connection = SimpleNamespace(lease_lock=threading.Lock(), stopped=threading.Event(),
                                     client="desktop-test", request=Mock())
        connection.request.return_value = {"draining": True, "pid": 555, "process_identity": [555, 77]}
        host = SimpleNamespace(pending_action="restart", connection=connection, quit_signature={},
            restart_command=["python", "-m", "shp_photocraft_tools.launcher"], data=Path("/fake"),
            destroy_after_quit=Mock(), shutdown_failed=Mock(), wait_for_shutdown=Mock(), message=Mock())
        with patch.object(Path, "read_text", return_value="123 (name) "+" ".join(["0"]*19+["88"])), \
                patch.object(Path, "open", return_value=io.BytesIO()):
            namespace["native_shutdown"](host)
        command = namespace["subprocess"].Popen.call_args.args[0]
        self.assertEqual(command[-6:], ["--wait-for-exit", "555", "77", "--wait-for-exit", "123", "88"])
        self.assertTrue(connection.stopped.is_set())
        glib.idle_add.assert_called_with(host.destroy_after_quit)
        connection.request.side_effect = ValueError("other windows")
        namespace["native_shutdown"](host)
        self.assertEqual(glib.idle_add.call_args.args[0], host.shutdown_failed)

    def test_controller_wait_surfaces_cleanup_failure_and_closes_pid_descriptor(self):
        source = Path(S.__file__).parent / "desktop/controller.py"
        desktop = next(node for node in ast.parse(source.read_text()).body
                       if isinstance(node, ast.ClassDef) and node.name == "Desktop")
        method = next(node for node in desktop.body if isinstance(node, ast.FunctionDef)
                      and node.name == "wait_for_shutdown")
        os = SimpleNamespace(pidfd_open=Mock(return_value=10), close=Mock())
        namespace = {"Path": Path, "os": os, "select": Mock(), "time": Mock()}
        namespace["select"].select.return_value = ([], [], [])
        namespace["time"].monotonic.return_value = 0
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        host = SimpleNamespace(connection=Mock())
        host.connection.request.return_value = {"shutdown_error": "identity changed"}
        with patch.object(Path, "read_text", return_value="123 (name) "+" ".join(["0"]*19+["88"])):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                namespace["wait_for_shutdown"](host, {"pid": 123, "process_identity": [123, 88]})
        os.close.assert_called_once_with(10)
        os.close.reset_mock()
        with patch.object(Path, "read_text", return_value="123 (name) "+" ".join(["0"]*19+["99"])):
            namespace["wait_for_shutdown"](host, {"pid": 123, "process_identity": [123, 88]})
        os.close.assert_called_once_with(10)
        self.assertEqual(host.connection.request.call_count, 1, "Must not poll a reused PID's server")
        host.connection.request.return_value = {}
        namespace["time"].monotonic.side_effect = [0, 91]
        with patch.object(Path, "read_text", return_value="123 (name) "+" ".join(["0"]*19+["88"])):
            with self.assertRaisesRegex(ValueError, "no duplicate restart"):
                namespace["wait_for_shutdown"](host, {"pid": 123, "process_identity": [123, 88]})
