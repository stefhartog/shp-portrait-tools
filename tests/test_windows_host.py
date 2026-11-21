import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shp_portrait_tools.desktop.protocol import validate_url
from shp_portrait_tools.shared.filelock import file_lock


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
        from shp_portrait_tools.shared import windows_process as wp
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

    def test_editor_started_by_a_service_leaves_its_job(self):
        """PhotoCraft started from the worker must not keep the worker's job alive (strict stop/restart)."""
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "editor.pid"
            code = ("import subprocess, sys; from pathlib import Path; "
                    "from shp_portrait_tools.shared.windows_process import popen_outside_job; "
                    "p = popen_outside_job([sys.executable, '-c', 'import time; time.sleep(60)']); "
                    f"Path({str(pid_file)!r}).write_text(str(p.pid))")
            # pythonw, as the supervised worker runs: no console host of its own in the job.
            process = subprocess.Popen([self.wp.background_python(), "-c", code], **self.wp.spawn_options())
            self.addCleanup(lambda: process.poll() is None and process.kill())
            self.wp.adopt(process)
            process.wait(30)
            editor = int(pid_file.read_text())
            self.addCleanup(lambda: subprocess.run(["taskkill", "/PID", str(editor), "/T", "/F"], capture_output=True))
            self.assertFalse(self.wp.owns(process, editor))
            self.wp.require_session_stopped(process, wait=5)  # raised "Owned job still has processes" before
            self.assertTrue(self.wp.is_alive(*self.wp.process_identity(editor)))

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
class NoConsoleTests(unittest.TestCase):
    def test_identity_and_native_icon_are_ready_before_gui_start(self):
        from shp_portrait_tools.desktop import windows_controller as wc
        import webview

        calls = []
        with tempfile.TemporaryDirectory() as directory:
            host = wc.Host("http://127.0.0.1:18760/", Path(directory))

            def start(**options):
                calls.append("start")
                self.assertEqual(options["gui"], "edgechromium")
                self.assertTrue(Path(options["icon"]).is_file())
                self.assertEqual(options["storage_path"], str(host.data / "data"))

            with patch("shp_portrait_tools.desktop.windows_identity.set_process_app_id",
                       side_effect=lambda: calls.append("identity")), \
                 patch("shp_portrait_tools.shared.windows_process.SingleInstance",
                       return_value=Mock(primary=True)), \
                 patch.object(host, "create_window", side_effect=lambda: calls.append("window")), \
                 patch.object(host, "create_tray", side_effect=lambda: calls.append("tray")), \
                 patch.object(wc.threading, "Thread"), patch.object(webview, "start", side_effect=start):
                self.assertEqual(host.run(), 0)
        self.assertEqual(calls, ["identity", "window", "tray", "start"])

    def test_background_python_prefers_pythonw(self):
        from shp_portrait_tools.shared.windows_process import background_python
        with tempfile.TemporaryDirectory() as directory:
            python, pythonw = Path(directory, "python.exe"), Path(directory, "pythonw.exe")
            python.touch()
            self.assertEqual(background_python(str(python)), str(python))
            pythonw.touch()
            self.assertEqual(background_python(str(python)), str(pythonw))
            self.assertEqual(background_python(str(pythonw)), str(pythonw))

    def test_spawn_flags_hide_console(self):
        from shp_portrait_tools.shared import windows_process as wp
        for flags in (wp.spawn_options(), wp.detached_options()):
            self.assertTrue(flags["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_launcher_uses_background_python_for_children(self):
        from shp_portrait_tools import launcher
        self.assertTrue(Path(launcher.background_python()).name.lower() in ("pythonw.exe", Path(sys.executable).name.lower()))


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class ShortcutInstallerTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import install_windows_shortcut as installer
        self.installer = installer
        self.addCleanup(sys.path.pop, 0)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        (self.repo / ".venv" / "Scripts").mkdir(parents=True)
        (self.repo / ".venv" / "Scripts" / "pythonw.exe").touch()
        self.out = root / "out"
        self.base = ["--output-dir", str(self.out), "--repo", str(self.repo), "--config", str(root / "launcher.json"), "--desktop"]
        self.links = [self.out / "Start Menu" / "SHP Portrait Tools.lnk", self.out / "Desktop" / "SHP Portrait Tools.lnk"]

    def run_installer(self, *extra):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.installer.main([*self.base, *extra])

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_installer("--dry-run"), 0)
        self.assertFalse(self.out.exists())

    def test_create_refuse_force_remove(self):
        self.assertEqual(self.run_installer(), 0)
        self.assertTrue(all(link.exists() for link in self.links))
        details = self.installer.read_shortcut(self.links[0])
        self.assertEqual(details["target"], str(self.repo / ".venv" / "Scripts" / "pythonw.exe"))
        self.assertIn("-m shp_portrait_tools.launcher --config", details["arguments"])
        self.assertEqual(details["cwd"], str(self.repo))
        self.assertEqual(details["app_id"], self.installer.APP_ID)
        self.assertTrue((self.out / "shp-photocraft-tools.ico").exists())
        self.links[0].write_bytes(b"keep")
        self.assertEqual(self.run_installer(), 1)
        self.assertEqual(self.links[0].read_bytes(), b"keep")
        self.assertEqual(self.run_installer("--force"), 0)
        self.assertNotEqual(self.links[0].read_bytes(), b"keep")
        self.assertEqual(self.run_installer("--remove", "--dry-run"), 0)
        self.assertTrue(self.links[0].exists())
        self.assertEqual(self.run_installer("--remove"), 0)
        self.assertFalse(any(link.exists() for link in self.links))

    def test_app_id_update_preserves_shortcut_properties(self):
        self.assertEqual(self.run_installer(), 0)
        link = self.links[0]
        self.installer.powershell(
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHP_LNK);"
            "$s.Description='User description';$s.Hotkey='CTRL+ALT+F9';$s.WindowStyle=7;$s.Save()",
            SHP_LNK=str(link))
        before = self.installer.read_shortcut(link)
        read_properties = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHP_LNK);"
            "$s.Description;$s.Hotkey;$s.WindowStyle;"
            "$f=(New-Object -ComObject Shell.Application).NameSpace((Split-Path $env:SHP_LNK));"
            "$f.ParseName((Split-Path $env:SHP_LNK -Leaf)).ExtendedProperty('System.AppUserModel.ID')")
        before_properties = self.installer.powershell(read_properties, SHP_LNK=str(link))
        self.installer.shortcut_app_id(link, "SHP.Test.Previous")
        self.installer.shortcut_app_id(link, self.installer.APP_ID)
        self.assertEqual(self.installer.read_shortcut(link), before)
        properties = self.installer.powershell(read_properties, SHP_LNK=str(link))
        self.assertEqual(properties, before_properties)
        self.assertEqual(properties[-1], self.installer.APP_ID)


