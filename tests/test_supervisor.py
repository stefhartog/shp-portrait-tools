"""Owned-process controls, proxy routing, origin checks and shell-scoped lifetime."""

import http.client
import json
import os
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch
from pathlib import Path

from shell_preview import fixture
from shp_portrait_tools.launcher import LaunchError
from shp_portrait_tools.shared.lifecycle import BrowserLifetime
from shp_portrait_tools.shared.supervision import ControlError, OwnerControl
from shp_portrait_tools.supervisor import SkinWorker, external_status
from shp_portrait_tools.shared.photocraft.bridge import detect_config


class GateTests(unittest.TestCase):
    def test_saved_photocraft_config_reaches_child_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pc = {"port": 17878, "token_file": str(root / "private" / "control.token"),
                  "exchange": str(root / "private" / "exchange"), "read_root": str(root / "private" / "exchange"),
                  "executable": str(root / "photocraft.exe"), "editor_config": str(root / "private" / "editor")}
            config = {"photocraft": pc, "models": str(root), "ports": {"skin": 18002}}
            clean_env = {key: value for key, value in os.environ.items()
                         if not key.startswith(("SHP_PC_", "DNB_PC_", "SHPC_PC_", "PHOTOCRAFT_"))}
            children = []
            def spawn(*args, **kwargs):
                children.append(kwargs["env"])
                return Mock(pid=1234, poll=Mock(return_value=None))
            with patch.dict(os.environ, clean_env, clear=True), \
                 patch("shp_portrait_tools.supervisor.probe", return_value=None), \
                 patch("shp_portrait_tools.supervisor.process_identity", return_value=(1234, 1)), \
                 patch("shp_portrait_tools.supervisor.adopt", return_value=(1234, 1)), \
                 patch("shp_portrait_tools.supervisor.subprocess.Popen", side_effect=spawn), \
                 patch.object(SkinWorker, "checked"), \
                 patch("shp_portrait_tools.supervisor.socket.create_connection") as connect:
                worker = SkinWorker(config, root / "data", root / "logs", "test")
                worker.start()
                self.assertEqual(len(children), 1)
                for child, prefix, module in zip(children, ("DNB",), ("dnb",)):
                    actual = detect_config(prefix, module, child)
                    self.assertEqual(actual["port"], pc["port"])
                    for key in ("token_file", "exchange", "read_root"):
                        self.assertEqual(actual[key], Path(pc[key]))
                    self.assertEqual(child["PHOTOCRAFT_BIN"], pc["executable"])
                    self.assertEqual(child["PHOTOCRAFT_CONFIG_DIR"], pc["editor_config"])
                status = external_status(environ=children[0])
                self.assertEqual(status["photocraft_skin"]["address"], "127.0.0.1:17878")
                self.assertEqual(connect.call_count, 1)

    def test_explicit_photocraft_environment_overrides_saved_config(self):
        from shp_portrait_tools.supervisor import photocraft_environment
        pc = dict(port=7878, token_file="/saved/token", exchange="/saved/exchange",
                  read_root="/saved/exchange", executable="/saved/photocraft", editor_config="/saved/editor")
        env = photocraft_environment({"photocraft": pc}, {"SHP_PC_PORT": "9000", "DNB_PC_PORT": "9001"})
        self.assertEqual(detect_config("DNB", "dnb", {k: v for k, v in env.items() if k != "DNB_PC_PORT"})["port"], 9000)
        self.assertEqual(detect_config("DNB", "dnb", env)["port"], 9001)
        self.assertEqual(env["SHP_PC_TOKEN_FILE"], pc["token_file"])

    def test_photocraft_endpoint_is_probed_once(self):
        with patch.dict(os.environ, {"DNB_PC_PORT": "7878"}):
            with patch("shp_portrait_tools.supervisor.socket.create_connection") as connect:
                status = external_status()
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(status, {"photocraft_skin": {"reachable": True, "address": "127.0.0.1:7878"}})
            with patch("shp_portrait_tools.supervisor.socket.create_connection", side_effect=OSError("offline")) as connect:
                status = external_status()
            self.assertEqual(connect.call_count, 1)
            self.assertFalse(status["photocraft_skin"]["reachable"])

    def test_bad_external_configuration_is_reported(self):
        with patch.dict(os.environ, {"DNB_PC_PORT": "invalid"}):
            status = external_status()
        self.assertFalse(status["photocraft_skin"]["reachable"])
        self.assertIn("error", status["photocraft_skin"])

    def test_busy_loaded_confirmation_revision_and_atomic_stop(self):
        busy, loaded = [False], [True]
        gate = OwnerControl("secret", busy=lambda: busy[0], loaded=lambda: loaded[0])
        with self.assertRaises(ControlError) as error:
            gate.prepare_stop()
        self.assertEqual(error.exception.code, "confirmation")
        revision = gate.snapshot()["revision"]
        with gate.work("/api/load"):
            with self.assertRaises(ControlError) as error:
                gate.prepare_stop(confirm=True)
            self.assertEqual(error.exception.code, "busy")
        with self.assertRaises(ControlError) as error:
            gate.prepare_stop(confirm=True, revision=revision)
        self.assertEqual(error.exception.code, "changed")
        busy[0] = True
        with self.assertRaises(ControlError):
            gate.prepare_stop(confirm=True)
        busy[0] = False
        gate.prepare_stop(confirm=True, revision=gate.snapshot()["revision"])
        with self.assertRaises(ControlError):
            with gate.work("/api/run"):
                self.fail("admitted work after stop")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.context = fixture()
        self.server, self.root = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.manager = self.server.worker
        self.wait_ready()

    def request(self, path, body=None, headers=None):
        if body is not None:
            headers = dict({"Origin": self.base, "Content-Type": "application/json", "X-SHP-Control": "1"},
                           **(headers or {}))
        req = Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers or {})
        with urlopen(req, timeout=15) as response:
            data = response.read()
            return json.loads(data) if response.headers["Content-Type"] == "application/json" else data

    def wait_ready(self):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = self.request("/api/status")["modules"]
            if all(state["phase"] == "running" for state in status.values()):
                return status
            if any(state["phase"] == "error" for state in status.values()):
                self.fail(str(status))
            time.sleep(.1)
        self.fail("Fixture module startup timed out")

    def assert_error(self, code, path, body=None, headers=None):
        with self.assertRaises(HTTPError) as raised:
            self.request(path, body, headers)
        self.assertEqual(raised.exception.code, code)
        return json.load(raised.exception)

    def worker(self, path, body=None):
        """A request straight to the Skin Tools worker, as the page in the window or a browser tab makes it."""
        origin = f"http://127.0.0.1:{self.manager.port}"
        headers = {"Origin": origin, "Content-Type": "application/json"} if body is not None else {}
        req = Request(origin + path, json.dumps(body).encode() if body is not None else None, headers)
        with urlopen(req, timeout=15) as response:
            data = response.read()
            return json.loads(data) if response.headers["Content-Type"] == "application/json" else data

    def control(self, name, action, **kwargs):
        return self.request(f"/api/modules/{name}/{action}",
                            dict(instance=self.manager.worker_instance, **kwargs))

    def test_status_page_direct_worker_page_and_no_proxy(self):
        status_page = self.request("/")
        self.assertIn(b"SHP Portrait Tools", status_page)
        self.assertIn(json.dumps(self.manager.page_url()).encode(), status_page)
        for path in ("/static/shell.js", "/static/shell.css", "/skin/", "/skin/api/health", "/crop/",
                     "/http://example.test/"):
            self.assert_error(404, path)
        page = self.worker("/")
        self.assertIn(b'href="static/shp-ui.css"', page)
        self.assertIn(b'id="viewTabs"', page)
        self.assertEqual(self.worker("/api/health")["module"], "skin")
        with self.assertRaises(HTTPError) as raised:
            self.worker("/api/owner/status", {})
        self.assertEqual(raised.exception.code, 403)

    def test_browser_tab_on_the_worker_keeps_the_supervisor_alive(self):
        self.assertFalse(self.manager.keep_alive())
        accepted = self.worker("/api/ui/heartbeat", {"client": "desktop-tab-probe"})
        self.assertTrue(accepted["managed"] and accepted["accepted"])
        self.assertEqual(self.manager.snapshot()["ui_clients"], 1)
        self.assertTrue(self.manager.keep_alive())
        self.worker("/api/ui/close", {"client": "desktop-tab-probe"})
        self.assertFalse(self.manager.keep_alive())

    def test_stop_restart_loaded_guard(self):
        self.worker("/api/pc/fetch", {})
        before = self.wait_ready()
        error = self.assert_error(409, "/api/modules/skin/restart", {"instance": before["skin"]["instance"]})
        self.assertEqual(error["code"], "confirmation")
        self.assertEqual(self.wait_ready()["skin"]["instance"], before["skin"]["instance"])
        # Another window changes work while the first confirmation is open.
        self.worker("/api/pc/fetch", {})
        changed = self.assert_error(409, "/api/modules/skin/restart",
                                    {"instance": before["skin"]["instance"], "confirm": True,
                                     "revision": error["revision"]})
        self.assertEqual(changed["code"], "changed")
        restarted = self.control("skin", "restart", confirm=True, revision=changed["revision"])
        self.assertNotEqual(restarted["instance"], before["skin"]["instance"])
        self.assertFalse(restarted["loaded"])
        self.assertEqual(self.control("skin", "stop")["phase"], "stopped")
        with self.assertRaises(OSError):
            self.worker("/api/health")
        self.assertIn(b"SHP Portrait Tools", self.request("/"))  # the status page stays available
        self.assertEqual(self.control("skin", "start")["phase"], "running")

    def test_hue_center_direct_worker_route(self):
        loaded=self.worker("/api/pc/fetch",{})
        body={"generation":loaded["generation"],"S":loaded["S"],"mode":"frequency",
              "tone":{"on":False},"color":{"on":False},"structure":{"protect":0}}
        first=self.worker("/api/hue-center",body)
        self.assertIsInstance(first["center"],float)
        connection=http.client.HTTPConnection("127.0.0.1",self.manager.port)
        try:
            connection.request("POST","/api/hue-center",json.dumps(body),{"Content-Type":"application/json"})
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read()),first)
        finally:
            connection.close()
        self.worker("/api/test/hue-route",{"available":False})
        with self.assertRaises(HTTPError) as raised:
            self.worker("/api/hue-center",body)
        self.assertEqual(raised.exception.code,404)

    def test_busy_api_and_background_job_cannot_be_forced(self):
        with ThreadPoolExecutor() as pool:
            work = pool.submit(self.worker, "/api/test/work", {})
            deadline = time.monotonic() + 2
            while not self.manager.snapshot()["busy"] and time.monotonic() < deadline:
                time.sleep(.02)
            result = self.assert_error(409, "/api/modules/skin/stop",
                                      {"instance": self.manager.worker_instance, "confirm": True})
            self.assertEqual(result["code"], "busy")
            self.assertTrue(work.result()["done"])

    def test_identity_change_and_missing_owner_capability_refused(self):
        worker = self.manager
        instance = worker.worker_instance
        worker.worker_instance = "not-the-owned-instance"
        try:
            with self.assertRaisesRegex(LaunchError, "ownership changed"):
                worker.stop(True)
            self.assertIsNone(worker.process.poll())
        finally:
            worker.worker_instance = instance
        connection = http.client.HTTPConnection("127.0.0.1", self.manager.config["ports"]["skin"])
        try:
            connection.request("POST", "/api/owner/stop", "{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()

    def test_origin_host_payload_and_fixed_action_checks(self):
        for headers in ({"Origin": "https://evil.test"}, {"Origin": "null"},
                        {"Content-Type": "text/plain"}, {"X-SHP-Control": "0"}):
            self.assert_error(403, "/api/modules/skin/stop", {}, headers)
        self.assert_error(403, "/api/status", headers={"Host": "rebound.example.test"})
        self.assert_error(400, "/api/modules/skin/exec", {})
        self.assert_error(400, "/api/modules/crop/stop", {})
        self.assert_error(400, "/api/modules/skin/stop", {"confirm": "yes"})
        self.assert_error(400, "/api/modules/skin/stop", {"revision": True})
        self.assert_error(409, "/api/modules/skin/stop", {"instance": "stale-window"})

    def test_last_shell_refresh_multiwindow_and_busy_completion_shutdown(self):
        life = self.server.lifetime = BrowserLifetime(grace=.1, startup=.1, lease=90)
        life.touch("first")
        life.touch("second")
        life.close("first")
        time.sleep(.2)
        self.assertFalse(life.should_stop(self.manager.busy))
        life.close("second")
        life.touch("refreshed")
        self.assertFalse(life.should_stop(self.manager.busy))
        with ThreadPoolExecutor() as pool:
            work = pool.submit(self.worker, "/api/test/work", {})
            deadline = time.monotonic() + 2
            while not self.manager.snapshot()["busy"] and time.monotonic() < deadline:
                time.sleep(.02)
            life.close("refreshed")
            time.sleep(.2)
            self.assertFalse(life.should_stop(self.manager.busy))
            self.assertTrue(work.result()["done"])
        self.server.finished.wait(5)
        self.assertTrue(self.server.finished.is_set())
        self.manager.close()
        self.assertEqual(self.manager.process.poll(), 0)


if __name__ == "__main__":
    unittest.main()
