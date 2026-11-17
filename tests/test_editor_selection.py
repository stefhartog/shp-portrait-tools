"""Editor preferences use disposable settings; no real editor or user configuration."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shp_photocraft_tools.modules.dnb import ui_server as U


class EditorSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = self.root / "settings.json"
        self.photocraft = object()
        self.broker = Mock()
        self.broker.connected.return_value = False
        for item in (
            patch.object(U, "GLOBAL_SETTINGS", self.settings),
            patch.object(U, "PRESETS", self.root / "presets.json"),
            patch.dict(U.EDITOR, selected="photocraft", photoshop=None, broker=None),
            patch.dict(U.BRIDGE, b=self.photocraft, doc=None),
            patch.object(U, "PhotoshopBroker", return_value=self.broker),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_choice_survives_restart_without_restoring_document_or_replacing_photocraft(self):
        self.settings.write_text(json.dumps({
            "custom": {"untouched": [1, 2]},
            "mask_placement": "layers",
            "layer_transfer": {"format": "png", "exclude_opaque_alpha": True},
        }))
        U.api_editor({"editor": "photoshop"})
        U.api_global_settings({"layer_transfer": {"format": "tiff"}})
        U.EDITOR["selected"] = "photocraft"
        U.EDITOR["broker"] = None
        U.BRIDGE["doc"] = {"index": 7}
        before = self.settings.read_bytes()
        U.restore_editor_selection()
        self.assertEqual(U.api_editor({}), {"editor": "photoshop", "photoshop_hue_native": True, "photoshop_png_capture": True})
        self.assertIsNone(U.EDITOR["broker"])
        self.assertIsNone(U.BRIDGE["doc"])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(U.api_pc_status({})["code"], "photoshop")
        self.assertEqual(U.EDITOR["selected"], "photoshop")
        U.api_editor({"editor": "photocraft"})
        U.EDITOR["selected"] = "photoshop"
        U.restore_editor_selection()
        self.assertIs(U.bridge(), self.photocraft)
        saved = json.loads(self.settings.read_text())
        self.assertEqual(saved["source_editor"], "photocraft")
        self.assertEqual(saved["custom"], {"untouched": [1, 2]})
        self.assertEqual(saved["mask_placement"], "layers")
        self.assertEqual(saved["layer_transfer"]["format"], "tiff")

    def test_legacy_or_missing_selection_defaults_to_photocraft_without_writing(self):
        for contents in (None, '{"custom": "keep"}'):
            if contents is not None:
                self.settings.write_text(contents)
            U.EDITOR["selected"] = "photoshop"
            U.restore_editor_selection()
            self.assertEqual(U.api_editor({})["editor"], "photocraft")
            self.assertEqual(self.settings.read_text() if self.settings.exists() else None, contents)
        U.PhotoshopBroker.assert_not_called()

    def test_invalid_preferences_and_failed_save_preserve_config_and_active_target(self):
        for contents in ('[]', '{"source_editor": "unknown"}', '{invalid'):
            self.settings.write_text(contents)
            with self.assertRaises(ValueError):
                U.restore_editor_selection()
            with self.assertRaises(ValueError):
                U.api_editor({"editor": "photoshop"})
            self.assertEqual(self.settings.read_text(), contents)
            self.assertEqual(U.EDITOR["selected"], "photocraft")
        self.settings.unlink()
        U.read_global_settings()
        before = self.settings.read_bytes()
        U.BRIDGE["doc"] = {"index": 7}
        with patch.object(U, "write_global_settings", side_effect=OSError("read only")):
            with self.assertRaisesRegex(OSError, "read only"):
                U.api_editor({"editor": "photoshop"})
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(U.EDITOR["selected"], "photocraft")
        self.assertEqual(U.BRIDGE["doc"], {"index": 7})
        with self.assertRaisesRegex(ValueError, "editor must"):
            U.api_editor({"editor": "unknown"})
        self.assertEqual(self.settings.read_bytes(), before)

    def test_normal_service_entrypoint_restores_choice_before_listening(self):
        self.settings.write_text('{"source_editor": "photoshop", "custom": 1}')
        server = Mock(server_port=18765)

        def create_server(*args, **kwargs):
            self.assertEqual(U.EDITOR["selected"], "photoshop")
            self.assertIsNone(U.EDITOR["broker"])
            return server

        with patch("sys.argv", ["shp-photocraft-dnb", "--port", "18765"]), \
                patch.object(U, "BrowserServer", side_effect=create_server), \
                patch.object(U, "WatchFolder"), patch.object(U, "MASK_PRECACHE"), \
                patch("builtins.print"):
            U.main()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()
        U.PhotoshopBroker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
