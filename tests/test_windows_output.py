"""Windows output safety checks with synthetic data in disposable directories."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shp_portrait_tools.shared import local_output, winfs

WINDOWS = sys.platform == "win32"


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


class PlatformPathTests(unittest.TestCase):
    def test_env_overrides_and_defaults(self):
        from shp_portrait_tools.shared import platform_paths as P
        with patch.dict(os.environ, {"DNB_DATA": str(Path("custom") / "old")}):
            os.environ.pop("SKIN_DATA", None)
            self.assertEqual(P.module_data("SKIN_DATA", "skin", "DNB_DATA"), Path("custom") / "old")
            os.environ["SKIN_DATA"] = str(Path("custom") / "d")
            self.assertEqual(P.module_data("SKIN_DATA", "skin", "DNB_DATA"), Path("custom") / "d")

    @unittest.skipUnless(WINDOWS, "Windows defaults")
    def test_windows_defaults_use_localappdata(self):
        from shp_portrait_tools.shared import platform_paths as P
        env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local", "APPDATA": r"C:\Users\x\AppData\Roaming"}
        with patch.dict(os.environ, env):
            for name in ("SKIN_DATA", "DNB_DATA"):
                os.environ.pop(name, None)
            self.assertEqual(P.module_data("SKIN_DATA", "skin", "DNB_DATA"),
                             Path(env["LOCALAPPDATA"]) / "shp-portrait-tools" / "skin")
            self.assertEqual(P.global_config_dir(), Path(env["APPDATA"]) / "shp-portrait-tools")

if __name__ == "__main__":
    unittest.main()
