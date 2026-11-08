"""Native LUT tests against disposable headless documents, never the desktop."""

import json
import shutil
import unittest
from unittest.mock import patch
import zipfile

import cv2
import numpy as np
from PIL import Image

import test_dnb_transfer as T
from shp_photocraft_tools.modules.dnb import skintone as ST
from shp_photocraft_tools.modules.dnb import frequency_separation as FS
from shp_photocraft_tools.shared.photocraft import ControlError


@unittest.skipUnless(shutil.which("photocraft-cli"), "photocraft-cli is not installed")
class SkintoneTransferTests(unittest.TestCase):
    setUp = T.TransferTests.setUp
    close_process = T.TransferTests.close_process
    new_target = T.TransferTests.new_target
    save_pixels = T.TransferTests.save_pixels

    def split(self, bgr, method="gaussian", radius=2, mask=None, blur_radius=0):
        if mask is None:
            mask = np.ones(bgr.shape[:2], np.float32)
        return {key:FS.rgb16(a) for key,a in ST.separate(bgr,mask,method,radius,blur_radius).items()}

    def test_direct_hue_mask_matches_previous_subgroup_and_reuses_one_mask_source(self):
        source = np.full((16, 20, 3), [.25, .4, .75], np.float32)
        source[:, 10:] = [.35, .55, .7]
        cv2.imwrite(str(self.root / "source.png"), FS.rgb16(source))
        p = ST.settings({"skintone": {"hue_center": 50, "hue_width": 2}})
        soft = np.tile(np.array([0, 32, 64, 128, 192, 255, 0, 255, 64, 128], np.uint8), (16, 2))
        for mask in (np.zeros_like(soft), soft, np.full_like(soft, 255)):
            outputs = []
            for legacy in (True, False):
                self.client.call("doc.open", path="source.png")
                doc = self.bridge.active_doc()
                icc = self.bridge._target_profile(doc)
                layers = self.split(source, mask=mask / 255, blur_radius=3)
                run, open_doc = self.client.run, self.bridge._open
                mapping, opened, masks, groups = {}, [], [], []

                def emulate_old_group(command, **params):
                    if "layer" in params:
                        params["layer"] = mapping.get(params["layer"], params["layer"])
                    result = run(command, **params)
                    if command == "layer.setProps" and params.get("name") == "Hue" and legacy:
                        mapping[params["layer"]] = int(run("layer.groupLayers",
                            name="Hue correction", mode="passThrough")["layer"])
                    if command == "layer.layerMask.revealSelection":
                        masks.append(params["layer"])
                    if command == "layer.groupLayers":
                        groups.append(params["name"])
                    return result

                def observe_open(rel):
                    opened.append(rel)
                    return open_doc(rel)

                with patch.object(self.client, "run", side_effect=emulate_old_group), \
                        patch.object(self.bridge, "_open", side_effect=observe_open):
                    self.bridge.send_skintone(layers, p, mask, doc, icc)
                outputs.append(self.save_pixels("result.png"))
                if not legacy:
                    self.assertEqual(groups, ["SkinTools"])
                    self.assertEqual(sum("mask" in rel for rel in opened), int(mask.any()))
                    self.assertEqual(len(masks), 2 if mask.any() else 0)
                    group = self.client.run("document.inspect")["layers"][0]
                    hue = group["children"][1]
                    self.assertEqual(hue["name"], "Hue")
                    self.assertEqual(hue["adjustment"]["kind"], "colorLookup")
                    self.assertTrue(hue["hasMask"])
                    self.assertNotIn("children", hue)
                self.assertEqual(len(self.bridge.session()["documents"]), 1)
                self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                self.client.run("file.close", document=doc["index"])
            # Removing the subgroup removes one native intermediate rounding step.
            np.testing.assert_allclose(outputs[0].astype(np.int32), outputs[1], atol=2, rtol=0)

    def test_native_profiles_depths_soft_mask_single_undo_and_preview_agreement(self):
        rng = np.random.default_rng(51)
        source = rng.uniform(.04, .98, (8, 8, 3))
        source[0] = np.repeat(np.linspace(0, 1, 8)[:, None], 3, axis=1)
        cv2.imwrite(str(self.root / "source.png"), np.round(source*65535).astype(np.uint16))
        mask = np.tile(np.array([0, 64, 128, 255, 0, 64, 128, 255], np.uint8), (8, 1))
        p = ST.settings({})
        errors = []
        for profile in ("srgb", "none", "display-p3", "adobe-rgb-compat", "prophoto-compat",
                        "linear-srgb", "rec2020", "aces-cg"):
            for depth in (8, 16, 32):
                with self.subTest(profile=profile, depth=depth):
                    self.client.call("doc.open", path="source.png")
                    self.client.run("edit.assignProfile", profile=profile)
                    self.client.run(f"image.mode.bits{depth}")
                    doc = self.bridge.active_doc()
                    path, _ = self.bridge.fetch(self.root / "input")
                    before = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[..., :3]
                    with Image.open(path) as image:
                        icc = image.info["icc_profile"]
                    histories = len(self.client.run("document.inspect")["history"])
                    base = before.astype(np.float32) / np.iinfo(before.dtype).max
                    layers = self.split(base, mask=mask/255, blur_radius=3)
                    result = self.bridge.send_skintone(layers, p, mask, doc, icc)
                    inspection = self.client.run("document.inspect")
                    self.assertEqual(len(inspection["history"]), histories+1)
                    self.assertEqual(len(self.bridge.session()["documents"]), 1)
                    group = inspection["layers"][0]
                    self.assertEqual(group["id"], result["layer"])
                    self.assertEqual(group["blend"], "Normal")
                    self.assertFalse(group["hasMask"])
                    self.assertEqual([a["name"] for a in group["children"]], ["Detail", "Hue", "Blur", "Color"])
                    detail,correction,blur,color=group["children"]
                    self.assertFalse(detail["hasMask"]);self.assertFalse(color["hasMask"])
                    self.assertEqual(detail["blend"],"Linear Light");self.assertEqual(color["blend"],"Normal")
                    self.assertEqual(correction["blend"],"Normal");self.assertTrue(correction["hasMask"])
                    self.assertEqual(blur["blend"],"Normal");self.assertTrue(blur["hasMask"])
                    self.assertEqual(correction["adjustment"]["kind"], "colorLookup")
                    self.assertNotIn("children", correction)
                    after = self.save_pixels("after.png")[..., :3]
                    numeric={key:a.astype(np.float32)/65535 for key,a in layers.items()}
                    _,expected = ST.composite(numeric, mask/255, p)
                    scale = np.iinfo(after.dtype).max
                    error = float(np.abs(after/scale-expected).max())
                    errors.append((depth, error))
                    np.testing.assert_allclose(after/scale, expected, atol=3/scale)
                    np.testing.assert_allclose(after[mask == 0]/scale, base[mask == 0], atol=3/scale)
                    self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                    self.client.run("file.close", document=doc["index"])
        print("Skintone native preview agreement, 24 profile/depth cases: " +
              ", ".join(f"{depth}-bit max {max(e for d,e in errors if d == depth):.8f}" for depth in (8,16,32)))

    def test_embedded_luts_persist_reopen_and_remain_editable(self):
        doc = self.new_target("display-p3")
        icc = self.bridge._target_profile(doc)
        p = ST.settings({"skintone": {"hue_center": 50, "hue_width": 2}})
        base=self.save_pixels("base.png")[...,:3].astype(np.float32)/65535
        result = self.bridge.send_skintone(self.split(base),p, np.full((8, 8), 128, np.uint8), doc, icc)
        before = self.save_pixels("before.png")
        self.client.call("doc.save", path="tone.pcraft")
        path = self.bridge.exchange / "tone.pcraft"
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            stored_group = next(a for a in manifest["document"]["layers"] if a["name"] == "SkinTools")
            hue = next(a for a in stored_group["content"]["children"] if a["name"] == "Hue")
            lut = hue["content"]["adjustment"]["ColorLookup"]
            self.assertEqual(len(lut["lut"]), 33**3*3)
            self.assertIn("SHP Skintone", lut["name"])
        self.client.run("file.close", document=doc["index"])
        self.client.call("doc.open", path="exchange/tone.pcraft")
        after = self.save_pixels("after.png")
        np.testing.assert_array_equal(after, before)
        group = self.client.run("document.inspect")["layers"][0]
        hue = group["children"][1]
        identity = ST.tables(ST.settings({"skintone": {"hue_amount": 0}}))[0]
        self.client.run("layer.setAdjustment", layer=hue["id"],
                        data=ST.cube_text(identity, "Hue"), fileName="identity.cube",
                        interpolation="trilinear", dither=False)
        edited = self.save_pixels("edited.png")
        self.assertGreater(float(np.abs(edited.astype(float)-after).max()), 10)
        self.client.run("layer.setProps", layer=group["id"], visible=False)
        self.assertNotEqual(result["layer"], None)

    def test_skin_colours_really_change_under_narrow_limits_and_noop_is_visible_original(self):
        source = np.full((16, 20, 3), [.35, .5, .7], np.float32)
        source[:,10:] = [.4,.55,.75]
        cv2.imwrite(str(self.root / "skin.png"), np.round(source*65535).astype(np.uint16))
        cases = (
            ("default core", {}, False),
            ("zero amount", {"hue_amount":0, "hue_width":2, "sat_amount":100,"sat_max":10,"sat_min":1}, False),
            ("hue only", {"hue_center":50,"hue_width":3}, True),
        )
        for label, settings, changed in cases:
            with self.subTest(case=label):
                self.client.call("doc.open", path="skin.png")
                doc = self.bridge.active_doc()
                icc = self.bridge._target_profile(doc)
                before = self.save_pixels("before.png")[...,:3]/65535
                p = ST.settings({"skintone":settings})
                layers=self.split(before.astype(np.float32))
                self.bridge.send_skintone(layers,p,np.full((16,20),255,np.uint8),doc,icc)
                after = self.save_pixels("after.png")[...,:3]/65535
                delta = float(np.max(np.abs(after-before)))
                if changed:
                    self.assertGreater(delta,.05,"Native LUT adjustment silently did nothing")
                else:
                    self.assertLess(delta,3/65535,"No-op settings must keep the original visible")
                _,expected = ST.composite({key:a.astype(np.float32)/65535 for key,a in layers.items()},np.ones((16,20)),p)
                np.testing.assert_allclose(after,expected,atol=3/65535)
                self.client.run("file.close",document=doc["index"])

    def test_missing_profile_stale_size_and_unsupported_or_failed_copy_leave_target_untouched(self):
        doc = self.new_target("srgb")
        icc = self.bridge._target_profile(doc)
        mask = np.full((8, 8), 255, np.uint8)
        layers=self.split(np.full((8,8,3),.4,np.float32))
        with self.assertRaisesRegex(ControlError, "size changed"):
            wrong=self.split(np.full((9,8,3),.4,np.float32))
            self.bridge.send_skintone(wrong,ST.settings({}), np.ones((9, 8), np.uint8), doc, icc)
        with self.assertRaisesRegex(ControlError, "no ICC profile captured"):
            self.bridge.send_skintone(layers,ST.settings({}), mask, doc, None)
        before = self.client.run("document.inspect")
        run = self.client.run
        for blocked in ("layer.newAdjustmentLayer.colorLookup", "layer.copyToDocument", "target copy"):
            def fail(command, **params):
                if command == blocked or (blocked == "target copy" and command == "layer.copyToDocument"
                                          and params["document"] == doc["index"]):
                    raise ControlError("fixture unsupported/copy failure")
                return run(command, **params)
            with patch.object(self.client, "run", side_effect=fail):
                with self.assertRaisesRegex(ControlError, "fixture"):
                    self.bridge.send_skintone(layers,ST.settings({}), mask, doc, icc)
            after = self.client.run("document.inspect")
            self.assertTrue(after["history"] == before["history"], "Failed Send changed target history")
            self.assertTrue(after["layers"] == before["layers"], "Failed Send changed target layers")
            self.assertEqual(len(self.bridge.session()["documents"]), 1)
            self.assertEqual(list(self.bridge.exchange.iterdir()), [])

    def test_low_frequency_correction_detail_restoration_and_neutral_profiles(self):
        x=np.indices((24,32))[1]
        source=np.full((24,32,3),[.28,.45,.72],np.float32)
        source+=((x%2)*2-1)[...,None]*.035
        source[6:10,8:12]=[.12,.2,.9]
        source[13:17,14:18]=[.6,.32,.5]
        cv2.imwrite(str(self.root/"textured.png"),FS.rgb16(source))
        mask=np.ones((24,32),np.float32)
        mask[:,:8]=0;mask[:,8:12]=64/255;mask[:,12:16]=128/255
        p=ST.settings({"skintone":{"hue_center":45,"hue_width":3,"sat_min":35,"sat_max":40}})
        neutral=dict(p,hue_amount=0)
        for method in ("gaussian","median"):
            for profile in ("srgb","linear-srgb","prophoto-compat"):
                for depth in (16,32):
                    for kind in ("active","zero amount","zero mask","neutral"):
                        with self.subTest(method=method,profile=profile,depth=depth,kind=kind):
                            self.client.call("doc.open",path="textured.png")
                            self.client.run("edit.assignProfile",profile=profile)
                            self.client.run(f"image.mode.bits{depth}")
                            doc=self.bridge.active_doc()
                            icc=self.bridge._target_profile(doc)
                            before=self.save_pixels("base.png")[...,:3]/65535
                            actual_mask=mask*0 if kind=="zero mask" else mask
                            layers=self.split(before.astype(np.float32),method,2,actual_mask,
                                              blur_radius=0 if kind=="neutral" else 3)
                            numeric={key:a.astype(np.float32)/65535 for key,a in layers.items()}
                            settings=neutral if kind in ("zero amount","neutral") else p
                            corrected,expected=ST.composite(numeric,actual_mask,settings)
                            self.bridge.send_skintone(layers,settings,np.round(actual_mask*255).astype(np.uint8),doc,icc)
                            after=self.save_pixels("after.png")[...,:3]/65535
                            np.testing.assert_allclose(after,expected,atol=4/65535)
                            if kind in ("zero mask","neutral"):
                                np.testing.assert_allclose(after,before,atol=4/65535)
                            elif kind=="zero amount":
                                np.testing.assert_allclose(after,FS.composite(numeric,actual_mask),atol=4/65535)
                                self.assertGreater(float(np.abs(after-before).max()),.003)
                            else:
                                self.assertGreater(float(np.abs(after-before).max()),.03)
                                naive=ST.process(before.astype(np.float32),actual_mask,settings)
                                self.assertGreater(float(np.abs(expected-naive).max()),.02)
                                outer=self.client.run("document.inspect")["layers"][0]
                                detail,correction,blur,color=outer["children"]
                                self.client.run("layer.setProps",layer=detail["id"],visible=False)
                                low=self.save_pixels("low.png")[...,:3]/65535
                                np.testing.assert_allclose(low,corrected,atol=3/65535)
                                self.client.run("layer.setProps",layer=detail["id"],visible=True)
                                self.client.run("layer.setProps",layer=correction["id"],visible=False)
                                blurred=self.save_pixels("blurred.png")[...,:3]/65535
                                np.testing.assert_allclose(blurred,FS.composite(numeric,actual_mask),atol=4/65535)
                                self.client.run("layer.setProps",layer=blur["id"],visible=False)
                                recovered=self.save_pixels("recovered.png")[...,:3]/65535
                                np.testing.assert_allclose(recovered,before,atol=4/65535)
                            self.client.run("file.close",document=doc["index"])


if __name__ == "__main__":
    unittest.main()
