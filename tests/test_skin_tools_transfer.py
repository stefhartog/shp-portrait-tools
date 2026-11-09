"""Native PhotoCraft transfer regressions for combined and flattened skin tools."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.dnb import skintone as ST, dodge_burn as D, ui_server as U, frequency_separation as FS
from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge
from shp_photocraft_tools.shared.photocraft import ControlError


class StdioClient:
    def __init__(self, process):
        self.process = process

    def call(self, method, **params):
        self.process.stdin.write(json.dumps({"id": 1, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise ControlError("isolated PhotoCraft process closed stdout")
        reply = json.loads(line)
        if not reply["ok"]:
            raise ControlError(f"{method}: {reply.get('error')}")
        return reply.get("result")

    def run(self, command, **params):
        return self.call("engine.execute", command=command, params=params)


@unittest.skipUnless(shutil.which("photocraft-cli"), "photocraft-cli is not installed")
class SkinToolsTransferTests(unittest.TestCase):
    def setUp(self):
        runtime = tempfile.TemporaryDirectory(prefix="skin-tools-transfer-")
        self.addCleanup(runtime.cleanup)
        self.root = Path(runtime.name)
        self.bridge = Bridge(port=1, token_file=self.root / "unused",
                             exchange=self.root / "exchange", read_root=self.root)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PHOTOCRAFT_")}
        env["PHOTOCRAFT_CONFIG_DIR"] = str(self.root / "config")
        self.process = subprocess.Popen(
            ["photocraft-cli", "serve", "--automation-read-root", str(self.root),
             "--automation-write-root", str(self.bridge.exchange)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.close_process)
        self.bridge.client = StdioClient(self.process)
        self.bridge.headless = True
        self.client = self.bridge.client

    def close_process(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=20)
        if self.process.stdout and not self.process.stdout.closed:
            self.process.stdout.close()

    def new_target(self,profile="srgb"):
        self.client.call("doc.new", width=8, height=8, depth=16)
        self.client.run("edit.assignProfile", profile=profile)
        self.client.run("edit.fill", contents="color", color=[0.35, 0.5, 0.65, 1])
        target = self.bridge.active_doc()
        return target, self.bridge._target_profile(target)

    def save_pixels(self, name):
        self.client.call("doc.save", path=name)
        path = self.bridge.exchange / name
        pixels = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        path.unlink()
        return pixels

    def test_dnb_bands_only_are_one_masked_skintools_folder(self):
        target, icc = self.new_target()
        path,_=self.bridge.fetch(self.root/"band-source")
        source=cv2.imread(str(path),-1)[...,:3].astype(np.float32)/65535
        mask = np.full((8, 8), 255, np.uint8)
        band = np.full((8, 8, 3), 36044, np.uint16)
        result = self.bridge.send_skin_bands(
            [(2, band)], mask, target, source_icc=icc, band_mix=65)

        inspection = self.client.run("document.inspect")
        self.assertEqual(len(inspection["layers"]), 2)
        folder = inspection["layers"][0]
        self.assertEqual(folder["id"], result["layer"])
        self.assertEqual(folder["name"], "SkinTools")
        self.assertEqual([layer["name"] for layer in folder["children"]], ["D&B band 2"])
        self.assertEqual(folder["children"][0]["blend"], "Soft Light")
        self.assertTrue(folder["hasMask"])
        self.assertFalse(folder["children"][0]["hasMask"])
        self.assertAlmostEqual(folder["children"][0]["opacity"], 0.65, places=2)
        self.assertEqual(folder["blend"],"Pass Through")
        actual=self.save_pixels("band-result.png")[...,:3].astype(np.float32)/65535
        expected=source+(D.softlight(source,band.astype(np.float32)/65535)-source)*.65
        np.testing.assert_allclose(actual,expected,atol=3/65535)
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])

    def test_combined_fs_and_bands_share_one_folder_and_expected_stack(self):
        target, icc = self.new_target()
        mask = np.full((8, 8), 255, np.uint8)
        band_mask = np.full((8, 8), 128, np.uint8)
        band1 = np.full((8, 8, 3), 32768, np.uint16)
        band2 = np.full((8, 8, 3), 36044, np.uint16)
        layers = {name: np.full((8, 8, 3), value, np.uint16)
                  for name, value in (("color", 32768), ("blur", 32768), ("detail", 32768))}
        result = self.bridge.send_skintone(
            layers, ST.settings({}), mask, target, icc,
            bands=[(2, band2), (1, band1)], band_mask8=band_mask, band_mix=55,
            directional=(np.full((8,8,3),40000,np.uint16),band_mask,37),
            transfer={"format":"tiff","parallel":True})

        inspection = self.client.run("document.inspect")
        self.assertEqual(len(inspection["layers"]), 2)
        folder = inspection["layers"][0]
        self.assertEqual(folder["id"], result["layer"])
        self.assertEqual(folder["name"], "SkinTools")
        children = folder["children"]
        self.assertEqual([layer["name"] for layer in children],
                         ["D&B band 2", "D&B band 1", "Detail", "Hue", "Directional Blur", "Blur", "Color"])
        for layer in children[:2]:
            self.assertEqual(layer["blend"], "Soft Light")
            self.assertFalse(layer["hasMask"])
            self.assertAlmostEqual(layer["opacity"], 0.55, places=2)
        self.assertEqual(children[2]["blend"], "Linear Light")
        self.assertTrue(folder["hasMask"])
        self.assertFalse(children[3]["hasMask"])
        self.assertFalse(children[4]["hasMask"])
        self.assertAlmostEqual(children[4]["opacity"],.37,places=2)
        self.assertFalse(children[5]["hasMask"])
        self.assertFalse(children[6]["hasMask"])
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])
        self.client.call("doc.save", path="combined.png")
        with Image.open(self.bridge.exchange / "combined.png") as image:
            self.assertEqual(image.info["icc_profile"], icc)
        (self.bridge.exchange / "combined.png").unlink()

    def test_legacy_masks_remain_on_ingredients_not_folder(self):
        target, icc = self.new_target()
        mask = np.full((8, 8), 128, np.uint8)
        pixels = np.full((8, 8, 3), 32768, np.uint16)
        self.bridge.send_skintone(
            {name: pixels for name in ("color", "blur", "detail")},
            ST.settings({}), mask, target, icc, mask_placement="layers",
            directional=(pixels, mask, 37), bands=[(1, pixels)], band_mask8=mask)
        group = self.client.run("document.inspect")["layers"][0]
        self.assertFalse(group["hasMask"])
        self.assertEqual({layer["name"] for layer in group["children"] if layer["hasMask"]},
                         {"D&B band 1", "Hue", "Directional Blur", "Blur"})
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])
        self.client.run("file.close", document=target["index"])
        target, icc = self.new_target()
        self.bridge.send_skin_bands([(1, pixels)], mask, target, source_icc=icc,
                                    mask_placement="layers")
        group = self.client.run("document.inspect")["layers"][0]
        self.assertFalse(group["hasMask"])
        self.assertTrue(group["children"][0]["hasMask"])

    def test_send_result_is_full_rgb16_normal_layer_without_a_folder(self):
        target, icc = self.new_target()
        corrected = np.empty((8, 8, 3), np.uint16)
        corrected[..., 0] = 10000
        corrected[..., 1] = 30000
        corrected[..., 2] = 55000
        result = self.bridge.send_result(corrected, target, icc)

        inspection = self.client.run("document.inspect")
        self.assertEqual(len(inspection["layers"]), 2)
        layer = inspection["layers"][0]
        self.assertEqual(layer["id"], result["layer"])
        self.assertEqual(layer["name"], "Corrected Result")
        self.assertEqual(layer["blend"], "Normal")
        self.assertFalse(layer["hasMask"])
        self.client.run("layer.setProps", layer=inspection["layers"][1]["id"], visible=False)
        self.client.call("doc.save", path="flattened.png")
        path = self.bridge.exchange / "flattened.png"
        np.testing.assert_array_equal(cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[..., :3], corrected)
        with Image.open(path) as image:
            self.assertEqual(image.info["icc_profile"], icc)
        path.unlink()
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])

    def test_send_result_rejects_non_rgb16_missing_icc_and_stale_dimensions(self):
        target, icc = self.new_target()
        rgb16 = np.full((8, 8, 3), 32768, np.uint16)
        with self.assertRaisesRegex(ControlError, "full RGB16"):
            self.bridge.send_result(np.full((8, 8), 32768, np.uint16), target, icc)
        with self.assertRaisesRegex(ControlError, "ICC profile"):
            self.bridge.send_result(rgb16, target, None)
        with self.assertRaisesRegex(ControlError, "document size changed"):
            self.bridge.send_result(np.full((7, 8, 3), 32768, np.uint16), target, icc)
        self.assertEqual(len(self.client.run("document.inspect")["layers"]), 1)
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])

    def test_flattened_profiles_and_uncertain_final_reply(self):
        corrected=np.full((8,8,3),23456,np.uint16)
        for profile in ("srgb","display-p3","prophoto-compat"):
            target,icc=self.new_target(profile)
            self.bridge.send_result(corrected,target,icc)
            self.client.call("doc.save",path="profile-result.png")
            path=self.bridge.exchange/"profile-result.png"
            with Image.open(path) as image:
                self.assertEqual(image.info["icc_profile"],icc)
            np.testing.assert_array_equal(cv2.imread(str(path),-1)[...,:3],corrected)
            path.unlink()
            self.client.run("file.close",document=target["index"])
        target,icc=self.new_target()
        attempted=[]
        def lost_reply(command,**params):
            attempted.append(command)
            self.client.run(command,**params)
            raise ControlError("simulated lost final reply")
        self.client.run_once=lost_reply
        with self.assertRaisesRegex(ControlError,"inspect the destination before retrying"):
            self.bridge.send_result(corrected,target,icc)
        self.assertEqual(attempted,["layer.copyToDocument"])
        self.assertEqual(len(self.client.run("document.inspect")["layers"]),2)
        self.assertEqual(len(self.bridge.session()["documents"]),1)
        self.assertEqual(list(self.bridge.exchange.iterdir()),[])

    def test_server_combined_layers_preserve_partial_fs_mix_before_bands(self):
        from test_dnb import PARAMS
        target,icc=self.new_target()
        source=np.random.default_rng(77).uniform(.3,.7,(8,8,3)).astype(np.float32)
        self.bridge.send_result(FS.rgb16(source),target,icc)
        source=FS.rgb16(source).astype(np.float32)/65535
        mask=np.tile(np.arange(8,dtype=np.float32)/7,(8,1))
        req={**PARAMS,"mode":"combine","S":181,"skintone":{"on":False},
             "frequency":{"radius":2,"blur_radius":4,"blur_strength":67},
             "combine":{"fs_mix":35,"dnb_mix":65},
             "second_band":{"on":True,"invert":True,"strength":.3}}
        p=U.parse_params(req)
        with patch.dict(U.STATE,img=source,source_img=source,source_icc=icc,source_opaque=True,
                        skintone_error=None), \
             patch.dict(U.BRIDGE,b=self.bridge,doc=target), \
             patch.object(U,"full_skin_mask",return_value=mask):
            _,_,expected=U.combined_result(source,mask,p)
            U.send_combined_layers(p)
        actual=self.save_pixels("server-combined.png")[...,:3].astype(np.float32)/65535
        np.testing.assert_allclose(actual,expected,atol=.002)
        folder=self.client.run("document.inspect")["layers"][0]
        self.assertEqual(folder["name"],"SkinTools")
        self.assertEqual([layer["name"] for layer in folder["children"]],
                         ["D&B band 2","D&B band 1","Detail","Blur","Color"])
        self.assertEqual(len(self.bridge.session()["documents"]),1)
        self.assertEqual(list(self.bridge.exchange.iterdir()),[])
