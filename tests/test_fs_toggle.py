"""Disabling added Blur retains Color/Detail and Skintone RGB16 semantics."""

from pathlib import Path
import json
import unittest
from unittest.mock import patch

import cv2
import numpy as np

import test_skintone as T
import test_fs_pipeline as P
from shp_photocraft_tools.modules.dnb import ui_server as U, skintone as ST, frequency_separation as FS


class ToggleApiTests(unittest.TestCase):
    setUp = T.SkintoneApiTests.setUp
    close_server = T.SkintoneApiTests.close_server
    get = T.SkintoneApiTests.get
    post = T.SkintoneApiTests.post

    def test_local_preview_and_export_use_blur_strength_mask_and_hue_opacity(self):
        loaded=self.post("/api/load",{"path":str(self.image)})
        req={"mode":"frequency","S":181,"view":"whole",
             "frequency":{"method":"gaussian","radius":2,"blur_radius":4,
                          "blur_strength":29,"blur_mask":False},
             "skintone":{"hue_width":1,"hue_amount":37}}
        p=U.parse_params(req)
        self.post("/api/run",req)
        mask=U.full_skin_mask(p)
        layers=U.skintone_layers(U.STATE["source_img"],mask,p)
        expected=ST.composite(layers,mask,p["skintone"],p["frequency"])[1]
        result=U.api_send_result(dict(req,generation=loaded["generation"]))
        np.testing.assert_array_equal(cv2.imread(result["path"],cv2.IMREAD_UNCHANGED)[...,:3],FS.rgb16(expected))
        saved=self.post("/api/save",req)
        np.testing.assert_array_equal(cv2.imread(str(Path(saved["dir"])/"result.png"),cv2.IMREAD_UNCHANGED)[...,:3],
                                      FS.rgb16(expected))
        full=ST.tables(dict(p["skintone"],hue_amount=100))[0]
        self.assertEqual((Path(saved["dir"])/"Hue.cube").read_text(),ST.cube_text(full,"Hue"))
        recipe=json.loads((Path(saved["dir"])/"settings.json").read_text())["layer_recipe"]
        self.assertIn("opacity 37.0%",recipe)
        self.assertIn("opacity 29.0%, unmasked",recipe)

    def test_preview_local_save_retains_separation_without_added_blur(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        req = {"mode":"frequency","S":181,"view":"whole","frequency":{"on":False},
               "skintone":{"hue_center":25,"hue_width":1}}
        with patch.object(FS, "split", wraps=FS.split) as split, \
                patch.object(FS, "separate", side_effect=AssertionError("no Blur separation")), \
                patch.object(FS, "skin_filter", wraps=FS.skin_filter) as filters:
            self.post("/api/run", req)
            p = U.parse_params(req)
            reg = U.STATE["last"]["reg"]
            _, mask, _ = U.tool_mask(reg,U.STATE["last"]["mreg"],p,181*U.STATE["last"]["k"])
            if mask is None:
                mask = np.ones(reg.shape[:2],np.float32)
            layers = U.skintone_layers(U.source_region(reg.shape,normalized=True),mask,p,U.STATE["last"]["k"])
            expected = ST.composite(layers,mask,p["skintone"])[1]
            actual = cv2.imdecode(np.frombuffer(self.get("/img/result")[0],np.uint8),1)
            np.testing.assert_array_equal(actual,U.u8(expected))
            self.post("/api/hue-center",dict(req,generation=loaded["generation"]))
            saved = self.post("/api/save", req)
            self.assertIn("color.png",saved["files"])
            self.assertNotIn("blur.png",saved["files"])
            self.assertIn("detail.png",saved["files"])
            self.assertNotIn("Blur (Normal",json.loads((Path(saved["dir"])/"settings.json").read_text())["layer_recipe"])
            result = U.api_send_result(dict(req,generation=loaded["generation"]))
            self.assertTrue(result["saved"])
            mask = U.full_skin_mask(p)
            layers = U.skintone_layers(U.STATE["source_img"],mask,p)
            expected = FS.rgb16(ST.composite(layers,mask,p["skintone"])[1])
            np.testing.assert_array_equal(cv2.imread(result["path"],cv2.IMREAD_UNCHANGED)[...,:3],expected)
            off = dict(req,skintone={"on":False})
            with patch.object(ST,"tables",side_effect=AssertionError("no Hue")):
                self.post("/api/run",off)
                self.post("/api/save",off)
                unchanged=U.api_send_result(dict(off,generation=loaded["generation"]))
            np.testing.assert_allclose(cv2.imread(unchanged["path"],cv2.IMREAD_UNCHANGED)[...,:3],FS.rgb16(U.STATE["source_img"]),atol=2)
            self.assertTrue(split.called)
            for call in filters.call_args_list:
                self.assertTrue(np.all(call.args[1] == 1))


class ToggleSendTests(unittest.TestCase):
    setUp = P.PipelineTests.setUp
    bridge = P.PipelineTests.bridge
    params = P.PipelineTests.params
    def test_send_retains_color_detail_but_never_computes_or_imports_added_blur(self):
        p=self.params()
        p["frequency"]["on"]=False
        for mask in (self.mask,np.zeros_like(self.mask)):
            b=self.bridge("standalone")
            with patch.object(U,"bridge",return_value=b), \
                    patch.object(U,"full_skin_mask",return_value=mask), \
                    patch.object(FS,"split",wraps=FS.split) as split, \
                    patch.object(FS,"skin_filter",wraps=FS.skin_filter) as filters:
                U.send_skintone_parallel(p)
            names=[args.get("name") for cmd,args in b.client.calls if cmd=="layer.setProps"]
            self.assertNotIn("Blur",names)
            self.assertIn("Detail",names)
            self.assertIn("Color",names)
            self.assertIn("Hue",names)
            self.assertTrue(split.called)
            self.assertEqual(filters.call_count,1)
            self.assertEqual(filters.call_args.args[2],p["frequency"]["radius"])
            self.assertTrue(np.all(filters.call_args.args[1] == 1))
            self.assertTrue(any(cmd=="layer.groupLayers" for cmd,_ in b.client.calls))
            self.assertEqual(list(b.exchange.iterdir()),[])
        p["skintone"]["on"]=False
        b=self.bridge("both-off")
        with patch.object(U,"bridge",return_value=b),patch.object(U,"full_skin_mask",return_value=self.mask), \
                patch.object(ST,"tables",side_effect=AssertionError("no Hue")), \
                patch.object(FS,"skin_filter",wraps=FS.skin_filter):
            U.send_skintone_parallel(p)
        self.assertFalse(any(cmd=="layer.newAdjustmentLayer.colorLookup" for cmd,_ in b.client.calls))

    def test_default_validation_and_cache_keys(self):
        self.assertTrue(FS.settings({})["on"])
        for on in (0,None,"false"):
            with self.assertRaises(ValueError):
                FS.settings({"frequency":{"on":on}})
        p=self.params()
        off={**p,"frequency":{**p["frequency"],"on":False}}
        with patch.dict(U.STATE,centre=(0,0),region_box=(0,0,64,48)):
            self.assertNotEqual(U.preview_key(p,"whole"),U.preview_key(off,"whole"))
            self.assertEqual(U.preview_key(off,"whole"),U.preview_key(
                {**off,"frequency":{**off["frequency"],"blur_radius":25}},"whole"))
        self.assertEqual(U.mask_settings_key(p,None),U.mask_settings_key(off,None))
        for method in ("gaussian","median","median_fast"):
            off["frequency"]["method"]=method
            mask=self.mask
            with patch.object(FS,"separate",side_effect=AssertionError("no added Blur")):
                layers=U.skintone_layers(self.source,mask,off)
            expected=FS.split(self.source,method,off["frequency"]["radius"])
            self.assertEqual(set(layers),{"color","detail"})
            for key in layers:
                np.testing.assert_array_equal(FS.rgb16(layers[key]),FS.rgb16(expected[key]))
            result=ST.composite(layers,mask,ST.settings({"skintone":{"on":False}}))[1]
            np.testing.assert_allclose(result,self.source,atol=1.5/65535 + 2*np.finfo(np.float32).eps)

    def test_unmasked_blur_filters_whole_image_and_sends_full_pixels_with_opacity(self):
        p=self.params()
        p["frequency"].update(blur_mask=False,blur_strength=27)
        p["skintone"]["hue_amount"]=34
        b=self.bridge("unmasked")
        with patch.object(U,"bridge",return_value=b),patch.object(U,"full_skin_mask",return_value=self.mask), \
                patch.object(FS,"skin_filter",wraps=FS.skin_filter) as filters:
            U.send_skintone_parallel(p)
        self.assertEqual(filters.call_count,2)
        self.assertTrue(all(np.all(call.args[1]==1) for call in filters.call_args_list))
        blur_props=[args for cmd,args in b.client.calls if cmd=="layer.setProps" and args.get("name")=="Blur"]
        self.assertAlmostEqual(blur_props[-1]["opacity"],.27)
        hue_props=[args for cmd,args in b.client.calls if cmd=="layer.setProps" and args.get("name")=="Hue"]
        self.assertAlmostEqual(hue_props[-1]["opacity"],.34)
        # RGB transfer still contains full-strength Blur, independent of opacity.
        expected=FS.split(self.source,"gaussian",2)
        blur=FS.rgb16(FS.skin_filter(expected["color"],np.ones(self.mask.shape,np.float32),4))
        self.assertTrue(any(a.shape[:2]==blur.shape[:2] and a.ndim==3
                            and np.array_equal(a[...,:3],blur) for a in b.client.files))

    def test_preview_blur_and_hue_amount_blend_full_effects(self):
        layers={key:value.astype(np.float32)/65535 for key,value in
                U.skintone_layers(self.source,self.mask,self.params(),native=True).items()}
        p=ST.settings({"skintone":{"hue_width":1,"hue_amount":38}})
        for masked in (True,False):
            f={"blur_strength":23,"blur_mask":masked}
            opacity=np.round(self.mask*255)/255 if masked else np.ones(self.mask.shape)
            expected=layers["color"]+(layers["blur"]-layers["color"])*opacity[...,None]*.23
            np.testing.assert_allclose(ST.base(layers,self.mask,f),expected,atol=1e-7)
            full=ST.process(expected,self.mask,dict(p,hue_amount=100))
            np.testing.assert_allclose(ST.process(expected,self.mask,p),
                                       expected+(full-expected)*.38,atol=1e-7)
