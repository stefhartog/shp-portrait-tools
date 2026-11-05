"""Verify the unchanged, separately installed ComfyUI node without importing torch."""

import ast
import hashlib
import unittest
from pathlib import Path


class ComfyNodeDistributionTests(unittest.TestCase):
    def test_preserved_source_and_registration(self):
        source = (Path(__file__).resolve().parents[1] /
                  "comfyui/custom_nodes/seam-color-correct/__init__.py").read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "f14e335bf4ffb87117f767392bb23adef07441c72678b426ca362fd362c161e8")
        tree = ast.parse(source)
        mappings = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS"
                                for target in node.targets))
        self.assertEqual([ast.literal_eval(key) for key in mappings.keys], ["SeamColorCorrect"])
