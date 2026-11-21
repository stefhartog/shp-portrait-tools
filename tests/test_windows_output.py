"""Windows output safety checks with synthetic data in disposable directories."""

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from shp_portrait_tools.shared import local_output, winfs
from shp_portrait_tools.shared.watch_folder import WatchFolder, verify_png

WINDOWS = sys.platform == "win32"


def make_png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"".join(b"\x00" + bytes(6) for _ in range(2)))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")

def junction(link, target):
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


@unittest.skipUnless(WINDOWS, "Windows filesystem semantics")
class WindowsLocalOutputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="shp out ü ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.image = self.root / "synthetic photo.png"
        self.image.write_bytes(b"source")

    def test_numbering_never_overwrites_including_case_and_directories(self):
        (self.root / "SYNTHETIC PHOTO_shpdnb_001.PNG").write_bytes(b"keep")
        (self.root / "synthetic photo_shpdnb_002.png").mkdir()
        first = Path(local_output.write_local_result(self.image, "shpdnb", b"new"))
        self.assertEqual(first.name, "synthetic photo_shpdnb_003.png")
        self.assertEqual((self.root / "SYNTHETIC PHOTO_shpdnb_001.PNG").read_bytes(), b"keep")
        self.assertEqual(first.read_bytes(), b"new")

    def test_failed_write_removes_only_its_own_file(self):
        before = set(self.root.iterdir())
        with patch.object(local_output.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                local_output.write_local_result(self.image, "shpfs", b"partial")
        self.assertEqual(set(self.root.iterdir()), before)
        with patch.object(local_output.os, "open", side_effect=PermissionError("read-only directory")):
            with self.assertRaisesRegex(PermissionError, "read-only directory"):
                local_output.write_local_result(self.image, "shpfs", b"data")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_unsafe_suffix_and_extension_are_rejected(self):
        for suffix in ("a:stream", "bad/name", "x\\y", "q?"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    local_output.write_local_result(self.image, suffix, b"x")
        device = local_output.write_local_result(self.root / "CON.png", "shpfs", b"x")
        self.assertEqual(Path(device).name, "CON_shpfs_001.png")
        Path(device).unlink()
        with self.assertRaisesRegex(ValueError, "extension"):
            local_output.write_local_result(self.image, "shpfs", b"x", extension="/escape")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.image.name])

    def test_validate_name_rules(self):
        for bad in ("NUL", "com1.png", "LPT3.txt", "a.", "a ", "a:b", "a*b", "a?b", "", "tab\tname"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    winfs.validate_name(bad)
        for good in ("console.png", "CON_shpdnb_001.png", "ünï cødé.png", "a b.png"):
            self.assertEqual(winfs.validate_name(good), good)

    def test_long_path_output(self):
        folder = self.root
        while len(str(folder)) < 300:
            folder = folder / ("d" * 50)
        os.makedirs(winfs.extended(folder))
        image = folder / "long.png"
        image.write_bytes(b"source")
        output = local_output.write_local_result(image, "shpdnb", b"long")
        self.assertGreater(len(output), 260)
        self.assertEqual(Path(winfs.extended(output)).read_bytes(), b"long")

    def test_swapped_source_folder_is_detected_via_junction(self):
        other = self.root / "other"
        other.mkdir()
        alias = self.root / "alias"
        junction(alias, other)
        original = self.root.stat()
        with self.assertRaisesRegex(ValueError, "source folder changed"):
            local_output.write_local_result(alias / self.image.name, "shpfs", b"data",
                                            (original.st_dev, original.st_ino))
        self.assertEqual(list(other.iterdir()), [])

    def test_pinned_folder_cannot_be_renamed_while_writing(self):
        pin = winfs.pin_directory(self.root)
        try:
            with self.assertRaises(OSError):
                os.rename(self.root, self.root.with_name(self.root.name + "_renamed"))
        finally:
            winfs.close_handle(pin)

    def test_batch_outputs_preset_replacement_and_external_change(self):
        with local_output.batch_output(self.root) as (folder, write, write_preset):
            self.assertEqual(folder.name, "shpcombine_batch_001")
            first = Path(write(0, "x" * 80 + ".jpg", b"one"))
            self.assertEqual(first.name, "x" * 40 + "_001.png")
            with self.assertRaises(FileExistsError):
                write(0, "x" * 80 + ".jpg", b"two")
            self.assertEqual(first.read_bytes(), b"one")
            write_preset({"a": 1})
            write_preset({"a": 2})
            self.assertEqual(json.loads((folder / "preset.json").read_text()), {"a": 2})
            self.assertFalse((folder / ".preset.json.tmp").exists())
            (folder / "preset.json").write_text("edited elsewhere")
            with self.assertRaisesRegex(ValueError, "changed externally"):
                write_preset({"a": 3})
            self.assertEqual((folder / "preset.json").read_text(), "edited elsewhere")
            self.assertFalse((folder / ".preset.json.tmp").exists())
        with local_output.batch_output(self.root) as (second, _, _):
            self.assertEqual(second.name, "shpcombine_batch_002")

    def test_batch_preset_locked_by_another_handle_is_not_overwritten(self):
        with local_output.batch_output(self.root) as (folder, _, write_preset):
            write_preset({"a": 1})
            with (folder / "preset.json").open("rb"):
                with self.assertRaises(PermissionError):
                    write_preset({"a": 2})
            self.assertEqual(json.loads((folder / "preset.json").read_text()), {"a": 1})
            self.assertFalse((folder / ".preset.json.tmp").exists())

    def test_batch_output_folder_is_pinned(self):
        with local_output.batch_output(self.root) as (folder, write, _):
            write(0, "a.png", b"x")
            moved = self.root / "moved"
            with self.assertRaises(OSError):
                os.rename(folder, moved)
            with self.assertRaises(OSError):
                os.rmdir(folder)

    def test_batch_requires_real_directory(self):
        with self.assertRaises(OSError):
            with local_output.batch_output(self.root / "missing"):
                pass
        with self.assertRaises(OSError):
            with local_output.batch_output(self.image):
                pass


@unittest.skipUnless(WINDOWS, "Windows filesystem semantics")
class WindowsWatchFolderTests(unittest.TestCase):
    PNG = make_png()
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="shp inbox ü ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.now = [0]
        self.watch = WatchFolder(self.inbox, self.root / "retained", clock=lambda: self.now[0])
        self.addCleanup(self.watch.close)

    def test_png_fixture_is_valid(self):
        (self.inbox / "a.png").write_bytes(self.PNG)
        verify_png(self.inbox / "a.png")

    def test_file_held_exclusively_by_exporter_is_retried_not_failed(self):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        target = self.inbox / "export.png"
        handle = kernel32.CreateFileW(str(target), 0x40000000, 0, None, 2, 0x80, None)  # GENERIC_WRITE, no sharing
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            self.watch.status()
            self.now[0] = 3
            status = self.watch.status()
            self.assertFalse(status["ready"])
            self.assertEqual(status["errors"], [])
        finally:
            kernel32.CloseHandle(handle)
        target.write_bytes(self.PNG)
        self.watch.status()
        self.now[0] = 6
        self.assertTrue(self.watch.status()["ready"])

    def test_inbox_that_is_a_junction_is_rejected(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        junction(link, real)
        status = WatchFolder(link, self.root / "retained").status()
        self.assertFalse(status["ready"])
        self.assertIn("link", status["error"])

    def test_file_symlink_is_not_imported_when_privileged(self):
        target = self.root / "target.png"
        target.write_bytes(self.PNG)
        try:
            (self.inbox / "linked.png").symlink_to(target)
        except OSError:
            self.skipTest("this account cannot create symlinks (needs Developer Mode or elevation)")
        self.watch.status()
        self.now[0] = 3
        status = self.watch.status()
        self.assertFalse(status["ready"])
        self.assertEqual(len(status["errors"]), 1)
        self.assertEqual(target.read_bytes(), self.PNG)

    def test_missing_inbox_is_reported_not_created(self):
        missing = self.root / "absent"
        status = WatchFolder(missing, self.root / "retained").status()
        self.assertFalse(status["ready"])
        self.assertIn("does not exist", status["error"])
        self.assertFalse(missing.exists())


class PlatformPathTests(unittest.TestCase):
    def test_env_overrides_and_defaults(self):
        from shp_portrait_tools.shared import platform_paths as P
        with patch.dict(os.environ, {"WATCH_DIRECTORY": "generic", "SHPC_WATCH_DIRECTORY": ""}, clear=False):
            self.assertEqual(P.watch_directory("DNB_WATCH_DIRECTORY", "a", "b"), Path("generic"))
            os.environ["DNB_WATCH_DIRECTORY"] = "specific"
            self.assertEqual(P.watch_directory("DNB_WATCH_DIRECTORY", "a", "b"), Path("specific"))
        with patch.dict(os.environ, {"DNB_DATA": str(Path("custom") / "d")}):
            self.assertEqual(P.module_data("DNB_DATA", "dnb"), Path("custom") / "d")

    @unittest.skipUnless(WINDOWS, "Windows defaults")
    def test_windows_defaults_use_localappdata(self):
        from shp_portrait_tools.shared import platform_paths as P
        env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local", "APPDATA": r"C:\Users\x\AppData\Roaming"}
        with patch.dict(os.environ, env):
            for name in ("DNB_DATA", "WATCH_DIRECTORY", "DNB_WATCH_DIRECTORY"):
                os.environ.pop(name, None)
            self.assertEqual(P.module_data("DNB_DATA", "dnb"), Path(env["LOCALAPPDATA"]) / "shp-photocraft-tools" / "dnb")
            self.assertEqual(P.global_config_dir(), Path(env["APPDATA"]) / "shp-photocraft-tools")
            self.assertEqual(P.watch_directory("DNB_WATCH_DIRECTORY", "w", "w"),
                             Path(env["LOCALAPPDATA"]) / "shp-photocraft-tools" / "w")

if __name__ == "__main__":
    unittest.main()
