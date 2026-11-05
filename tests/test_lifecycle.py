"""Tab lease expiry, reloads, busy protection and real HTTP shutdown without external services."""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shp_photocraft_tools.modules.crop.server import Handler
from shp_photocraft_tools.shared.lifecycle import BrowserLifetime, BrowserServer


class LifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.lifetime = BrowserLifetime(clock=lambda: self.now)

    def test_startup_has_time_for_firefox(self):
        self.now = 119
        self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.now = 120
        self.assertTrue(self.lifetime.should_stop(lambda: False))
        self.assertFalse(self.lifetime.touch("too-late"))
        self.assertFalse(self.lifetime.reserve())

    def test_last_tab_close_grace_and_refresh(self):
        self.lifetime.touch("one")
        self.lifetime.touch("two")
        self.lifetime.close("one")
        self.now = 30
        self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.lifetime.close("two")
        self.now = 44
        self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.lifetime.touch("refreshed")
        self.now = 46
        self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.lifetime.close("refreshed")
        self.now = 61
        self.assertTrue(self.lifetime.should_stop(lambda: False))

    def test_lost_close_beacon_eventually_expires(self):
        self.lifetime.touch("crashed-tab")
        self.now = 121
        self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.now = 136
        self.assertTrue(self.lifetime.should_stop(lambda: False))

    def test_processing_and_generation_finish_before_shutdown(self):
        self.lifetime.touch("tab")
        self.lifetime.close("tab")
        self.now = 20
        with self.lifetime.work():
            self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.assertFalse(self.lifetime.should_stop(lambda: True))
        self.assertTrue(self.lifetime.should_stop(lambda: False))
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            with self.lifetime.work():
                self.fail("admitted work after shutdown")

    def test_renewal_and_launcher_reservation(self):
        self.lifetime.touch("background-tab")
        for self.now in range(20, 200, 20):
            self.assertTrue(self.lifetime.touch("background-tab"))
            self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.lifetime.close("background-tab")
        self.now = 190
        self.assertTrue(self.lifetime.reserve())
        self.now = 200
        self.assertFalse(self.lifetime.should_stop(lambda: False))
        self.lifetime.touch("reopened")
        self.assertFalse(self.lifetime.should_stop(lambda: False))

    def test_unknown_close_cannot_extend_or_close_other_tabs(self):
        self.lifetime.touch("real")
        self.lifetime.close("unknown")
        self.assertIn("real", self.lifetime.clients)
        self.assertEqual(self.lifetime.deadline, 120)


class HttpTests(unittest.TestCase):
    def test_managed_module_serves_script_closes_and_exits(self):
        server = BrowserServer(("127.0.0.1", 0), Handler, close_with_ui=True)
        server.lifetime = BrowserLifetime(lease=0.1, grace=0.1, startup=0.1)
        # Health/script requests do not create a lease; explicit close is fast, heartbeat stays long-polled.
        server.lifetime.touch("tab")
        server.lifetime.lease = 90
        server.lifetime.touch("tab")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/api/health") as response:
                self.assertEqual(json.load(response)["ui_lifecycle"], 1)
            with urlopen(base + "/static/lifecycle.js") as response:
                self.assertIn(b"pagehide", response.read())
            request = Request(base + "/api/ui/close", json.dumps({"client": "tab"}).encode())
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["accepted"])
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive(), "last closed tab did not stop server")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_manual_server_does_not_opt_in_and_bad_requests_fail(self):
        server = BrowserServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/api/ui/heartbeat", json.dumps({"client": "manual"}).encode())
            with urlopen(request) as response:
                self.assertFalse(json.load(response)["managed"])
            for body in ({}, {"client": 3}, {"client": ""}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + "/api/ui/heartbeat", json.dumps(body).encode()))
                self.assertEqual(error.exception.code, 400)
            self.assertTrue(thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_native_controller_heartbeat_is_short_and_closes_without_renewal_race(self):
        server = BrowserServer(("127.0.0.1", 0), Handler, close_with_ui=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/api/ui/heartbeat", json.dumps({"client": "desktop-fixture"}).encode())
            with urlopen(request, timeout=2) as response:
                self.assertTrue(json.load(response)["accepted"])
            self.assertIn("desktop-fixture", server.lifetime.clients)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
