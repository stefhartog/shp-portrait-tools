"""Linux Qt desktop host: navigation rules, page hooks, thread hand-off, single instance and process identity.

The shared quit/restart/tray-model behaviour is covered by test_desktop_host.py with a fake platform; these
tests cover only what qt_controller adds. They run Qt offscreen, so no window appears.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    if sys.platform == "win32":
        raise ImportError("Linux Qt host")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from shp_portrait_tools.desktop import qt_controller as Q
except ImportError as error:  # pragma: no cover - platform/dependency guard
    Q = None
    SKIP = str(error)
else:
    SKIP = ""

BASE, PAGE = "http://127.0.0.1:18760", "http://127.0.0.1:18765"


def trusted(origin):
    return origin in (BASE, PAGE)


def app():
    return QApplication.instance() or QApplication(["test-qt-host"])


def pump(until, timeout=10):
    deadline = time.monotonic() + timeout
    while not until():
        if time.monotonic() >= deadline:
            raise AssertionError("Qt event loop condition not reached")
        app().processEvents()
        time.sleep(0.01)


@unittest.skipIf(Q is None, SKIP)
class NavigationTests(unittest.TestCase):
    def test_only_tools_origins_blank_and_their_blobs(self):
        allowed = [BASE + "/", PAGE + "/", PAGE + "/static/x.css?v=1#top", "about:blank",
                   "blob:" + PAGE + "/7f3c2a", "blob:" + BASE + "/1"]
        blocked = ["http://127.0.0.1:18766/", "http://localhost:18765/", "https://127.0.0.1:18765/",
                   "http://user:pw@127.0.0.1:18765/", "http://user@127.0.0.1:18765/", "file:///etc/passwd",
                   "data:text/html,<p>x</p>", "blob:https://example.com/1", "blob:null/1", "javascript:alert(1)",
                   "https://example.com/", "about:srcdoc", ""]
        for url in allowed:
            self.assertTrue(Q.navigation_allowed(url, trusted), url)
        for url in blocked:
            self.assertFalse(Q.navigation_allowed(url, trusted), url)

    def test_document_script_flags_desktop_and_only_the_page_origin_for_pickers(self):
        script = Q.document_script(PAGE)
        self.assertIn("window.__shpOwnedDesktop = true", script)
        self.assertIn(f'location.origin === "{PAGE}"', script)
        # The origin is JSON-quoted, so a hostile value cannot break out of the string.
        self.assertIn('\\"', Q.document_script('x";alert(1);"'))

    def test_single_instance_name_is_per_supervisor_and_in_the_runtime_dir(self):
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, XDG_RUNTIME_DIR=runtime):
            first, second = Q.single_instance_name(BASE + "/"), Q.single_instance_name("http://127.0.0.1:18761/")
            self.assertTrue(first.startswith(runtime + "/shp-portrait-tools-"))
            self.assertNotEqual(first, second)
            self.assertEqual(first, Q.single_instance_name(BASE + "/"))


@unittest.skipIf(Q is None, SKIP)
class ProcessIdentityTests(unittest.TestCase):
    def test_own_identity_matches_the_launcher_wait_and_detects_replacement(self):
        from shp_portrait_tools.shared.owned_process import process_identity
        host = Q.Host(BASE + "/", Path(tempfile.gettempdir()))
        pid, created = host.own_identity()
        self.assertEqual((pid, created), (os.getpid(), process_identity(os.getpid())[1]))
        self.assertTrue(host.process_alive(pid, created))
        self.assertFalse(host.process_alive(pid, created + 1))  # same pid, different process
        self.assertFalse(host.process_alive(2 ** 22 + 7, created))  # beyond pid_max: gone

    def test_spawn_detached_starts_a_new_session(self):
        host = Q.Host(BASE + "/", Path(tempfile.gettempdir()))
        with patch.object(Q.subprocess, "Popen") as popen:
            host.spawn_detached(["true"], log=None)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


@unittest.skipIf(Q is None, SKIP)
class InvokerTests(unittest.TestCase):
    def test_worker_calls_run_on_the_gui_thread_and_return_results_or_errors(self):
        app()
        invoker = Q.Invoker()
        gui = threading.current_thread()
        seen = {}

        def worker():
            seen["thread"] = invoker.wait(threading.current_thread)
            try:
                invoker.wait(lambda: 1 / 0)
            except ZeroDivisionError:
                seen["error"] = True

        thread = threading.Thread(target=worker)
        thread.start()
        pump(lambda: not thread.is_alive())
        self.assertIs(seen["thread"], gui)
        self.assertTrue(seen["error"])
        self.assertEqual(invoker.wait(lambda: 5), 5)  # already on the GUI thread: runs inline


@unittest.skipIf(Q is None, SKIP)
class GuardedPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app()
        from qtpy.QtWebEngineWidgets import QWebEngineView
        from webview.platforms import qt as backend
        cls.backend, cls.original = backend, backend.BrowserView.WebPage
        cls.host = Mock(base=BASE, page=PAGE, hooked=True, trusted_origin=trusted)
        Q.install_page_class(cls.host)
        cls.view = QWebEngineView()

    @classmethod
    def tearDownClass(cls):
        cls.backend.BrowserView.WebPage = cls.original

    def page(self):
        return self.backend.BrowserView.WebPage(Mock(webview=self.view))

    def test_installs_once_and_injects_flags_at_document_creation(self):
        guarded = self.backend.BrowserView.WebPage
        Q.install_page_class(self.host)
        self.assertIs(self.backend.BrowserView.WebPage, guarded)
        from qtpy.QtWebEngineCore import QWebEngineScript
        scripts = self.page().scripts().find("shp-tools-flags")
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0].injectionPoint(), QWebEngineScript.InjectionPoint.DocumentCreation)
        self.assertFalse(scripts[0].runsOnSubFrames())
        self.assertIn(PAGE, scripts[0].sourceCode())

    def test_navigation_popups_and_permissions(self):
        from qtpy.QtCore import QUrl
        page = self.page()
        self.host.message.reset_mock()
        self.assertTrue(page.acceptNavigationRequest(QUrl(PAGE + "/"), 0, True))
        self.assertFalse(page.acceptNavigationRequest(QUrl("https://example.com/"), 0, True))
        self.assertIn("Blocked navigation", self.host.message.call_args.args[0])
        self.assertIsNone(page.createWindow(0))
        permission = Mock()
        page.deny_permission(permission)
        permission.deny.assert_called_once_with()

    def test_placeholder_loads_before_the_hooks_are_armed(self):
        from qtpy.QtCore import QUrl
        self.host.hooked = False
        try:
            self.assertTrue(self.page().acceptNavigationRequest(QUrl("data:text/html,x"), 0, True))
        finally:
            self.host.hooked = True


@unittest.skipIf(Q is None, SKIP)
class SingleInstanceTests(unittest.TestCase):
    def test_second_launch_asks_the_first_window_to_show(self):
        app()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, XDG_RUNTIME_DIR=root):
            data = Path(root) / "data"
            data.mkdir()
            first, second = Q.Host(BASE + "/", data), Q.Host(BASE + "/", data)
            shown = threading.Event()
            first.show = shown.set
            self.assertTrue(first.claim_single_instance())
            self.assertFalse(second.claim_single_instance())
            pump(shown.is_set)
            first.single_server.close()

    def test_stale_socket_is_replaced(self):
        app()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, XDG_RUNTIME_DIR=root):
            data = Path(root) / "data"
            data.mkdir()
            Path(Q.single_instance_name(BASE + "/")).write_text("")  # left behind by a crashed host
            host = Q.Host(BASE + "/", data)
            self.assertTrue(host.claim_single_instance())
            host.single_server.close()


if __name__ == "__main__":
    unittest.main()
