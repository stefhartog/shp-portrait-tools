"""Standalone Skin Tools service on Windows: loopback bind ownership and clean termination."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

WINDOWS = sys.platform == "win32"


def start(port, data, inbox):
    env = dict(os.environ, DNB_DATA=data, DNB_WATCH_DIRECTORY=inbox, PYTHONIOENCODING="utf-8",
               APPDATA=str(Path(data).parent / "config"))
    env.pop("SHP_SUPERVISOR_TOKEN", None)
    return subprocess.Popen([sys.executable, "-m", "shp_portrait_tools.modules.dnb.ui_server", "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, text=True, encoding="utf-8")


@unittest.skipUnless(WINDOWS, "Windows service ownership")
class StandaloneServiceTests(unittest.TestCase):
    def test_port_is_exclusive_and_service_stops_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="shp svc ") as temp:
            data, inbox = str(Path(temp) / "data"), str(Path(temp) / "inbox")
            settings = Path(temp) / "config" / "shp-photocraft-tools" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"source_editor": "photoshop", "custom": "keep"}')
            before = settings.read_bytes()
            first = start(0, data, inbox)
            def cleanup():
                if first.poll() is None:
                    first.kill()
                    first.wait(timeout=15)
                first.stdout.close()
            self.addCleanup(cleanup)
            line = ""
            while "http://127.0.0.1:" not in line:
                line = first.stdout.readline()
                self.assertTrue(line or first.poll() is None, "server exited before listening")
            port = int(line.split("http://127.0.0.1:")[1].split()[0])
            with urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"Skin Tools", response.read())
            origin = f"http://127.0.0.1:{port}"
            request = Request(origin + "/api/editor", b"{}", {
                "Content-Type": "application/json", "Origin": origin})
            with urlopen(request, timeout=10) as response:
                self.assertEqual(json.load(response), {"editor": "photoshop", "photoshop_hue_native": True, "photoshop_png_capture": True})
            self.assertEqual(settings.read_bytes(), before)
            second = start(port, data, inbox)
            try:
                output, _ = second.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                second.kill()
                second.communicate()
                self.fail("a second service bound a port that is already owned")
            self.assertNotEqual(second.returncode, 0)
            self.assertRegex(output, r"10048|Address already in use")
            with urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                self.assertEqual(response.status, 200)
            first.terminate()
            first.wait(timeout=15)
            self.assertFalse(Path(data, "models").exists())


if __name__ == "__main__":
    unittest.main()
