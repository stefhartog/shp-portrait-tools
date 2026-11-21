"""Windows launcher configuration and shortcut-like fresh-process checks."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shp_portrait_tools import launcher


@unittest.skipUnless(sys.platform == "win32", "Windows launcher configuration")
class WindowsPhotoCraftSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / "models"
        self.models.mkdir()
        for name in launcher.MODEL_FILES:
            (self.models / name).touch()
        self.pc = {"port": 17878, "token_file": str(self.root / "token"),
                   "exchange": str(self.root / "exchange"), "read_root": str(self.root / "exchange"),
                   "executable": str(self.root / "photocraft.exe"), "editor_config": str(self.root / "editor")}
        self.config = self.root / "launcher.json"
        self.config.write_text(json.dumps({"models": str(self.models), "photocraft": self.pc}))

    def test_persistence_and_fresh_process_precedence(self):
        self.assertEqual(launcher.read_config(self.config)["photocraft"], self.pc)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SHP_PC_", "DNB_PC_", "SHPC_PC_", "PHOTOCRAFT_"))}
        env["DNB_PC_PORT"] = "19000"
        env["PHOTOCRAFT_BIN"] = str(self.root / "override.exe")
        code = ("import json,sys; from pathlib import Path; "
                "from shp_portrait_tools.launcher import read_config,apply_photocraft_env; "
                "from shp_portrait_tools.shared.photocraft.bridge import detect_config; "
                "import os; apply_photocraft_env(read_config(Path(sys.argv[1]))); "
                "c=detect_config('DNB','dnb'); "
                "print(json.dumps({'port':c['port'],'token':str(c['token_file']),"
                "'root':str(c['read_root']),'exe':os.environ['PHOTOCRAFT_BIN'],"
                "'config':os.environ['PHOTOCRAFT_CONFIG_DIR']}))")
        result = subprocess.run([sys.executable, "-c", code, str(self.config)], cwd=self.root,
                                env=env, capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value, {"port": 19000, "token": self.pc["token_file"],
                                 "root": self.pc["read_root"], "exe": env["PHOTOCRAFT_BIN"],
                                 "config": self.pc["editor_config"]})

    def test_invalid_configuration_is_reported(self):
        self.pc["token_file"] = "relative"
        self.config.write_text(json.dumps({"models": str(self.models), "photocraft": self.pc}))
        with self.assertRaisesRegex(launcher.LaunchError, "Invalid saved PhotoCraft"):
            launcher.read_config(self.config)

    def test_explicit_shared_environment_wins(self):
        with patch.dict(os.environ, {"SHP_PC_PORT": "19999"}, clear=True):
            launcher.apply_photocraft_env({"photocraft": self.pc})
            self.assertEqual(os.environ["SHP_PC_PORT"], "19999")
