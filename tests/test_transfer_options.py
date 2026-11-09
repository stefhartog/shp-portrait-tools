"""FS transfer codecs, option wiring and disposable real-engine imports."""
from concurrent.futures import Future
from io import BytesIO
import itertools
import shutil
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageCms

from shp_photocraft_tools.modules.dnb import ui_server as U, skintone as ST
from shp_photocraft_tools.modules.dnb.photocraft_bridge import tagged_rgb16, transfer_settings
import test_dnb_transfer as DT
import test_fs_pipeline as FP


def variants():
    for fmt, compressed, alpha in itertools.product(("png", "tiff"), (False, True), (False, True)):
        yield {"format": fmt, "compressed": compressed, "opaque_alpha": alpha, "parallel": True}


class CodecTests(unittest.TestCase):
    def test_every_codec_preserves_rgb16_profile_and_declares_alpha_compression(self):
        pixels = np.random.default_rng(129).integers(0, 65536, (25, 37, 3), dtype=np.uint16)
        pixels[0, :4] = np.array([0, 1, 256, 65535])[:, None]
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for options in variants():
            with self.subTest(options=options):
                data = tagged_rgb16(pixels, icc, options)
                with Image.open(BytesIO(data)) as image:
                    self.assertEqual(image.info["icc_profile"], icc)
                    if options["format"] == "tiff":
                        self.assertEqual(image.tag_v2[259], 8 if options["compressed"] else 1)
                        self.assertEqual(image.tag_v2[277], 4 if options["opaque_alpha"] else 3)
                        self.assertEqual(image.tag_v2.get(338), (2,) if options["opaque_alpha"] else None)
                actual = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
                np.testing.assert_array_equal(actual[..., :3], pixels)
                self.assertEqual(actual.shape[2], 4 if options["opaque_alpha"] else 3)
                if options["opaque_alpha"]:
                    self.assertTrue(np.all(actual[..., 3] == 65535))

    def test_validation_legacy_defaults_and_transfer_does_not_change_preview_or_mask_key(self):
        default = transfer_settings()
        self.assertEqual(default, {"format": "png", "compressed": True, "opaque_alpha": True, "parallel": True})
        for invalid in ([], {"format": "jpeg"}, {"compressed": 1}, {"opaque_alpha": None},
                        {"parallel": "false"}, {"compression": "zip"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                transfer_settings(invalid)
        p = U.parse_params({"mode": "frequency", "S": 181})
        self.assertEqual(p["frequency_transfer"], default)
        changed = {**p, "frequency_transfer": transfer_settings({"format": "tiff", "parallel": False})}
        with patch.dict(U.STATE, centre=(10,10), region_box=(0,0,20,20)):
            self.assertEqual(U.preview_key(p, "crop"), U.preview_key(changed, "crop"))
        self.assertEqual(U.mask_settings_key(p, None), U.mask_settings_key(changed, None))


class PipelineOptionTests(unittest.TestCase):
    setUp = FP.PipelineTests.setUp
    bridge = FP.PipelineTests.bridge
    params = FP.PipelineTests.params

    def test_all_options_keep_control_serialized_mask_png_and_cleanup(self):
        for options in variants():
            for parallel in (False, True):
                options = {**options, "parallel": parallel}
                with self.subTest(options=options):
                    b = self.bridge("options")
                    p = {**self.params(), "frequency_transfer": options}
                    with patch.object(U, "bridge", return_value=b), \
                            patch.object(U, "full_skin_mask", return_value=self.mask):
                        U.send_skintone_parallel(p)
                    self.assertEqual(set(b.client.control_threads), {threading.get_ident()})
                    imports = [params["path"] for cmd, params in b.client.calls if cmd == "app.open"]
                    self.assertEqual(sum(path.endswith(".tiff") for path in imports),
                                     3 if options["format"] == "tiff" else 0)
                    self.assertTrue(any("mask" in path and path.endswith(".png") for path in imports))
                    self.assertEqual(list(b.exchange.iterdir()), [])
                    self.assertEqual(len(b.client.documents), 1)

    def test_serial_encoding_respects_switch_and_failure_prevents_insertion(self):
        b = self.bridge("serial-option")
        p = {**self.params(), "frequency_transfer": transfer_settings({"parallel": False})}
        seen = []
        write = b._write_rgb
        def encode(*args):
            seen.append(threading.get_ident())
            return write(*args)
        with patch.object(U, "bridge", return_value=b), \
                patch.object(U, "full_skin_mask", return_value=self.mask), \
                patch.object(b, "_write_rgb", side_effect=encode):
            U.send_skintone_parallel(p)
        self.assertEqual(set(seen), {threading.get_ident()})
        b = self.bridge("tiff-failure")
        p["frequency_transfer"] = transfer_settings({"format": "tiff"})
        write = b._write_rgb
        def broken(rel, *args):
            if "Detail" in rel:
                raise ValueError("TIFF encoding failed")
            return write(rel, *args)
        with patch.object(U, "bridge", return_value=b), \
                patch.object(U, "full_skin_mask", return_value=self.mask), \
                patch.object(b, "_write_rgb", side_effect=broken), \
                self.assertRaisesRegex(ValueError, "TIFF encoding failed"):
            U.send_skintone_parallel(p)
        self.assertEqual(list(b.exchange.iterdir()), [])
        self.assertEqual(len(b.client.documents), 1)


@unittest.skipUnless(shutil.which("photocraft-cli"), "photocraft-cli is not installed")
class RealCodecTests(unittest.TestCase):
    setUp = DT.TransferTests.setUp
    close_process = DT.TransferTests.close_process
    new_target = DT.TransferTests.new_target
    save_pixels = DT.TransferTests.save_pixels

    def test_correction_off_native_folder_has_no_hue_and_matches_composite(self):
        target = self.new_target("srgb")
        path, _ = self.bridge.fetch(self.root/"input")
        with Image.open(path) as image:
            icc = image.info["icc_profile"]
        layers = {key: np.full((8,8,3), value, np.uint16)
                  for key, value in (("color",24000),("blur",36000),("detail",32768))}
        mask = np.tile(np.array([0,32,64,128,192,224,255,255],np.uint8),(8,1))
        settings = ST.settings({"skintone":{"on":False}})
        with patch.object(ST, "tables", side_effect=AssertionError("no LUT")):
            self.bridge.send_skintone(layers, settings, mask, target, icc)
        children = self.client.run("document.inspect")["layers"][0]["children"]
        self.assertEqual([child["name"] for child in children], ["Detail","Blur","Color"])
        self.assertTrue(self.client.run("document.inspect")["layers"][0]["hasMask"])
        self.assertEqual([child["hasMask"] for child in children], [False,False,False])
        expected = ST.composite({key:a.astype(np.float32)/65535 for key,a in layers.items()},
                                np.ones_like(mask), settings)[1]
        source = cv2.imread(str(path), -1)[...,:3].astype(np.float32)/65535
        expected = source + (expected-source)*(mask[...,None]/255)
        actual = self.save_pixels("off.png")
        np.testing.assert_allclose(actual[...,:3], np.round(expected*65535), atol=1)
        self.assertEqual(len(self.bridge.session()["documents"]), 1)
        self.assertEqual(list(self.bridge.exchange.iterdir()), [])

    def test_no_added_blur_native_stack_preserves_profiles_masks_and_rgb16(self):
        settings = ST.settings({"skintone":{"hue_center":25,"hue_width":1}})
        soft = np.tile(np.array([0,32,64,128,192,224,255,255],np.uint8),(8,1))
        for profile in ("srgb","display-p3","prophoto-compat"):
            for mask in (soft,np.zeros_like(soft)):
                target=self.new_target(profile)
                path,_=self.bridge.fetch(self.root/"input")
                with Image.open(path) as image:
                    icc=image.info["icc_profile"]
                raw=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)[...,:3]
                layers={key:U.FS.rgb16(value) for key,value in U.FS.split(raw.astype(np.float32)/65535,"gaussian",2).items()}
                self.bridge.send_skintone(layers,settings,mask,target,icc,include_blur=False)
                layers=self.client.run("document.inspect")["layers"]
                self.assertEqual([a["name"] for a in layers[0]["children"]],["Detail","Hue","Color"])
                self.assertTrue(layers[0]["hasMask"])
                self.assertFalse(layers[0]["children"][1]["hasMask"])
                self.assertEqual(len(layers),2)
                pixels={key:U.FS.rgb16(value).astype(np.float32)/65535 for key,value in U.FS.split(raw.astype(np.float32)/65535,"gaussian",2).items()}
                expected=ST.composite(pixels,np.ones_like(mask),settings)[1]
                source=raw.astype(np.float32)/65535
                expected=source+(expected-source)*(mask[...,None]/255)
                self.client.call("doc.save",path="standalone.png")
                actual=cv2.imread(str(self.bridge.exchange/"standalone.png"),cv2.IMREAD_UNCHANGED)
                np.testing.assert_allclose(actual[...,:3],np.round(expected*65535),atol=2)
                with Image.open(self.bridge.exchange/"standalone.png") as image:
                    self.assertEqual(image.info["icc_profile"],icc)
                (self.bridge.exchange/"standalone.png").unlink()
                self.assertEqual(len(self.bridge.session()["documents"]),1)
                self.assertEqual(list(self.bridge.exchange.iterdir()),[])
                self.client.run("file.close",document=target["index"])

    def test_no_blur_or_hue_native_stack_reconstructs_source(self):
        target=self.new_target("srgb")
        path,_=self.bridge.fetch(self.root/"input")
        with Image.open(path) as image:
            icc=image.info["icc_profile"]
        raw=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)[...,:3]
        layers={key:U.FS.rgb16(value) for key,value in U.FS.split(raw.astype(np.float32)/65535,"median_fast",2).items()}
        self.bridge.send_skintone(layers,ST.settings({"skintone":{"on":False}}),
                                 np.zeros((8,8),np.uint8),target,icc,include_blur=False)
        children=self.client.run("document.inspect")["layers"][0]["children"]
        self.assertEqual([a["name"] for a in children],["Detail","Color"])
        self.assertFalse(any(a["hasMask"] for a in children))
        np.testing.assert_allclose(self.save_pixels("reconstructed.png")[...,:3],raw,atol=2)

    def test_native_hue_and_blur_opacity_are_separate_from_full_strength_pixels(self):
        layers={key:np.full((8,8,3),value,np.uint16)
                for key,value in (("color",[14000,28000,40000]),("blur",[25000,35000,45000]),
                                  ("detail",[32768,32768,32768]))}
        mask=np.tile(np.array([0,32,64,128,192,224,255,255],np.uint8),(8,1))
        for masked in (True,False):
            for strength,amount in ((0,0),(35,40),(100,100)):
                target=self.new_target("srgb")
                path,_=self.bridge.fetch(self.root/"input")
                with Image.open(path) as image:
                    icc=image.info["icc_profile"]
                settings=ST.settings({"skintone":{"hue_width":1,"hue_amount":amount}})
                self.bridge.send_skintone(layers,settings,mask,target,icc,
                                         blur_strength=strength,blur_masked=masked)
                children=self.client.run("document.inspect")["layers"][0]["children"]
                hue=next(a for a in children if a["name"]=="Hue")
                blur=next(a for a in children if a["name"]=="Blur")
                self.assertAlmostEqual(hue["opacity"],amount/100)
                self.assertAlmostEqual(blur["opacity"],strength/100)
                self.assertFalse(hue["hasMask"])
                self.assertFalse(blur["hasMask"])
                self.assertTrue(self.client.run("document.inspect")["layers"][0]["hasMask"])
                f={"blur_strength":strength,"blur_mask":masked}
                expected=ST.composite({key:a.astype(np.float32)/65535 for key,a in layers.items()},
                                      np.ones_like(mask),settings,f)[1]
                source=cv2.imread(str(path),-1)[...,:3].astype(np.float32)/65535
                expected=source+(expected-source)*(mask[...,None]/255)
                np.testing.assert_allclose(self.save_pixels("opacity.png")[...,:3],
                                           np.round(expected*65535),atol=3)
                self.client.run("file.close",document=target["index"])

    def test_all_formats_import_exact_rgb16_with_destination_profiles(self):
        pixels = np.random.default_rng(271).integers(0, 65536, (8, 8, 3), dtype=np.uint16)
        pixels[0, :4] = np.array([0, 1, 256, 65535])[:, None]
        for profile in ("srgb", "display-p3", "prophoto-compat"):
            target = self.new_target(profile)
            path, _ = self.bridge.fetch(self.root/"input")
            with Image.open(path) as image:
                icc = image.info["icc_profile"]
            for options in variants():
                with self.subTest(profile=profile, options=options):
                    name = "pixels." + options["format"]
                    self.bridge._write_rgb(name, pixels, icc, options)
                    scratch = self.bridge._open(name)
                    inspection = self.client.run("document.inspect")
                    self.assertEqual(inspection["depth"], 16)
                    self.assertEqual((inspection["width"], inspection["height"]), (8, 8))
                    self.client.call("doc.save", path="roundtrip.png")
                    actual = cv2.imread(str(self.bridge.exchange/"roundtrip.png"), cv2.IMREAD_UNCHANGED)
                    np.testing.assert_array_equal(actual[..., :3], pixels)
                    if actual.shape[2] == 4:
                        self.assertTrue(np.all(actual[..., 3] == 65535))
                    with Image.open(self.bridge.exchange/"roundtrip.png") as image:
                        self.assertEqual(image.info["icc_profile"], icc)
                    self.client.run("file.close", document=scratch)
                    (self.bridge.exchange/name).unlink()
                    (self.bridge.exchange/"roundtrip.png").unlink()
            self.client.run("file.close", document=target["index"])

    def test_complete_folders_preserve_recipe_masks_and_pixels_for_all_options(self):
        rng = np.random.default_rng(81)
        layers = {key:rng.integers(10000, 50000, (8,8,3), dtype=np.uint16)
                  for key in ("color", "blur", "detail")}
        mask = np.tile(np.array([0,32,64,128,192,224,255,255],np.uint8),(8,1))
        reference = None
        for options in variants():
            for parallel in (False, True):
                with self.subTest(options=options, parallel=parallel):
                    target = self.new_target("srgb")
                    path, _ = self.bridge.fetch(self.root/"input")
                    with Image.open(path) as image:
                        icc = image.info["icc_profile"]
                    pending = Future()
                    pending.set_result(layers["blur"])
                    result = self.bridge.send_skintone(
                        {key:layers[key] for key in ("color", "detail")},
                        ST.settings({"skintone":{"hue_amount":0}}), mask, target, icc,
                        blur_future=pending, transfer={**options,"parallel":parallel})
                    inspection = self.client.run("document.inspect")
                    self.assertEqual(len(self.bridge.session()["documents"]),1)
                    group = inspection["layers"][0]
                    self.assertEqual(group["id"],result["layer"])
                    self.assertEqual([layer["name"] for layer in group["children"]],
                                     ["Detail","Hue","Blur","Color"])
                    children = group["children"]
                    self.assertTrue(group["hasMask"])
                    self.assertEqual([child["hasMask"] for child in children],[False,False,False,False])
                    actual = self.save_pixels("composite.png")
                    if reference is None:
                        reference = actual.copy()
                    np.testing.assert_array_equal(actual,reference)
                    self.client.run("layer.layerMask.enabled",layer=group["id"],enabled=False)
                    for child in children:
                        if child["name"] == "Hue":
                            continue
                        for other in children:
                            self.client.run("layer.setProps",layer=other["id"],visible=other["id"]==child["id"])
                        self.client.run("layer.setProps",layer=child["id"],blend="Normal")
                        self.client.run("layer.setProps",layer=inspection["layers"][1]["id"],visible=False)
                        actual = self.save_pixels("isolated.png")
                        visible = np.ones(mask.shape,bool)
                        np.testing.assert_array_equal(actual[...,:3][visible],layers[child["name"].lower()][visible])
                        if actual.shape[2] == 4:
                            np.testing.assert_array_equal(actual[...,3],np.full(mask.shape,65535,np.uint16))
                    self.assertEqual(list(self.bridge.exchange.iterdir()),[])
                    self.client.run("file.close",document=target["index"])
