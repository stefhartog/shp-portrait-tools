"""Model download for the installers: pinned files, SHA-256 checks, nothing half-written left behind."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shp_portrait_tools import fetch_models as F


class FakeResponse(io.BytesIO):
    def __exit__(self, *args):
        self.close()
        return False


class FetchModelsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dest = Path(temp.name) / "models"
        self.bodies = {url: name.encode() * 3 for name, (url, _) in F.MODELS.items()}
        self.requested = []

    def opener(self, request, timeout):
        self.requested.append(request.full_url)
        return FakeResponse(self.bodies[request.full_url])

    def pinned(self):
        return {name: (url, hashlib.sha256(self.bodies[url]).hexdigest()) for name, (url, _) in F.MODELS.items()}

    def test_downloads_verifies_and_skips_verified_files(self):
        with patch.object(F, "MODELS", self.pinned()):
            F.fetch_onnx(self.dest, self.opener, log=lambda *_: None)
            self.assertEqual(sorted(p.name for p in self.dest.iterdir()), sorted(F.MODELS))
            self.assertEqual(len(self.requested), 3)
            F.fetch_onnx(self.dest, self.opener, log=lambda *_: None)
            self.assertEqual(len(self.requested), 3)  # all present and verified: no new download

    def test_checksum_mismatch_keeps_nothing_and_replaces_a_bad_file(self):
        models = self.pinned()
        name = "yunet.onnx"
        url, good = models[name]
        with patch.object(F, "MODELS", {name: (url, "0" * 64)}):
            with self.assertRaisesRegex(ValueError, "does not match"):
                F.fetch_onnx(self.dest, self.opener, log=lambda *_: None)
            self.assertEqual(list(self.dest.iterdir()), [])
        (self.dest / name).write_bytes(b"corrupt")
        with patch.object(F, "MODELS", {name: (url, good)}):
            F.fetch_onnx(self.dest, self.opener, log=lambda *_: None)
        self.assertEqual(F.sha256(self.dest / name), good)

    def test_pins_match_models_md(self):
        text = (Path(__file__).resolve().parents[1] / "MODELS.md").read_text(encoding="utf-8")
        for name, (url, digest) in F.MODELS.items():
            self.assertIn(url, text)
            self.assertIn(f"{digest}  {name}", text)


if __name__ == "__main__":
    unittest.main()
