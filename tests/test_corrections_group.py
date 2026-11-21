"""PhotoCraft send-layer grouping contracts without a live editor."""

import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np

from shp_portrait_tools.modules.dnb import skintone as ST
from shp_portrait_tools.modules.dnb.photocraft_bridge import Bridge


class RecordingClient:
    def __init__(self):
        self.calls = []
        self.selected = []
        self.layers = itertools.count(100)
        self.groups = itertools.count(500)
        self.groupings = []

    def run(self, command, **params):
        self.calls.append((command, params))
        if command == "layer.list":
            return {"layers": [{"id": next(self.layers)}]}
        if command == "layer.copyToDocument":
            return {"layers": [next(self.layers)]}
        if command == "layer.newAdjustmentLayer.colorLookup":
            return {"layer": next(self.layers)}
        if command == "layer.select":
            if params.get("mode") == "replace":
                self.selected.clear()
            self.selected.append(params["layer"])
        elif command == "layer.groupLayers":
            group = next(self.groups)
            self.groupings.append((params["name"], tuple(self.selected), group, params["mode"]))
            self.selected.clear()
            return {"layer": group}
        return {}


class CorrectionsGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bridge = Bridge(port=1, token_file=root / "unused",
                             exchange=root / "exchange", read_root=root)
        self.client = RecordingClient()
        self.bridge.client = self.client
        self.doc = {"index": 1, "name": "target", "width": 4, "height": 3}
        self.pixels = np.full((3, 4, 3), 32768, np.uint16)
        self.mask = np.full((3, 4), 128, np.uint8)
        self.mask_targets = []
        self.bridge._find = Mock(return_value=self.doc)
        self.bridge._activate = Mock()
        self.bridge._write_rgb = Mock()
        self.open_ids = itertools.count(10)
        self.bridge._open = Mock(side_effect=lambda _rel: next(self.open_ids))
        self.bridge._release_source = Mock()
        self.bridge._group_skin_mask = Mock(
            side_effect=lambda scratch, layer, mask, filename, scratch_docs:
                self.mask_targets.append((layer, mask.copy())))

    def assert_corrections_masked(self, root_name, expected_count=1):
        corrections = [group for group in self.client.groupings if group[0] == "Corrections"]
        roots = [group for group in self.client.groupings if group[0] == root_name]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(len(roots), 1)
        self.assertEqual(corrections[0][3], "Pass Through")
        self.assertEqual(len(corrections[0][1]), expected_count)
        self.assertIn(corrections[0][2], roots[0][1])
        self.assertEqual([target for target, _ in self.mask_targets], [corrections[0][2]])
        np.testing.assert_array_equal(self.mask_targets[0][1], self.mask)
        return corrections[0], roots[0]

    def assert_corrections_between_color_and_detail(self, corrections, root):
        moves = [params for command, params in self.client.calls if command == "layer.moveTo"]
        self.assertEqual(moves, [
            {"layer": corrections[2], "target": root[1][0], "position": "above"},
            {"layer": root[1][2], "target": corrections[2], "position": "above"},
        ])

    def test_frequency_send_masks_blur_in_corrections_between_color_and_detail(self):
        self.bridge.send_frequency(
            {"color": self.pixels, "blur": self.pixels, "detail": self.pixels},
            self.mask, self.doc, b"icc")
        corrections, root = self.assert_corrections_masked("SkinTools")
        self.assertEqual(len(root[1]), 3)
        self.assertEqual(root[1][1], corrections[2])
        self.assert_corrections_between_color_and_detail(corrections, root)
        self.assertEqual([name for name, *_ in self.client.groupings],
                         ["Corrections", "SkinTools"])

    def test_standalone_dnb_bands_get_a_masked_corrections_subgroup(self):
        self.bridge.send_skin_bands(
            [(1, self.pixels), (2, self.pixels)], self.mask, self.doc,
            source_icc=b"icc")
        corrections, root = self.assert_corrections_masked("SkinTools", expected_count=2)
        self.assertEqual(root[1], (corrections[2],))
        self.assertFalse(any(command == "layer.moveTo" for command, _ in self.client.calls))

    def test_combine_send_groups_effects_between_color_and_detail(self):
        self.bridge.send_skintone(
            {"color": self.pixels, "blur": self.pixels, "detail": self.pixels},
            ST.settings({}), self.mask, self.doc, b"icc")
        corrections, root = self.assert_corrections_masked("SkinTools", expected_count=2)
        self.assertEqual(root[1][1], corrections[2])
        self.assert_corrections_between_color_and_detail(corrections, root)
        self.assertEqual([name for name, *_ in self.client.groupings],
                         ["Corrections", "SkinTools"])

    def test_per_layer_setting_does_not_put_a_mask_on_either_group(self):
        self.bridge.send_frequency(
            {"color": self.pixels, "blur": self.pixels, "detail": self.pixels},
            self.mask, self.doc, b"icc", mask_placement="layers")
        corrections = next(group for group in self.client.groupings if group[0] == "Corrections")
        root = next(group for group in self.client.groupings if group[0] == "SkinTools")
        self.assertIn(corrections[2], root[1])
        self.assertEqual([target for target, _ in self.mask_targets], [corrections[1][0]])
        self.assertNotEqual(self.mask_targets[0][0], corrections[2])


if __name__ == "__main__":
    unittest.main()
