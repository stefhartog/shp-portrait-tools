"""Isolated Combine processing, export, cache and transfer contracts."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import cv2
import numpy as np
from PIL import Image

import test_skintone as T
from shp_photocraft_tools.modules.dnb import ui_server as U, dodge_burn as D, skintone as ST, frequency_separation as FS


class CombineMathTests(unittest.TestCase):
    def test_mix_order_original_derived_bands_and_disabled_work(self):
        source=np.random.default_rng(91).uniform(.2,.8,(32,40,3)).astype(np.float32)
        original=source.copy()
        mask=np.tile(np.linspace(0,1,40,dtype=np.float32),(32,1))
        req={**T.T.PARAMS,"mode":"combine","S":181,"frequency":{"radius_at_s181":2},
             "skintone":{"on":False},"second_band":{"on":True,"invert":True,"strength":.5},
             "combine":{"fs_mix":35,"dnb_mix":65}}
        p=U.parse_params(req)
        layers=U.skintone_layers(source,mask,p)
        _,fs=ST.composite(layers,mask,p["skintone"],p["frequency"])
        base=source+(fs-source)*.35
        bands,_,_,_=D.band_stack(source,p["S"],p["bands"],p["strength"],p["second_band"],
             first_on=p["band1_on"],edge=p["edge"],tau=p["tau"],max_ev=p["max_ev"],mask_result=(mask,None))
        for _,gray in bands:
            base=np.clip(base+(D.softlight(base,gray[...,None])-base)*mask[...,None]*.65,0,1).astype(np.float32)
        with patch.object(D,"combined_mask",side_effect=AssertionError("mask regenerated")), \
             patch.object(D,"band_stack",wraps=D.band_stack) as stack:
            _,actual_bands,result=U.combined_result(source,mask,p)
        np.testing.assert_allclose(result,base,atol=2e-7)
        self.assertEqual([index for index,_ in actual_bands],[1,2])
        np.testing.assert_array_equal(stack.call_args.args[0],original)
        np.testing.assert_array_equal(source,original)
        for fs_mix,dnb_mix in ((0,0),(0,100),(100,0),(100,100)):
            q=U.parse_params({**req,"combine":{"fs_mix":fs_mix,"dnb_mix":dnb_mix}})
            with patch.object(U,"skintone_layers",wraps=U.skintone_layers) as splitter, \
                 patch.object(D,"band_stack",wraps=D.band_stack) as stack:
                _,_,result=U.combined_result(source,mask,q)
            self.assertEqual(splitter.call_count,int(fs_mix>0))
            self.assertEqual(stack.call_count,int(dnb_mix>0))
            if fs_mix==dnb_mix==0:
                np.testing.assert_array_equal(result,source)
            if dnb_mix==0 and fs_mix==100:
                np.testing.assert_array_equal(result,fs)
        for raw in ({"fs_mix":-1},{"dnb_mix":101},{"fs_mix":True},{"fs_mix":float("nan")},[]):
            with self.assertRaises(ValueError):U.parse_params({**req,"combine":raw})
        self.assertEqual(U.parse_params({**req,"combine":{}})["combine"],{"fs_mix":100,"dnb_mix":100})


class CombineApiTests(unittest.TestCase):
    setUp=T.SkintoneApiTests.setUp
    close_server=T.SkintoneApiTests.close_server
    get=T.SkintoneApiTests.get
    def post(self,route,body):
        request=Request(self.base+route,json.dumps(body).encode(),
                        {"Content-Type":"application/json","Origin":self.base})
        with urlopen(request,timeout=10) as response:
            return json.load(response)

    def request(self):
        return {**T.T.PARAMS,"mode":"combine","S":181,"view":"whole","precache_mask":False,
                "skintone":{"on":False},"frequency":{"radius_at_s181":2},
                "combine":{"fs_mix":55,"dnb_mix":35},"second_band":{"on":True,"invert":True}}

    def test_preview_cache_presets_save_and_rgb16_local_result(self):
        self.post("/api/load",{"path":str(self.image)})
        req=self.request()
        original=U.STATE["source_img"].copy()
        self.post("/api/run",req)
        self.assertTrue(self.post("/api/run",req)["cached"])
        p=U.parse_params(req)
        for changed in ({**req,"combine":{"fs_mix":0,"dnb_mix":35}},
                        {**req,"combine":{"fs_mix":55,"dnb_mix":0}},
                        {**req,"second_band":{"on":False}},
                        {**req,"frequency":{"radius":3}}):
            self.assertNotEqual(U.preview_key(p,"whole"),U.preview_key(U.parse_params(changed),"whole"))
            self.assertFalse(self.post("/api/run",changed).get("cached",False))
        for mode in ("dnb","frequency","mask"):
            self.assertEqual(U.mask_settings_key(p,None),
                             U.mask_settings_key(U.parse_params({**req,"mode":mode}),None))
        self.post("/api/run",req)
        result,icc=U.skin_result(p)
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/result")[0],np.uint8),1),
                                      U.u8(result))
        saved=self.post("/api/save",req)
        folder=Path(saved["dir"])
        self.assertEqual(saved["files"],["result.png","settings.json"])
        np.testing.assert_array_equal(cv2.imread(str(folder/"result.png"),-1)[...,:3],FS.rgb16(result))
        with Image.open(folder/"result.png") as image:
            self.assertEqual(image.info["icc_profile"],icc)
        exported=self.post("/api/send-result",req)
        self.assertTrue(exported["saved"])
        self.assertIn("shpcombine",Path(exported["path"]).name)
        np.testing.assert_array_equal(cv2.imread(exported["path"],-1)[...,:3],FS.rgb16(result))
        self.post("/api/presets/save",{"name":"Combined","settings":req})
        self.assertEqual(json.loads(self.get("/api/presets")[0])["presets"]["Combined"],req)
        np.testing.assert_array_equal(U.STATE["source_img"],original)

    def test_individual_result_previews_ignore_mixes_and_follow_shared_settings(self):
        self.post("/api/load",{"path":str(self.image)})
        req=self.request()
        self.post("/api/run",req)
        unmixed={key:self.get("/img/"+key)[0] for key in ("fs_result","dnb_result","mask")}
        original=U.STATE["source_img"].copy()
        p=U.parse_params(req)
        mask=U.full_skin_mask(p)
        layers=U.skintone_layers(original,mask,p)
        _,fs_result=ST.composite(layers,mask,p["skintone"],p["frequency"])
        _,dnb_result,_,_=D.band_stack(original,p["S"],p["bands"],p["strength"],p["second_band"],
            first_on=p["band1_on"],edge=p["edge"],tau=p["tau"],max_ev=p["max_ev"],mask_result=(mask,None))
        for key,expected in (("fs_result",fs_result),("dnb_result",dnb_result)):
            np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(unmixed[key],np.uint8),1),U.u8(expected))
        for mixes in ({"fs_mix":0,"dnb_mix":0},{"fs_mix":100,"dnb_mix":100}):
            self.post("/api/run",{**req,"combine":mixes})
            for key,data in unmixed.items():
                self.assertEqual(self.get("/img/"+key)[0],data)
            if mixes["fs_mix"]==0:
                np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/result")[0],np.uint8),1),
                                              U.u8(original))
        self.post("/api/run",{**req,"frequency":{"radius":3,"blur_radius":20}})
        self.assertNotEqual(self.get("/img/fs_result")[0],unmixed["fs_result"])
        self.assertEqual(self.get("/img/dnb_result")[0],unmixed["dnb_result"])
        self.post("/api/run",{**req,"second_band":{"on":True,"invert":False}})
        self.assertNotEqual(self.get("/img/dnb_result")[0],unmixed["dnb_result"])
        self.assertEqual(self.get("/img/fs_result")[0],unmixed["fs_result"])
        for mode in ("frequency","dnb"):
            self.post("/api/run",{**req,"mode":mode})
            self.post("/api/run",req)
            for key,data in unmixed.items():
                self.assertEqual(self.get("/img/"+key)[0],data)
            self.assertTrue(self.post("/api/run",req)["cached"])
        self.post("/api/run",{**req,"band1_on":False,"second_band":{"on":False}})
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/dnb_result")[0],np.uint8),1),
                                      U.u8(original))
        np.testing.assert_array_equal(U.STATE["source_img"],original)

    def test_flattened_send_and_one_editable_stack_routes(self):
        calls=[]
        class Bridge:
            def status(inner):
                return {"doc":{"index":1,"name":"synthetic"},"headless":True}
            def fetch(inner,dest):
                return self.image,{"index":1,"name":"synthetic"}
            def send_result(inner,pixels,doc,*,source_icc):
                calls.append(("result",pixels,source_icc))
                return {"document":doc["name"]}
            def send_skintone(inner,layers,settings,mask,doc,icc,**kwargs):
                if "blur_future" in kwargs:
                    layers={**layers,"blur":kwargs["blur_future"].result()}
                calls.append(("fs",layers,settings,mask,kwargs))
                return {"document":doc["name"]}
            def send_skin_bands(inner,bands,mask,doc,**kwargs):
                calls.append(("bands",bands,mask,kwargs))
                return {"document":doc["name"]}
        U.BRIDGE["b"]=Bridge()
        self.post("/api/pc/fetch",{})
        req=self.request()
        with self.assertRaises(HTTPError) as error:
            self.post("/api/pc/send_layers",{**req,"generation":U.STATE["landmark_generation"]-1})
        self.assertEqual(error.exception.code,400)
        self.assertEqual(calls,[])
        expected,icc=U.skin_result(U.parse_params(req))
        self.post("/api/send-result",req)
        self.assertEqual(calls[-1][0],"result")
        np.testing.assert_array_equal(calls[-1][1],FS.rgb16(expected))
        self.assertEqual(calls[-1][2],icc)
        self.post("/api/pc/send_layers",req)
        self.assertEqual(calls[-1][0],"fs")
        self.assertEqual([index for index,_ in calls[-1][4]["bands"]],[1,2])
        self.assertEqual(calls[-1][4]["band_mix"],35)
        self.assertAlmostEqual(calls[-1][4]["blur_strength"],100)
        p=U.parse_params(req)
        mask=U.full_skin_mask(p)
        unmixed=U.skintone_layers(U.STATE["source_img"],mask,p)
        for key in ("color","blur"):
            expected=U.STATE["source_img"]+(unmixed[key]-U.STATE["source_img"])*.55
            np.testing.assert_array_equal(calls[-1][1][key],FS.rgb16(expected))
        self.post("/api/pc/send_layers",{**req,"combine":{"fs_mix":0,"dnb_mix":70}})
        self.assertEqual(calls[-1][0],"bands")
        self.assertEqual(calls[-1][3]["band_mix"],70)
        self.post("/api/pc/send_layers",{**req,"combine":{"fs_mix":100,"dnb_mix":0}})
        self.assertEqual(calls[-1][0],"fs")
        self.assertIsNone(calls[-1][4]["band_mask8"])
        self.post("/api/pc/send_layers",{**req,"mode":"dnb"})
        self.assertEqual(calls[-1][0],"bands")
        self.post("/api/pc/send_layers",{**req,"mode":"frequency"})
        self.assertEqual(calls[-1][0],"fs")
        for mode in ("dnb","frequency"):
            self.post("/api/send-result",{**req,"mode":mode})
            self.assertEqual(calls[-1][0],"result")
        missing={**req,"directional":{"on":True,"left_upper_arm":True,"torso":True}}
        for mode in ("frequency","combine"):
            self.post("/api/pc/send_layers",{**missing,"mode":mode})
            self.assertEqual(calls[-1][0],"fs")
            self.assertNotIn("directional",calls[-1][1])
            self.assertIsNone(calls[-1][4].get("directional"))
            self.post("/api/send-result",{**missing,"mode":mode})
            self.assertEqual(calls[-1][0],"result")


if __name__=="__main__":
    unittest.main()
