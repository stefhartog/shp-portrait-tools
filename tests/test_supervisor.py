"""Owned-process controls, proxy routing, origin checks and shell-scoped lifetime."""

import http.client
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from shell_preview import fixture
from shp_photocraft_tools.launcher import LaunchError
from shp_photocraft_tools.shared.lifecycle import BrowserLifetime
from shp_photocraft_tools.shared.supervision import ControlError, OwnerControl
from shp_photocraft_tools.supervisor import external_status


class GateTests(unittest.TestCase):
    def test_shared_photocraft_endpoint_is_probed_once(self):
        with patch.dict(os.environ, {"SHPC_PC_PORT": "7878", "DNB_PC_PORT": "7878",
                                    "SHPC_COMFY": "http://127.0.0.1:8188"}):
            with patch("shp_photocraft_tools.supervisor.socket.create_connection") as connect:
                status = external_status()
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(status["photocraft_crop"], status["photocraft_dnb"])
            with patch("shp_photocraft_tools.supervisor.socket.create_connection", side_effect=OSError("offline")) as connect:
                status = external_status()
            self.assertEqual(connect.call_count, 2)
            self.assertFalse(status["photocraft_crop"]["reachable"])
            self.assertEqual(status["photocraft_crop"], status["photocraft_dnb"])

    def test_bad_external_configuration_is_reported_without_losing_other_indicators(self):
        with patch.dict(os.environ, {"SHPC_COMFY": "not-a-url", "SHPC_PC_PORT": "invalid", "DNB_PC_PORT": "1"}):
            status = external_status()
        self.assertIn("HTTP(S)", status["comfy"]["error"])
        self.assertIn("error", status["photocraft_crop"])
        self.assertEqual(status["photocraft_dnb"]["address"], "127.0.0.1:1")

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
        self.manager = self.server.modules
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

    def control(self, name, action, **kwargs):
        return self.request(f"/api/modules/{name}/{action}",
                            dict(instance=self.manager.modules[name].instance, **kwargs))

    def test_packaged_assets_relative_api_image_routes_and_fixed_proxy(self):
        self.assertIn(b"SHP Portrait Tools", self.request("/"))
        for path in ("/static/shell.js", "/static/shell.css", "/static/lifecycle.js", "/static/shp-ui.css"):
            self.assertTrue(self.request(path))
        for name in ("crop", "dnb"):
            page = self.request(f"/{name}/")
            self.assertIn(b'href="static/shp-ui.css"', page)
            self.assertEqual(self.request(f"/{name}/api/health")["module"], name)
            self.assertEqual(self.request(f"/{name}/static/shp-ui.css"), self.request("/static/shp-ui.css"))
            self.assert_error(403, f"/{name}/api/owner/status", {})
        self.request("/crop/api/fetch", {})
        self.assertTrue(self.request("/crop/img/xorig?v=1").startswith(b"\xff\xd8"))
        for path in ("/crop/static/../pyproject.toml", "/crop/%61pi/owner/status"):
            self.assert_error(403, path)
        self.assert_error(404, "/http://example.test/")

    def test_stop_restart_loaded_guard_and_other_module_untouched(self):
        self.request("/crop/api/fetch", {})
        before = self.wait_ready()
        error = self.assert_error(409, "/api/modules/crop/restart", {"instance": before["crop"]["instance"]})
        self.assertEqual(error["code"], "confirmation")
        self.assertEqual(self.wait_ready()["crop"]["instance"], before["crop"]["instance"])
        # Another shell changes work while the first confirmation is open.
        self.request("/crop/api/fetch", {})
        changed = self.assert_error(409, "/api/modules/crop/restart",
                                    {"instance": before["crop"]["instance"], "confirm": True,
                                     "revision": error["revision"]})
        self.assertEqual(changed["code"], "changed")
        restarted = self.control("crop", "restart", confirm=True, revision=changed["revision"])
        self.assertNotEqual(restarted["instance"], before["crop"]["instance"])
        self.assertEqual(self.wait_ready()["dnb"]["instance"], before["dnb"]["instance"])
        self.assertFalse(restarted["loaded"])
        self.assertEqual(self.control("crop", "stop")["phase"], "stopped")
        error = self.assert_error(409, "/crop/")
        self.assertEqual(error["code"], "module_stopped")
        self.assertEqual(self.control("crop", "start")["phase"], "running")

    def test_hue_center_real_proxy_and_direct_child_route(self):
        loaded=self.request("/dnb/api/pc/fetch",{})
        body={"generation":loaded["generation"],"S":loaded["S"],"mode":"frequency",
              "tone":{"on":False},"color":{"on":False},"structure":{"protect":0}}
        proxied=self.request("/dnb/api/hue-center",body)
        self.assertIsInstance(proxied["center"],float)
        connection=http.client.HTTPConnection("127.0.0.1",self.manager.config["ports"]["dnb"])
        try:
            connection.request("POST","/api/hue-center",json.dumps(body),{"Content-Type":"application/json"})
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read()),proxied)
        finally:
            connection.close()
        self.request("/dnb/api/test/hue-route",{"available":False})
        connection=http.client.HTTPConnection("127.0.0.1",self.server.server_port)
        try:
            connection.request("POST","/dnb/api/hue-center",json.dumps(body),
                               {"Content-Type":"application/json","Origin":self.base})
            response=connection.getresponse()
            self.assertEqual(response.status,404)
            self.assertEqual(response.getheader("Content-Type"),"text/plain")
            self.assertEqual(response.read(),b"not found")
        finally:
            connection.close()

    def test_busy_api_and_background_job_cannot_be_forced(self):
        with ThreadPoolExecutor() as pool:
            work = pool.submit(self.request, "/dnb/api/test/work", {})
            deadline = time.monotonic() + 2
            while not self.manager.snapshot("dnb")["busy"] and time.monotonic() < deadline:
                time.sleep(.02)
            result = self.assert_error(409, "/api/modules/dnb/stop",
                                      {"instance": self.manager.modules["dnb"].instance, "confirm": True})
            self.assertEqual(result["code"], "busy")
            self.assertTrue(work.result()["done"])
        self.request("/crop/api/test/job", {"seconds": 1})
        result = self.assert_error(409, "/api/modules/crop/restart",
                                  {"instance": self.manager.modules["crop"].instance, "confirm": True})
        self.assertEqual(result["code"], "busy")

    def test_identity_change_and_missing_owner_capability_refused(self):
        module = self.manager.modules["crop"]
        instance = module.instance
        module.instance = "not-the-owned-instance"
        try:
            with self.assertRaisesRegex(LaunchError, "ownership changed"):
                self.manager.stop("crop", True)
            self.assertIsNone(module.process.poll())
        finally:
            module.instance = instance
        connection = http.client.HTTPConnection("127.0.0.1", self.manager.config["ports"]["crop"])
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
            self.assert_error(403, "/api/modules/crop/stop", {}, headers)
        self.assert_error(403, "/api/status", headers={"Host": "rebound.example.test"})
        self.assert_error(400, "/api/modules/crop/exec", {})
        self.assert_error(400, "/api/modules/crop/stop", {"confirm": "yes"})
        self.assert_error(400, "/api/modules/crop/stop", {"revision": True})
        self.assert_error(409, "/api/modules/crop/stop", {"instance": "stale-window"})

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
        self.request("/crop/api/test/job", {"seconds": 1.2})
        life.close("refreshed")
        time.sleep(.2)
        self.assertFalse(life.should_stop(self.manager.busy))
        self.server.finished.wait(5)
        self.assertTrue(self.server.finished.is_set())
        self.manager.close()
        self.assertTrue(all(module.process.poll() == 0 for module in self.manager.modules.values()))


if __name__ == "__main__":
    unittest.main()
