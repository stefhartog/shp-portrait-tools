"""Synthetic Skintone mathematics and API contracts; no inference or live services."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import cv2
import numpy as np
from PIL import Image

import test_dnb as T
from shp_photocraft_tools.modules.dnb import skintone as ST, ui_server as S


class SkintoneTests(unittest.TestCase):
    def test_center_circular_weights_wrap_and_neutral_or_empty_evidence(self):
        hsv=np.array([[[359,.6,.8],[1,.6,.8],[180,0,.8],[90,.8,0]]],np.float32)
        color=cv2.cvtColor(hsv,cv2.COLOR_HSV2BGR)
        result=ST.estimate_center(color,np.ones((1,4),np.float32))
        self.assertEqual(result["center"],0)
        result=ST.estimate_center(color,np.array([[.25,.75,1,1]],np.float32))
        self.assertAlmostEqual(result["center"],.5)
        for mask in (np.zeros((1,4),np.float32),np.array([[0,0,1,1]],np.float32)):
            self.assertIsNone(ST.estimate_center(color,mask)["center"])
        opposing=cv2.cvtColor(np.array([[[0,.6,.8],[180,.6,.8]]],np.float32),cv2.COLOR_HSV2BGR)
        self.assertIsNone(ST.estimate_center(opposing,np.ones((1,2),np.float32))["center"])

    def test_soft_limits_core_shoulders_continuity_and_bounds(self):
        x = np.linspace(-100, 100, 20001)
        out = ST.soft_limit(x, -30, 30, 5)
        self.assertTrue(np.all(np.diff(out) >= 0))
        self.assertTrue(np.all((-30 <= out) & (out <= 30)))
        np.testing.assert_array_equal(out[np.abs(x) <= 25], x[np.abs(x) <= 25])
        self.assertGreater(float(out[np.searchsorted(x, -28)]), -28)
        np.testing.assert_array_equal(ST.soft_limit(x, -30, 30, 0), np.clip(x, -30, 30))
        np.testing.assert_allclose(ST.soft_limit(x, -2, 2, 30), ST.soft_limit(x, -2, 2, 2))
        for boundary in (-25, 25):
            step = 1e-4
            derivative = np.diff(ST.soft_limit(np.array([boundary-step, boundary, boundary+step]), -30, 30, 5)) / step
            np.testing.assert_allclose(derivative, 1, atol=2e-5)

    def test_circular_hue_preserves_core_value_saturation_and_neutrals(self):
        hsv = np.array([[[350, .6, .8], [5, .6, .8], [120, .6, .8], [240, .6, .8]]], np.float32)
        rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
        p = ST.settings({"skintone": {"hue_center": 0, "hue_width": 20}})
        result = cv2.cvtColor(ST.transform(rgb, p, "Hue"), cv2.COLOR_RGB2HSV)
        np.testing.assert_allclose(result[0, :2], hsv[0, :2], atol=1e-4)
        offsets = (result[..., 0] + 180) % 360 - 180
        self.assertTrue(np.all(np.abs(offsets) <= 20.001))
        np.testing.assert_allclose(result[..., 1:], hsv[..., 1:], atol=1e-6)
        gray = np.repeat(np.linspace(0, 1, 1001, dtype=np.float32)[None, :, None], 3, axis=2)
        processed = ST.process(gray, np.ones(gray.shape[:2], np.float32), p)
        np.testing.assert_allclose(processed, gray, atol=6e-7)
        np.testing.assert_allclose(processed[..., 0], processed[..., 2], atol=2e-7)

    def test_legacy_saturation_is_ignored_no_saturation_table_is_generated(self):
        hsv = np.array([[[30, .1, .9], [30, .4, .9], [30, .9, .9]]], np.float32)
        rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
        p = ST.settings({})
        legacy = ST.settings({"skintone":{"sat_min":95,"sat_max":1,"sat_amount":100,"sat_softness":99}})
        self.assertEqual(legacy,p)
        self.assertEqual(len(ST.tables(legacy)),1)
        np.testing.assert_array_equal(ST.process(rgb[...,::-1],np.ones((1,3)),p),
                                      ST.process(rgb[...,::-1],np.ones((1,3)),legacy))
        np.testing.assert_array_equal(ST.transform(rgb, dict(p, hue_width=180), "Hue"), rgb)

    def test_lut_order_identity_size_and_mask_once(self):
        rng = np.random.default_rng(42)
        bgr = rng.uniform(.05, .95, (130, 45, 3)).astype(np.float32)
        p = ST.settings({})
        hue, = ST.tables(p)
        for table, channel in zip((hue,), ("Hue",)):
            nodes = np.array([[[.25, .5, .75], [.75, .25, .5]]], np.float32)
            np.testing.assert_allclose(ST.sample(nodes, table), ST.transform(nodes, p, channel), atol=6e-7)
            text = ST.cube_text(table, channel)
            request = json.dumps({"method": "engine.execute", "params": {"command": "layer.newAdjustmentLayer.colorLookup",
                                 "params": {"data": text, "fileName": channel+".cube", "interpolation": "trilinear", "dither": False}}})
            self.assertLess(len(request.encode()), 1048576)
            self.assertEqual(len(text.splitlines()), ST.LUT_SIZE ** 3 + 4)
        zero = np.zeros(bgr.shape[:2], np.float32)
        np.testing.assert_array_equal(ST.process(bgr, zero, p), bgr)
        full = ST.process(bgr, zero+1, p)
        soft = ST.process(bgr, zero+.3, p)
        np.testing.assert_allclose(soft, bgr+(full-bgr)*(round(.3*255)/255), atol=2e-7)
        identity = ST.settings({"skintone": {"hue_amount": 0, "sat_amount": 0}})
        np.testing.assert_allclose(ST.process(bgr, zero+1, identity), bgr, atol=6e-7)

    def test_distribution_weighting_and_no_samples(self):
        img = T.synthetic_image()
        mask = np.ones(img.shape[:2], np.float32)
        p = ST.settings({})
        d = ST.distribution(img, ST.process(img, mask, p), mask, p)
        self.assertEqual(len(d["bins"]), 16)
        self.assertGreater(d["sampled_weight"], 0)
        for row in filter(None, d["bins"]):
            for label in ("before", "after"):
                for key in ("hue", "saturation"):
                    self.assertEqual(row[label][key], sorted(row[label][key]))
        for empty in (mask*0, mask):
            gray = np.full(img.shape, .5, np.float32)
            self.assertEqual(ST.distribution(gray, gray, empty, p)["sampled_weight"], 0)
        self.assertIn("mask is empty", ST.distribution(img,img,mask*0,p)["effect"]["note"])
        zero = dict(p,hue_amount=0,sat_amount=0)
        self.assertIn("Hue Amount is zero", ST.distribution(img,img,mask,zero)["effect"]["note"])

    def test_validation(self):
        for bad in (None, [], {"hue_center": float("nan")},
                    {"hue_width": -1}, {"hue_softness": 31}, {"hue_amount": -1}):
            with self.assertRaises((ValueError, TypeError)):
                ST.settings({"skintone": bad})
        with self.assertRaisesRegex(ValueError, "dimensions"):
            ST.process(np.zeros((2, 2, 3)), np.zeros((3, 3)), ST.settings({}))
        with self.assertRaisesRegex(ValueError, "normalized"):
            ST.process(np.full((2, 2, 3), 1.1), np.ones((2, 2)), ST.settings({}))

    def test_zero_hue_width_is_finite_and_legacy_saturation_does_not_apply(self):
        p = ST.settings({"skintone":{"hue_width":0,"hue_center":30,"sat_min":50,"sat_max":50}})
        hsv=np.array([[[10,.6,.9],[50,.6,.9]]],np.float32)
        rgb=cv2.cvtColor(hsv,cv2.COLOR_HSV2RGB)
        hue=ST.transform(rgb,p,"Hue")
        result=cv2.cvtColor(hue,cv2.COLOR_RGB2HSV)
        np.testing.assert_allclose(result[...,0],30,atol=1e-4)
        np.testing.assert_allclose(result[...,1],.6,atol=1e-6)
        self.assertTrue(np.isfinite(ST.process(rgb[...,::-1],np.ones((1,2)),p)).all())
        # Plot-window changes never clamp the measured distribution.
        original=cv2.cvtColor(np.array([[[85,.9,.8]]],np.float32),cv2.COLOR_HSV2BGR)
        d=ST.distribution(original,original,np.ones((1,1)),p)
        row=next(row for row in d["bins"] if row)
        self.assertAlmostEqual(row["before"]["hue"][1]+d["hue_center"],85,places=4)
        self.assertAlmostEqual(row["before"]["saturation"][1],90,places=4)


class SkintoneApiTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    get = T.DnbTests.get
    post = T.DnbTests.post

    def test_load_and_get_estimate_new_native_color_not_prior_center_or_lut(self):
        class FakeBridge:
            def fetch(inner,dest):
                return self.image,{"index":1,"name":self.image.name}
        S.BRIDGE["b"]=FakeBridge()
        target={"S":181,"conf":"high","desc":"test","centre":(64,48),"face_info":None,"seen":[]}
        shape=(96,128)
        ones=np.ones(shape,np.float32)
        components={"A":ones,"features":ones*0,"colour":ones,"skin":ones}
        p=dict(T.PARAMS,mode="frequency",frequency={"method":"gaussian","radius":3,"blur_radius":0},
               skintone={"hue_center":230,"hue_width":1,"hue_amount":100},
               color={"on":False},tone={"on":False},structure={"protect":0})
        with patch.object(S.M,"pick_target",return_value=target), \
                patch.object(S.M,"person_box",return_value=(0,0,128,96)), \
                patch.object(S.K,"skin_mask",return_value=components):
            generations=[]
            for route,hue in (("/api/load",18),("/api/pc/fetch",43)):
                raw=cv2.cvtColor(np.full((96,128,3),[hue,.6,.8],np.float32),cv2.COLOR_HSV2BGR)
                cv2.imwrite(str(self.image),S.FS.rgb16(raw))
                loaded=self.post(route,{"path":str(self.image)})
                generations.append(loaded["generation"])
                with patch.object(ST,"process",side_effect=AssertionError("estimate must not apply LUT")):
                    estimate=self.post("/api/hue-center",dict(p,generation=loaded["generation"]))
                self.assertAlmostEqual(estimate["center"],hue,delta=.1)
                self.assertEqual(estimate["generation"],loaded["generation"])
                self.post("/api/run",dict(p,skintone={"hue_center":35}))
                self.assertEqual(self.post("/api/hue-center",dict(p,generation=loaded["generation"])),estimate)
            self.assertNotEqual(*generations)
            with self.assertRaises(HTTPError):
                self.post("/api/hue-center",dict(p,generation=generations[0]))
            with patch.object(S,"tool_mask",return_value=(ones,ones*0,None)):
                result=self.post("/api/hue-center",dict(p,generation=generations[-1]))
                self.assertIsNone(result["center"])
                self.assertIn("no usable",result["note"])
        loaded=self.post("/api/load",{"path":str(self.image)})
        result=self.post("/api/hue-center",dict(p,generation=loaded["generation"]))
        self.assertIsNone(result["center"])
        self.assertIn("no detected skin mask",result["note"])


    def test_preview_save_send_shared_mask_and_presets(self):
        sent = []

        class FakeBridge:
            def fetch(inner, dest):
                return self.image, {"index": 1, "name": self.image.name}

            def send_skintone(inner, layers, settings, mask, doc, icc):
                sent.append((settings, mask, doc, icc, layers))
                return {"layer": 20, "document": doc["name"]}

        S.BRIDGE["b"] = FakeBridge()
        self.post("/api/pc/fetch", {})
        p = dict(T.PARAMS, mode="skintone",
                 skintone=dict(ST.DEFAULTS,hue_center=25,sat_min=93,sat_max=2,sat_amount=100))
        with patch.object(S.D, "process", side_effect=AssertionError("not a D&B correction")):
            run = self.post("/api/run", p)
        self.assertGreater(run["stats"]["skintone"]["sampled_weight"], 0)
        mask_bytes = self.get("/img/mask")[0]
        self.post("/api/pc/send", p)
        self.assertEqual(sent[0][0]["hue_center"], 25)
        self.assertEqual(sent[0][1].dtype, np.uint8)
        mask = sent[0][1] / 255
        layers={key:a.astype(np.float32)/65535 for key,a in sent[0][4].items()}
        corrected, expected = ST.composite(layers, mask, sent[0][0])
        _,analysis_mask,_=S.tool_mask(S.STATE["img"],S.STATE["masks"],S.parse_params(p),p["S"])
        self.assertEqual(run["stats"]["skintone"], ST.distribution(layers["color"],corrected,analysis_mask,sent[0][0]))
        payload, headers = self.get("/img/result")
        self.assertEqual(headers["Content-Type"], "image/png")
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(payload, np.uint8), 1), S.u8(expected))
        for mode in ("mask", "frequency", "dnb"):
            rerun=self.post("/api/run", dict(p, mode=mode))
            self.assertEqual(self.get("/img/mask")[0], mask_bytes)
            if mode=="frequency":
                self.assertEqual(rerun["stats"]["skintone"],run["stats"]["skintone"])
        saved = self.post("/api/save", p)
        self.assertEqual(set(saved["files"]), {"Hue.cube", "color.png", "blur.png", "detail.png", "result.png", "skinmask.png", "settings.json"})
        with Image.open(Path(saved["dir"]) / "result.png") as img:
            self.assertTrue(img.info.get("icc_profile"))
        for name, table in zip(("Hue",), ST.tables(sent[0][0])):
            self.assertEqual((Path(saved["dir"]) / f"{name}.cube").read_text(), ST.cube_text(table, name))
        for key in ("color", "detail", "blur"):
            np.testing.assert_array_equal(cv2.imread(str(Path(saved["dir"])/f"{key}.png"),cv2.IMREAD_UNCHANGED)[...,:3],
                                          sent[0][4][key])
        recipe=json.loads((Path(saved["dir"])/"settings.json").read_text())["layer_recipe"]
        self.assertIn("Blur (Normal with skinmask.png)",recipe)
        self.assertIn("Hue (with skinmask.png;", recipe)
        self.assertNotIn("Pass Through", recipe)
        self.assertNotIn("Saturation",recipe)
        self.post("/api/presets/save", {"name": "Tone", "settings": p})
        self.assertEqual(json.loads(self.get("/api/presets")[0])["presets"]["Tone"], p)
        self.post("/api/run",dict(p,mode="frequency"))
        self.assertEqual(json.loads(self.get("/api/presets")[0])["presets"]["Tone"],p)
        self.post("/api/load", {"path": str(self.image)})
        with self.assertRaises(HTTPError):
            self.post("/api/pc/send", p)

    def test_empty_analysis_invalid_settings_and_transparency_are_explicit(self):
        self.post("/api/load", {"path": str(self.image)})
        p = dict(T.PARAMS, mode="skintone")
        mask = np.zeros(S.STATE["img"].shape[:2], np.float32)
        with patch.object(S, "tool_mask", return_value=(mask, mask, None)):
            self.assertEqual(self.post("/api/run", p)["stats"]["skintone"]["sampled_weight"], 0)
        with self.assertRaises(HTTPError):
            self.post("/api/run", dict(p, skintone={"hue_center": 400}))
        S.STATE["source_opaque"] = False
        for route in ("/api/run", "/api/save"):
            with self.assertRaises(HTTPError):
                self.post(route, p)

    def test_shared_fs_radius_and_blur_scale_for_preview(self):
        self.post("/api/load", {"path":str(self.image)})
        p=S.parse_params(dict(T.PARAMS,mode="skintone",S=362,
                             frequency={"method":"median","radius_at_s181":6,"blur_percent":500}))
        mask=np.ones(S.STATE["img"].shape[:2],np.float32)
        with patch.object(ST,"separate",return_value={}) as split:
            S.skintone_layers(S.STATE["source_img"],mask,p,.25)
            self.assertEqual(split.call_args.args[2:],("median",3,15))
        p2=S.parse_params(dict(T.PARAMS,mode="skintone",S=362,
                              frequency={"method":"median","radius_at_s181":6,"blur_percent":100}))
        first=S.skintone_layers(S.STATE["source_img"],mask,p)
        second=S.skintone_layers(S.STATE["source_img"],mask,p2)
        self.assertEqual(set(first),{"color","detail","blur"})
        for key in ("color","detail"):
            np.testing.assert_array_equal(first[key],second[key])
        for method in ("gaussian", "median"):
            p["frequency"]["method"] = method
            preview = S.skintone_layers(S.STATE["source_img"], mask, p)
            native = S.skintone_layers(S.STATE["source_img"], mask, p, native=True)
            original = S.FS.separate(S.STATE["source_img"], mask, method,
                                     p["frequency"]["radius"], p["frequency"]["blur_radius"])
            for key in native:
                self.assertEqual(native[key].dtype, np.uint16)
                np.testing.assert_array_equal(native[key], S.FS.rgb16(original[key]))
                np.testing.assert_array_equal(native[key], S.FS.rgb16(preview[key]))
                np.testing.assert_array_equal(preview[key], native[key].astype(np.float32) / 65535)
        self.assertGreater(float(np.abs(first["blur"]-second["blur"]).max()),.0001)
        self.assertEqual(p["mode"],"frequency")

    def test_downscaled_subject_crop_preview_uses_scaled_radius_and_cropped_source(self):
        y,x=np.mgrid[:1800,:2000]
        source=np.stack((.2+.08*np.sin(x/11),.4+.12*np.cos(y/15),.7+.05*np.sin((x+y)/5)),axis=-1).astype(np.float32)
        path=self.root/"large.png"
        cv2.imwrite(str(path),S.FS.rgb16(source))
        self.post("/api/load",{"path":str(path)})
        S.STATE["centre"]=(1100,1000)
        view=next(iter(S.CROP_SIDES))
        p=dict(T.PARAMS,mode="skintone",view=view,S=1800/S.CROP_SIDES[view],
               frequency={"method":"gaussian","radius":12,"blur_radius":0})
        parsed=S.parse_params(p)
        reg,k,mreg=S.get_region(view,p["S"])
        self.assertLess(k,1)
        left,top,w,h=S.STATE["region_box"]
        native=S.STATE["source_img"][top:top+h,left:left+w]
        resized=cv2.resize(native,(reg.shape[1],reg.shape[0]),interpolation=cv2.INTER_AREA)
        _,mask,_=S.tool_mask(reg,mreg,parsed,p["S"]*k)
        layers=ST.separate(resized,mask,"gaussian",12*k,0)
        _,expected=ST.composite(layers,mask,parsed["skintone"])
        self.post("/api/run",p)
        for key,a in dict(layers,blur=ST.base(layers,mask),result=expected).items():
            np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/"+key)[0],np.uint8),1),S.u8(a))

    def test_sdr_white_downscale_roundoff_is_not_hdr_at_either_integer_precision(self):
        source = np.full((1401, 2102, 3), 255, np.uint8)
        source[:10] = [100, 160, 220]
        uncorrected = cv2.resize(source.astype(np.float32)/255, (1400, 933),
                                 interpolation=cv2.INTER_AREA)
        self.assertGreater(float(uncorrected.max()), 1)
        for depth in (8, 16):
            with self.subTest(depth=depth):
                pixels = source if depth == 8 else source.astype(np.uint16)*257
                cv2.imwrite(str(self.image), pixels)
                loaded = self.post("/api/load", {"path": str(self.image)})
                self.assertIsNone(loaded["skintone_error"])
                native = S.STATE["source_img"].copy()
                shape = native.shape[:2]
                mask = np.ones(shape, np.float32)
                with patch.object(S, "tool_mask", side_effect=lambda img,*a: (
                        None, np.ones(img.shape[:2], np.float32), None)):
                    run = self.post("/api/run", dict(T.PARAMS, mode="skintone"))
                self.assertEqual(run["stats"]["preview_px"], [1400, 933])
                actual = cv2.imdecode(np.frombuffer(self.get("/img/result")[0], np.uint8), 1)
                np.testing.assert_array_equal(actual[-1], 255)
                np.testing.assert_array_equal(S.STATE["source_img"], native)
                # Full-resolution processing never needs to repair preview accumulation.
                np.testing.assert_array_equal(ST.process(native[-1:], mask[-1:], ST.settings({})), native[-1:])

    def test_float_tiff_normalization_and_genuine_hdr_rejection_before_preview_save_send(self):
        class FakeBridge:
            def send_skintone(inner, *args):
                self.fail("unsupported HDR must never reach Send")

        for maximum in (.75, 1.5):
            with self.subTest(maximum=maximum):
                path = self.root / "float.tif"
                raw = np.linspace(.25, maximum, 96*128, dtype=np.float32).reshape(96,128)
                self.assertTrue(cv2.imwrite(str(path), raw))
                loaded = self.post("/api/load", {"path": str(path)})
                np.testing.assert_array_equal(S.STATE["source_img"][..., 0], raw)
                if maximum <= 1:
                    self.assertIsNone(loaded["skintone_error"])
                    self.post("/api/run", dict(T.PARAMS, mode="skintone"))
                    continue
                self.assertIn("ranges from 0.25 to 1.5", loaded["skintone_error"])
                original = S.STATE["source_img"].copy()
                S.BRIDGE.update(b=FakeBridge(), doc={"index":1,"name":path.name})
                for route in ("/api/run", "/api/save", "/api/pc/send"):
                    with self.assertRaises(HTTPError) as error:
                        self.post(route, dict(T.PARAMS, mode="skintone"))
                    body = json.load(error.exception)
                    self.assertIn("tone-mapped SDR copy", body["error"])
                    self.assertIn("No source values have been clipped", body["error"])
                self.post("/api/run", dict(T.PARAMS, mode="mask"))
                np.testing.assert_array_equal(S.STATE["source_img"], original)
                self.assertFalse((self.root / "results").exists())


if __name__ == "__main__":
    unittest.main()
