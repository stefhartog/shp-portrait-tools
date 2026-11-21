"""One-time move from the pre-rename `shp-photocraft-tools` folders. Uses only temporary folders."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shp_portrait_tools import launcher as L
from shp_portrait_tools.shared import platform_paths as P
from shp_portrait_tools.shared.photocraft.bridge import detect_config


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_base, self.config_base = P._data_base, P._config_base
        env = {"LOCALAPPDATA": str(self.root / "local"), "APPDATA": str(self.root / "roaming"),
               "XDG_CONFIG_HOME": str(self.root / "roaming"), "XDG_STATE_HOME": str(self.root / "state"),
               "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home")}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Linux data lives under ~/.local/share; point it at the same temporary tree.
        base = patch.object(P, "_data_base", lambda: self.root / "local")
        base.start()
        self.addCleanup(base.stop)
        self.old_data = self.root / "local" / P.LEGACY_APP
        self.new_data = self.root / "local" / P.APP
        self.old_config = self.root / "roaming" / P.LEGACY_APP
        self.new_config = self.root / "roaming" / P.APP
        self.messages = []

    def legacy_install(self):
        for path, content in {
            "dnb/models/yunet.onnx": "model", "dnb/presets.json": "{}", "dnb/inbox/a.png": "png",
            "state/shell.log": "log", "webview/profile/x": "x",
            "crop/settings.json": "ai fill", "photocraft-shortcut/run.pyw": "pyw", "release-updates/u/x": "x",
        }.items():
            file = self.old_data / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
        launcher = {"models": str(self.old_data / "dnb" / "models"), "ports": {"crop": 18768, "dnb": 18765},
                    "photocraft": {"token_file": str(self.root / "control" / "token")}}
        for path, content in {
            "launcher.json": json.dumps(launcher), "launcher.json.lock": "", "launcher.json.old.bak": "{}",
            "settings.json": '{"theme":"dark"}', "photoshop/credential.dpapi": "secret",
            "shp-photocraft-tools.ico": "ico", "crop/exchange/x": "x", "dnb/exchange/y": "y",
            "dnb/photocraft.log": "pc",
        }.items():
            file = self.old_config / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)

    def migrate(self):
        P.migrate_legacy_roots(self.messages.append)

    def test_fresh_install_uses_new_names(self):
        self.assertEqual(P.data_root(), self.new_data)
        self.assertEqual(P.global_config_dir(), self.new_config)
        self.migrate()
        self.assertFalse(self.new_data.exists())
        self.assertEqual(self.messages, [])

    def test_unmigrated_install_keeps_using_old_folders(self):
        self.legacy_install()
        self.assertEqual(P.data_root(), self.old_data)
        self.assertEqual(P.global_config_dir(), self.old_config)
        # The Skin Tools subfolder was `dnb` before the module rename; it is still found.
        self.assertEqual(P.module_data("SKIN_DATA_UNSET", "skin"), self.old_data / "dnb")

    def test_moves_only_app_owned_entries_and_rewrites_models_path(self):
        self.legacy_install()
        self.migrate()
        self.assertEqual(sorted(p.name for p in self.new_data.iterdir()), ["skin", "state", "webview"])
        self.assertEqual((self.new_data / "skin" / "inbox" / "a.png").read_text(), "png")
        self.assertEqual(P.module_data("SKIN_DATA_UNSET", "skin"), self.new_data / "skin")
        self.assertEqual(sorted(p.name for p in self.old_data.iterdir()),
                         sorted(["crop", "photocraft-shortcut", "release-updates", P.MIGRATION_NOTE]))
        self.assertEqual(sorted(p.name for p in self.new_config.iterdir()),
                         ["launcher.json", "launcher.json.lock", "launcher.json.old.bak", "photoshop",
                          "settings.json", "shp-photocraft-tools.ico"])
        self.assertEqual(sorted(p.name for p in self.old_config.iterdir()), sorted(["crop", "dnb", P.MIGRATION_NOTE]))
        config = json.loads((self.new_config / "launcher.json").read_text())
        self.assertEqual(config["models"], str(self.new_data / "skin" / "models"))
        self.assertEqual(config["photocraft"]["token_file"], str(self.root / "control" / "token"))
        self.assertEqual(P.data_root(), self.new_data)
        self.assertEqual(P.global_config_dir(), self.new_config)
        self.assertTrue(any("Moved" in message for message in self.messages))
        # Running again is a no-op.
        before = sorted(str(p) for p in self.root.rglob("*"))
        self.migrate()
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)

    @unittest.skipUnless(sys.platform == "win32", "PhotoCraft folders are per-OS")
    def test_photocraft_control_folders_keep_old_name(self):
        self.legacy_install()
        self.migrate()
        with patch.dict(os.environ, {}, clear=False):
            for key in ("DNB_PC_TOKEN_FILE", "SHP_PC_TOKEN_FILE", "DNB_PC_EXCHANGE", "SHP_PC_EXCHANGE"):
                os.environ.pop(key, None)
            config = detect_config("DNB", "dnb")
        self.assertEqual(config["config_dir"], self.old_config / "dnb")
        self.assertEqual(config["exchange"], self.old_config / "dnb" / "exchange")

    def test_failed_move_restores_everything(self):
        self.legacy_install()
        real_rename = os.rename

        def flaky(source, target):
            if Path(source).name == "webview" and Path(source).parent == self.old_data:
                raise PermissionError("in use")
            return real_rename(source, target)

        before = sorted(str(p) for p in self.root.rglob("*"))
        with patch.object(P.os, "rename", side_effect=flaky):
            self.migrate()
        self.assertFalse(self.new_data.exists())
        self.assertEqual(P.data_root(), self.old_data)
        self.assertTrue(any("Kept using" in message and "webview" in message for message in self.messages))
        # The data stayed together in the old folder; only its Skin Tools subfolder took the new name.
        self.assertTrue((self.old_data / "skin" / "presets.json").is_file())
        config = json.loads((self.new_config / "launcher.json").read_text())
        self.assertEqual(config["models"], str(self.old_data / "skin" / "models"))
        old_tree = sorted(p.replace(str(self.old_data / "dnb"), str(self.old_data / "skin"))
                          for p in before if p.startswith(str(self.old_data) + os.sep))
        self.assertEqual(sorted(str(p) for p in self.old_data.rglob("*")), old_tree)

    def test_skin_subfolder_rename_in_an_already_renamed_install(self):
        (self.new_data / "dnb" / "models").mkdir(parents=True)
        self.new_config.mkdir(parents=True)
        (self.new_config / "launcher.json").write_text(json.dumps(
            {"models": str(self.new_data / "dnb" / "models"), "ports": {"dnb": 18765}}))
        self.assertEqual(P.module_data("SKIN_DATA_UNSET", "skin"), self.new_data / "dnb")
        self.migrate()
        self.assertEqual([p.name for p in self.new_data.iterdir()], ["skin"])
        config = json.loads((self.new_config / "launcher.json").read_text())
        self.assertEqual(config["models"], str(self.new_data / "skin" / "models"))
        self.assertEqual(config["ports"], {"dnb": 18765})  # launcher.read_config maps the old key
        refused = self.new_data / "skin"
        refused.rename(self.new_data / "dnb")
        with patch.object(P.os, "rename", side_effect=PermissionError("in use")):
            self.migrate()
        self.assertTrue((self.new_data / "dnb").is_dir())
        self.assertTrue(any("could not rename" in message for message in self.messages))

    def test_existing_new_folder_is_never_overwritten(self):
        self.legacy_install()
        self.new_data.mkdir(parents=True)
        (self.new_data / "keep.txt").write_text("new")
        self.migrate()
        self.assertEqual([p.name for p in self.new_data.iterdir()], ["keep.txt"])
        self.assertTrue((self.old_data / "dnb").is_dir())

    def test_launcher_follows_a_shortcut_that_still_names_the_old_config(self):
        self.legacy_install()
        old_config = self.old_config / "launcher.json"
        with patch.dict(os.environ, {"SHP_TOOLS_DATA": ""}):
            resolved = L.prepare_folders(old_config)
        self.assertEqual(resolved, self.new_config / "launcher.json")
        self.assertTrue(resolved.is_file())
        unrelated = self.root / "elsewhere" / "launcher.json"
        self.assertEqual(L.prepare_folders(unrelated), unrelated)


if __name__ == "__main__":
    unittest.main()
