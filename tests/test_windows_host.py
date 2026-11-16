import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from shp_photocraft_tools.desktop.protocol import validate_url
from shp_photocraft_tools.shared.filelock import file_lock


class FileLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.lock"
            with file_lock(path):
                with self.assertRaises(Exception):
                    with file_lock(path, timeout=0.3):
                        pass
            with file_lock(path, timeout=0.3):
                pass

    def test_injected_acquire_release_called(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with file_lock(Path(directory) / "y.lock", acquire=lambda f, t: events.append("a"),
                           release=lambda f: events.append("r")):
                events.append("body")
        self.assertEqual(events, ["a", "body", "r"])


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsProcessTests(unittest.TestCase):
    def setUp(self):
        from shp_photocraft_tools.shared import windows_process as wp
        self.wp = wp

    def spawn(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **self.wp.spawn_options())
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def test_adopt_owns_and_terminates(self):
        process = self.spawn()
        identity = self.wp.adopt(process)
        self.assertEqual(identity[0], process.pid)
        self.assertTrue(self.wp.owns(process, process.pid))
        self.assertFalse(self.wp.owns(process, 4))
        self.assertTrue(self.wp.is_alive(*identity))
        handles = self.wp.pin_session(process, identity)
        self.wp.drain_pinned(handles, graceful=lambda: None, grace=0.1)
        process.wait(5)
        self.assertFalse(self.wp.is_alive(*identity))
        self.wp.close_handle(handles[0])

    def test_identity_mismatch_refused(self):
        process = self.spawn()
        identity = self.wp.adopt(process)
        self.assertFalse(self.wp.is_alive(identity[0], identity[1] + 1))
        process.kill()
        process.wait(5)
        with self.assertRaises(ProcessLookupError):
            self.wp.process_identity(process.pid)

    def test_single_instance(self):
        first = self.wp.SingleInstance("SHP.Test.single.instance")
        second = self.wp.SingleInstance("SHP.Test.single.instance")
        self.assertTrue(first.primary)
        self.assertFalse(second.primary)
        result = []
        waiter = threading.Thread(target=lambda: result.append(first.wait_for_show(3000)))
        waiter.start()
        second.signal_primary()
        waiter.join()
        self.assertEqual(result, [True])


class ProtocolTests(unittest.TestCase):
    def test_validate_url(self):
        self.assertEqual(validate_url("http://127.0.0.1:18760/"), "http://127.0.0.1:18760")
        with self.assertRaises(ValueError):
            validate_url("http://example.com:1/")

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_prompt_protocol(self):
        from shp_photocraft_tools.desktop.windows_controller import answer_open_prompt

        class Fake:
            def __init__(self, choice="folder"):
                self.choice = choice
            def image(self): return "C:\\a.png"
            def images(self): return ["C:\\a.png", "C:\\b.png"]
            def folder(self): return "C:\\dir"
            def batch_source(self): return self.choice

        self.assertEqual(answer_open_prompt("SHP_OPEN_IMAGE_V1", Fake()), "C:\\a.png")
        self.assertEqual(answer_open_prompt("SHP_OPEN_BATCH_FOLDER_V1", Fake()), "C:\\dir")
        self.assertEqual(json.loads(answer_open_prompt("SHP_OPEN_BATCH_IMAGES_V1", Fake())), Fake().images())
        self.assertEqual(json.loads(answer_open_prompt("SHP_OPEN_BATCH_SOURCE_V1", Fake())), {"folder": "C:\\dir"})
        self.assertEqual(json.loads(answer_open_prompt("SHP_OPEN_BATCH_SOURCE_V1", Fake("images")))["paths"], Fake().images())
        self.assertIsNone(answer_open_prompt("SHP_OPEN_BATCH_SOURCE_V1", Fake(None)))


if __name__ == "__main__":
    unittest.main()
