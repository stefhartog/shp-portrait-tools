"""Local-source exports without GUI or live services."""

from concurrent.futures import ThreadPoolExecutor
import io
import itertools
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np
from PIL import Image

from shp_portrait_tools.modules.skin import ui_server as S
try:
    from shp_portrait_tools import supervisor
except ImportError:  # supervisor is Linux-only (fcntl)
    supervisor = None
from shp_portrait_tools.shared import local_output
import test_dnb as T
from winlinks import directory_alias, file_symlink


class LocalResultTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    post = T.DnbTests.post

    def send(self, body):
        request = Request(self.base + "/api/send-result", json.dumps(body).encode(),
                          {"Content-Type": "application/json", "Origin": self.base})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_one_full_resolution_file_per_mode_with_profile_and_precision(self):
        pixels = np.round(T.synthetic_image() * 65535).astype(np.uint16)
        self.image.write_bytes(S.PCB.tagged_rgb16(pixels, S.SRGB_ICC))
        loaded = self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(loaded["source_kind"], "local")
        for mode, suffix in (("dnb", "shpdnb"), ("mask", "shpskinmask"),
                             ("frequency", "shpfs"), ("combine", "shpcombine")):
            params = {**T.PARAMS, "mode": mode, "view": "detail", "generation": loaded["generation"],
                      "combine": {"fs_mix": 40, "dnb_mix": 60},
                      "path": str(self.root / "ignored" / "not-the-source.png")}
            p = S.parse_params(params)
            if mode == "dnb":
                _, expected, _ = S.dnb_full_result(p)
            else:
                _, total, _ = S.tool_mask(S.STATE["img"], S.STATE["masks"], p, p["S"])
                mask = np.ones(pixels.shape[:2], np.float32) if total is None else total
                if mode == "frequency":
                    layers = S.skintone_layers(S.STATE["source_img"], mask, p)
                    _, expected = S.ST.composite(layers, mask, p["skintone"], p["frequency"])
                elif mode == "combine":
                    _, _, expected = S.combined_result(S.STATE["source_img"], mask, p)
                else:
                    expected = mask
            with self.subTest(mode=mode), patch.object(S, "api_pc_status") as pc:
                saved = self.send(params)
                pc.assert_not_called()  # A local source stays local even if PhotoCraft connects.
                output = Path(saved["path"])
                self.assertEqual(output, self.root / f"synthetic_{suffix}_001.png")
                decoded = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
                self.assertEqual(decoded.shape[:2], pixels.shape[:2])
                if mode == "mask":
                    self.assertEqual(decoded.ndim, 2)
                    self.assertEqual(decoded.dtype, np.uint8)
                    np.testing.assert_array_equal(decoded, S.u8(expected))
                else:
                    self.assertEqual(decoded.dtype, np.uint16)
                    np.testing.assert_array_equal(decoded[..., :3], S.FS.rgb16(expected))
                    self.assertEqual(decoded.shape[2], 3)
                    with Image.open(output) as image:
                        self.assertEqual(image.info["icc_profile"], S.SRGB_ICC)
                    result, icc = S.skin_result(p)
                    np.testing.assert_array_equal(S.FS.rgb16(result), decoded)
                    self.assertEqual(icc, S.SRGB_ICC)
                bundle = self.post("/api/save", params)
                artifact = {"dnb": "synthetic_1_result.png", "mask": "skinmask.png",
                            "frequency": "result.png", "combine": "result.png"}[mode]
                self.assertIn(artifact, bundle["files"])
                np.testing.assert_array_equal(
                    cv2.imread(str(Path(bundle["dir"]) / artifact), cv2.IMREAD_UNCHANGED), decoded,
                )
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()),
                         ["synthetic.png", "synthetic_shpcombine_001.png", "synthetic_shpdnb_001.png",
                          "synthetic_shpfs_001.png", "synthetic_shpskinmask_001.png"])
        np.testing.assert_array_equal(cv2.imread(str(self.image), cv2.IMREAD_UNCHANGED)[..., :3], pixels)

    def test_provenance_disconnect_stale_and_origin_guards(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        original_pixels = S.STATE["img"]
        with self.assertRaises(HTTPError):
            self.post("/api/load", {"path": str(self.root / "missing.png")})
        self.assertIs(S.STATE["img"], original_pixels)
        self.assertEqual(S.STATE["source_kind"], "local")
        with self.assertRaises(HTTPError) as error:
            self.post("/api/send-result", T.PARAMS)
        self.assertEqual(error.exception.code, 403)
        with patch.object(S, "write_local_result") as writer:
            with self.assertRaisesRegex(ValueError, "loaded image changed"):
                S.api_send_result({**T.PARAMS, "generation": loaded["generation"] - 1})
            writer.assert_not_called()
        with patch.object(S, "bridge") as bridge:
            bridge.return_value.fetch.return_value = (self.image, {"name": "PC source"})
            fetched = self.post("/api/pc/fetch", {})
        self.assertEqual(fetched["source_kind"], "photocraft")
        self.assertEqual(S.STATE["source_kind"], "photocraft")
        with patch.object(S, "api_pc_status", return_value={"connected": False}), \
                patch.object(S, "write_local_result") as writer:
            with self.assertRaisesRegex(ValueError, "disconnected"):
                S.api_send_result(T.PARAMS)
            writer.assert_not_called()
        with patch.object(S, "api_pc_status", return_value={"connected": True}), \
                patch.object(S, "api_pc_send", return_value={"sent": True}) as sender:
            self.assertEqual(S.api_send_result(T.PARAMS), {"sent": True})
            sender.assert_called_once_with(T.PARAMS)
        self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(S.STATE["source_kind"], "local")
        self.assertIsNone(S.BRIDGE["doc"])
        with patch.object(S.cv2, "imencode", return_value=(False, None)):
            with self.assertRaisesRegex(ValueError, "could not encode"):
                S.api_send_result({**T.PARAMS, "mode": "mask"})
        self.assertFalse(list(self.root.glob("*_shpskinmask_*")))

    def test_editor_send_routes_forward_result_or_mask_to_the_loaded_document(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        pixels = T.synthetic_image()
        mask = pixels[..., 0]
        document = {"name": "captured document"}
        request = {**T.PARAMS, "generation": loaded["generation"]}
        for editor in ("photoshop", "photocraft"):
            with self.subTest(editor=editor), \
                 patch.dict(S.STATE, source_kind=editor, icc=S.SRGB_ICC, source_icc=S.SRGB_ICC), \
                 patch.dict(S.BRIDGE, doc=document), patch.dict(S.EDITOR, selected=editor), \
                 patch.object(S, "api_pc_status", return_value={"connected": True}), \
                 patch.object(S, "bridge") as select_bridge, \
                 patch.object(S, "skin_result", return_value=(pixels, S.SRGB_ICC)), \
                 patch.object(S, "full_skin_mask", return_value=mask), \
                 patch.object(S, "write_local_result") as local:
                adapter = select_bridge.return_value
                adapter.send_result.return_value = {"kind": "result"}
                adapter.send_mask.return_value = {"kind": "mask"}
                for mode, kind in (("dnb", "result"), ("mask", "mask")):
                    response = self.send({**request, "mode": mode})
                    self.assertEqual(
                        set(response), {"sent", "kind", "mask_precache", "layer_precache"},
                    )
                    self.assertIs(response["sent"], True)
                    self.assertEqual(response["kind"], kind)
                for method, expected in ((adapter.send_result, S.FS.rgb16(pixels)),
                                         (adapter.send_mask, S.u8(mask))):
                    method.assert_called_once()
                    np.testing.assert_array_equal(method.call_args.args[0], expected)
                    self.assertIs(method.call_args.args[1], document)
                    self.assertEqual(method.call_args.kwargs, {"source_icc": S.SRGB_ICC})
                local.assert_not_called()

    def test_global_formats_compression_alpha_and_mask_exemption(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        result = T.synthetic_image()
        mask = np.ones(result.shape[:2], np.float32)
        with patch.object(S, "dnb_full_result", return_value=([], result, mask)), \
             patch.object(S, "full_skin_mask", return_value=mask), \
             patch.object(S, "combined_result", return_value=(None, None, result)):
            for fmt, compressed, alpha in itertools.product(("png", "tiff"), (False, True), (False, True)):
                options = {"format":fmt, "compressed":compressed, "opaque_alpha":alpha, "parallel":True}
                self.post("/api/global-settings", {"layer_transfer":options})
                for mode in ("dnb", "combine"):
                    with self.subTest(format=fmt, compressed=compressed, alpha=alpha, mode=mode):
                        params = {**T.PARAMS, "mode":mode, "generation":loaded["generation"],
                                  "frequency_transfer":{"format":"png"}}
                        output = Path(self.send(params)["path"])
                        self.assertEqual(output.suffix, "." + fmt)
                        decoded = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
                        self.assertEqual(decoded.dtype, np.uint16)
                        self.assertEqual(decoded.shape[2], 4 if alpha else 3)
                        np.testing.assert_array_equal(decoded[..., :3], S.FS.rgb16(result))
                        with Image.open(output) as image:
                            self.assertEqual(image.info["icc_profile"], S.SRGB_ICC)
                            if fmt == "tiff":
                                self.assertEqual(image.tag_v2[259], 8 if compressed else 1)
                output = Path(self.send({**T.PARAMS, "mode":"mask"})["path"])
                self.assertEqual(output.suffix, ".png")
                self.assertEqual(cv2.imread(str(output), -1).dtype, np.uint8)
            saved = self.post("/api/save", {**T.PARAMS, "mode":"combine"})
            self.assertIn("result.tiff", saved["files"])
            settings = json.loads((Path(saved["dir"]) / "settings.json").read_text())
            self.assertNotIn("frequency_transfer", settings)
            self.assertEqual(settings["output_settings"], options)

    def test_collision_symlink_concurrency_and_failed_write_cleanup(self):
        victim = self.root / "keep.png"
        victim.write_bytes(b"untouched")
        file_symlink(self.root / "synthetic_shpdnb_001.png", victim)
        (self.root / "synthetic_shpdnb_002.png").write_bytes(b"also untouched")
        with ThreadPoolExecutor(max_workers=6) as pool:
            outputs = list(pool.map(lambda i: S.write_local_result(self.image, "shpdnb", str(i).encode()),
                                    range(6)))
        self.assertEqual(len(set(outputs)), 6)
        self.assertEqual(victim.read_bytes(), b"untouched")
        self.assertEqual((self.root / "synthetic_shpdnb_002.png").read_bytes(), b"also untouched")
        file_symlink(self.root / "synthetic_shpdnb_001.tiff", victim)
        output = Path(S.write_local_result(self.image, "shpdnb", b"tiff", extension=".tiff"))
        self.assertEqual(output.name, "synthetic_shpdnb_002.tiff")
        self.assertEqual(victim.read_bytes(), b"untouched")
        with self.assertRaisesRegex(ValueError, "extension"):
            S.write_local_result(self.image, "shpdnb", b"bad", extension="/escape")
        before = set(self.root.iterdir())
        with patch.object(local_output.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                S.write_local_result(self.image, "shpfs", b"partial")
        self.assertEqual(set(self.root.iterdir()), before)
        with patch.object(local_output.os, "open", side_effect=PermissionError("read-only directory")):
            with self.assertRaisesRegex(PermissionError, "read-only directory"):
                S.write_local_result(self.image, "shpfs", b"data")
        self.assertEqual(set(self.root.iterdir()), before)
        other = self.root / "other"
        other.mkdir()
        alias = self.root / "alias"
        directory_alias(alias, other)
        original_directory = self.root.stat()
        with self.assertRaisesRegex(ValueError, "source folder changed"):
            S.write_local_result(alias / self.image.name, "shpfs", b"data",
                                 (original_directory.st_dev, original_directory.st_ino))
        self.assertEqual(list(other.iterdir()), [])