class IconTests(unittest.TestCase):
    def test_native_images_match_launcher_and_grayscale_svg_artwork(self):
        import xml.etree.ElementTree as ET
        from shp_portrait_tools.desktop.icons import icon_image
        from shp_portrait_tools.desktop.windows_controller import tray_image

        for tray in (False, True):
            name = "shp-photocraft-tools" + ("-tray" if tray else "") + ".svg"
            svg = ET.parse(Path(__file__).resolve().parent.parent / "desktop" / name).getroot()
            rect, brackets, circle, half = list(svg)
            image = icon_image(tray=tray, size=256)
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            for point, color in (((15, 64), rect.attrib["fill"]), ((9, 64), rect.attrib["stroke"]),
                                 ((27, 40), brackets.attrib["stroke"]), ((78, 64), circle.attrib["fill"]),
                                 ((50, 64), half.attrib["fill"])):
                self.assertEqual(image.getpixel(tuple(v * 2 for v in point)), (*bytes.fromhex(color.lstrip("#")), 255))
            if tray:
                self.assertTrue(all(r == g == b for r, g, b, a in image.get_flattened_data() if a))
        self.assertEqual(tray_image().tobytes(), icon_image(tray=True, size=64).tobytes())

    def test_multi_size_ico_matches_native_launcher_artwork(self):
        from PIL import Image
        from shp_portrait_tools.desktop.icons import icon_image, write_icon

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.ico"
            write_icon(path)
            with Image.open(path) as image:
                self.assertEqual(image.ico.sizes(), {(s, s) for s in (16, 24, 32, 48, 64, 128, 256)})
                self.assertEqual(image.ico.getimage((256, 256)).tobytes(), icon_image().tobytes())

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_winforms_can_decode_native_icon(self):
        import clr
        clr.AddReference("System.Drawing")
        from System.Drawing import Icon
        from shp_portrait_tools.desktop.icons import write_icon

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.ico"
            write_icon(path)
            icon = Icon(str(path))
            bitmap = icon.ToBitmap()
            try:
                color = bitmap.GetPixel(round(bitmap.Width * 50 / 128), round(bitmap.Height * 64 / 128))
                self.assertEqual(color.A, 255)
                self.assertTrue(all(abs(actual - expected) <= 4
                                    for actual, expected in zip((color.R, color.G, color.B), (76, 141, 255))))
                self.assertEqual(bitmap.GetPixel(0, 0).A, 0)
            finally:
                bitmap.Dispose()
                icon.Dispose()


if __name__ == "__main__":
    unittest.main()
