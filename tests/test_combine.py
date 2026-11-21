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
from shp_portrait_tools.modules.dnb import ui_server as U, dodge_burn as D, skintone as ST, frequency_separation as FS


class CombineMathTests(unittest.TestCase):
    def test_editable_and_flattened_directional_hue_and_bands(self):
        source = np.random.default_rng(42).uniform(.25, .75, (24, 28, 3)).astype(np.float32)
        mask = np.broadcast_to(np.linspace(0, 1, 28, dtype=np.float32), source.shape[:2]).copy()
        region = np.flip(mask, axis=1).copy()
        state = dict(U.STATE, img=source, source_img=source, masks={}, source_opaque=True,
                     skintone_error=None)
        request = {**T.T.PARAMS, "mode": "combine", "S": 181, "directional": {"on": True},
                   "frequency": {"on": True, "radius": 2, "blur_radius": 4, "blur_strength": 80},
                   "skintone": {"on": True, "hue_amount": 65}, "band1_on": True}
        inputs = []

        def directional(base, *_args, **_kwargs):
            inputs.append(base.copy())
            return base * .9 + .04, region

        with patch.object(U, "STATE", state), \
             patch.object(U.DB, "apply", side_effect=directional), \
             patch.object(U.DB, "effective_settings", side_effect=lambda config, *_: (config, None)), \
             patch.object(U.DB, "segments"), patch.object(U.DB, "output_strength", return_value=43):
            for placement in ("folder", "layers"):
                reference = None
                with patch.object(U, "read_global_settings", return_value={"mask_placement": placement}):
                    for mix in (0, 50, 100):
                        with self.subTest(placement=placement, mix=mix):
                            p = U.parse_params({**request, "combine": {"fs_mix": mix, "dnb_mix": 35}})
                            _, bands, flat = U.combined_result(source, mask, p)
                            previews = {}
                            _, _, preview = U.combined_result(source, mask, p, previews=previews)
                            np.testing.assert_array_equal(preview, flat)
                            _, _, cached_preview = U.combined_result(
                                source, mask, p, previews={}, preview_components={
                                    "layers": U.skintone_layers(source, mask, p), "bands": bands,
                                    "fs_result": previews["fs_result"], "dnb_result": source})
                            np.testing.assert_array_equal(cached_preview, flat)
                            call = U.processing_service.combine_layers(
                                p, source, full_mask=lambda _: mask, skintone_layers=U.skintone_layers,
                                cached_bands=lambda *args, **kwargs: (bands, None, None),
                                mask8=U.u8, send_fs=lambda *args, **kwargs: (*args, kwargs),
                                mask_placement=placement)
                            native, hue, mask8, options = call
                            if reference is None:
                                reference = native
                            for name in ("color", "detail", "blur"):
                                np.testing.assert_array_equal(reference[name], native[name])
                            self.assertEqual(options["blur_strength"], mix * .8)
                            self.assertEqual(hue["hue_amount"], 65)
                            self.assertEqual(options["band_mix"], 35)
                            self.assertEqual(options["directional"][2], 43)
                            color, blur, detail = (native[name].astype(np.float32) / 65535
                                                   for name in ("color", "blur", "detail"))
                            shared = mask8.astype(np.float32) / 255
                            inside = np.ones_like(shared) if placement == "folder" else shared
                            base = color + (blur - color) * inside[..., None] * (mix * .008)
                            np.testing.assert_allclose(inputs[-1], base, atol=1e-7)
                            direction_pixels, direction_mask, strength = options["directional"]
                            direction_alpha = inside if placement == "folder" else direction_mask / 255
                            base += (direction_pixels / 65535 - base) * direction_alpha[..., None] * strength / 100
                            base = ST.process(base, inside, hue)
                            for _, gray in options["bands"]:
                                base += (D.softlight(base, gray / 65535) - base) * inside[..., None] * .35
                            if placement == "folder":
                                base = color + (base - color) * shared[..., None]
                            np.testing.assert_allclose(flat, np.clip(base + 2 * detail - 1, 0, 1), atol=3/65535)

    @patch.object(U, "read_global_settings", return_value={"mask_placement": "layers"})
    def test_mix_order_original_derived_bands_and_disabled_work(self, _settings):
        source=np.random.default_rng(91).uniform(.2,.8,(32,40,3)).astype(np.float32)
        original=source.copy()
        mask=np.tile(np.linspace(0,1,40,dtype=np.float32),(32,1))
        req={**T.T.PARAMS,"mode":"combine","S":181,"frequency":{"radius_at_s181":2},
             "skintone":{"on":False},"second_band":{"on":True,"invert":True,"strength":.5},
             "combine":{"fs_mix":35,"dnb_mix":65}}
        p=U.parse_params(req)
        layers=U.skintone_layers(source,mask,p)
        _,fs=ST.composite(layers,mask,p["skintone"],p["frequency"])
        f={**p["frequency"], "blur_strength":p["frequency"]["blur_strength"]*.35}
        base=ST.base(layers,mask,f)
        quantized_mask=U.u8(mask).astype(np.float32)/255
        bands,_,_,_=D.band_stack(source,p["S"],p["bands"],p["strength"],p["second_band"],
             first_on=p["band1_on"],edge=p["edge"],tau=p["tau"],max_ev=p["max_ev"],mask_result=(mask,None))
        for _,gray in bands:
            base=np.clip(base+(D.softlight(base,gray[...,None])-base)*quantized_mask[...,None]*.65,0,1)
        base=np.clip(base+2*layers["detail"]-1,0,1)
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
            self.assertEqual(splitter.call_count,1)
            self.assertEqual(stack.call_count,int(dnb_mix>0))
            if fs_mix==dnb_mix==0:
                np.testing.assert_allclose(result,source,atol=2/65535)
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

    def test_preview_route_preserves_render_cache_and_rejects_stale_requests(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        req = {**self.request(), "generation": loaded["generation"], "layer_cache_revision": "route"}
        expected = U.api_run(req)
        images = dict(U.STATE["imgs"])
        actual = self.post("/api/run", req)
        self.assertTrue(actual["cached"])
        self.assertEqual(actual["stats"], expected["stats"])
        self.assertEqual(U.STATE["imgs"], images)
        self.assertEqual(actual["layer_precache"]["status"], "skipped")
        self.assertFalse(actual["mask_precache"]["enabled"])
        for updated, message in (
            ({"generation": loaded["generation"] + 1}, "the loaded image changed; preview discarded"),
            ({"precache_mask": 1}, "precache_mask must be a boolean"),
            ({"precache_layers": 1}, "precache_layers must be a boolean"),
            ({"layer_cache_revision": ""}, "layer_cache_revision must be a short string"),
        ):
            with self.subTest(updated=updated), self.assertRaises(HTTPError) as caught:
                self.post("/api/run", {**req, **updated})
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(json.load(caught.exception)["error"], f"ValueError: {message}")
            self.assertEqual(U.STATE["version"], actual["version"])

    def test_mix_preview_reuses_components_and_matches_fresh_pixels(self):
        self.post("/api/load", {"path": str(self.image)})
        req = self.request()
        with patch.object(U, "tool_mask", wraps=U.tool_mask) as mask, \
             patch.object(U, "skintone_layers", wraps=U.skintone_layers) as fs, \
             patch.object(D, "band_stack", wraps=D.band_stack) as dnb:
            self.post("/api/run", req)
            for mixes in ({"fs_mix": 0, "dnb_mix": 35}, {"fs_mix": 85, "dnb_mix": 35},
                          {"fs_mix": 85, "dnb_mix": 0}, {"fs_mix": 85, "dnb_mix": 100}):
                before = (mask.call_count, fs.call_count, dnb.call_count)
                self.post("/api/run", {**req, "combine": mixes})
                self.assertEqual((mask.call_count, fs.call_count, dnb.call_count), before)
                cached = dict(U.STATE["imgs"])
                U.STATE["preview_cache"] = {}
                self.post("/api/run", {**req, "combine": mixes})
                self.assertEqual(cached, U.STATE["imgs"])
            mask.reset_mock(); fs.reset_mock(); dnb.reset_mock()
            self.post("/api/run", {**req, "strength": .4, "frequency": {"radius": 4}})
            self.assertEqual((mask.call_count, fs.call_count, dnb.call_count), (0, 1, 1))
            self.post("/api/run", {**req, "strength": .4})
            self.assertEqual((mask.call_count, fs.call_count, dnb.call_count), (0, 2, 1))
            self.post("/api/run", {**req, "view": "detail"})
            self.assertEqual((mask.call_count, fs.call_count, dnb.call_count), (1, 3, 2))
            self.post("/api/load", {"path": str(self.image)})
            self.post("/api/run", req)
            self.assertEqual((mask.call_count, fs.call_count, dnb.call_count), (2, 4, 3))

    def test_preview_component_cache_obeys_shared_budget(self):
        self.post("/api/load", {"path": str(self.image)})
        req = self.request()
        with patch.object(U, "PREVIEW_CACHE_LIMIT", 1), \
             patch.object(U, "skintone_layers", wraps=U.skintone_layers) as fs:
            self.post("/api/run", req)
            self.post("/api/run", {**req, "combine": {"fs_mix": 35, "dnb_mix": 35}})
            self.assertEqual(fs.call_count, 2)
            self.assertFalse(U.STATE["preview_cache"])
        self.post("/api/run", req)
        self.assertIn("preview:fs", U.STATE["preview_cache"])
        self.assertIn("preview:dnb", U.STATE["preview_cache"])
        self.assertLessEqual(sum(entry["bytes"] for entry in U.STATE["preview_cache"].values()),
                             U.PREVIEW_CACHE_LIMIT)

    def test_preview_components_survive_mode_switches_at_maximum_preview_size(self):
        image_path = Path(self.temp.name) / "square-preview.png"
        y, x = np.mgrid[:1400, :1400]
        image = np.stack((x % 256, y % 256, (x + y) % 256), axis=2).astype(np.uint8)
        self.assertTrue(cv2.imwrite(str(image_path), image))
        self.post("/api/load", {"path": str(image_path)})
        req = self.request()
        with patch.object(U, "skintone_layers", wraps=U.skintone_layers) as fs, \
             patch.object(D, "band_stack", wraps=D.band_stack) as dnb:
            for mode in ("frequency", "dnb", "combine", "frequency", "dnb", "combine"):
                self.post("/api/run", {**req, "mode": mode})
            self.assertEqual(fs.call_count, 1)
            self.assertEqual(dnb.call_count, 1)
        cache = U.STATE["preview_cache"]
        self.assertTrue({"preview:mask", "preview:fs", "preview:dnb"} <= set(cache))
        self.assertLessEqual(sum(entry["bytes"] for entry in cache.values()), U.PREVIEW_CACHE_LIMIT)

    def test_preview_components_share_across_tabs_and_invalidate_independently(self):
        self.post("/api/load", {"path": str(self.image)})
        req = self.request()
        with patch.object(U, "skintone_layers", wraps=U.skintone_layers) as fs, \
             patch.object(D, "band_stack", wraps=D.band_stack) as dnb:
            self.post("/api/run", {**req, "mode": "frequency"})
            self.assertEqual((fs.call_count, dnb.call_count), (1, 0))
            self.post("/api/run", {**req, "mode": "dnb"})
            self.assertEqual((fs.call_count, dnb.call_count), (1, 1))
            self.post("/api/run", req)
            self.assertEqual((fs.call_count, dnb.call_count), (1, 1),
                             "Combine should reuse standalone FS and D&B artifacts")
            self.post("/api/run", {**req, "strength": .4})
            self.assertEqual((fs.call_count, dnb.call_count), (1, 2),
                             "Changing D&B settings must preserve FS layers")
            self.post("/api/run", {**req, "strength": .4, "frequency": {"radius": 4}})
            self.assertEqual((fs.call_count, dnb.call_count), (2, 2),
                             "Changing FS settings must preserve D&B bands")

    def test_dnb_band_previews_are_masked_and_combine_shares_them(self):
        self.post("/api/load", {"path": str(self.image)})
        req = {**self.request(), "second_band": {"on": True, "invert": True}}
        dnb_req = {**req, "mode": "dnb"}
        self.post("/api/run", dnb_req)
        p = U.parse_params(dnb_req)
        reg, k, mreg = U.get_region("whole", p["S"])
        source = U.source_region(reg.shape, normalized=True)
        base_mask, total, structure = U.tool_mask(reg, mreg, p, p["S"] * k)
        layers, _, _, _ = D.band_stack(
            source, p["S"] * k, p["bands"], p["strength"], p["second_band"],
            first_on=p["band1_on"], sigma=p["layer_blur_at_s181"] * p["S"] * k / 181,
            edge=p["edge"], tau=p["tau"], max_ev=p["max_ev"], mask=base_mask,
            shadows=p["shadows"], highlights=p["highlights"], tone_strength=p["tone_strength"],
            protect=p["protect"], protect_mm=p["protect_mm"], protect_thr=p["protect_thr"],
            mask_result=(total, structure))
        show = np.ones(reg.shape[:2], np.float32) if total is None else total
        image = lambda key: cv2.imdecode(np.frombuffer(self.get("/img/" + key)[0], np.uint8), 1)
        for key, index in (("layer", 1), ("layer2", 2)):
            pixels = next(value for band, value in layers if band == index)
            expected = np.repeat(U.u8(.5 + (pixels - .5) * show * 8)[..., None], 3, axis=2)
            np.testing.assert_array_equal(image(key), expected)
        self.assertNotIn("layerx", U.STATE["imgs"])
        self.assertNotIn("layer2x", U.STATE["imgs"])
        self.assertNotIn("layerm", U.STATE["imgs"])
        standalone = image("layer").copy()
        with patch.object(D, "band_stack", wraps=D.band_stack) as stack:
            self.post("/api/run", req)
            self.assertEqual(stack.call_count, 0, "Combine should reuse exact masked standalone bands")
        self.assertEqual(standalone.shape[:2], image("mask").shape[:2])

    def test_dnb_preview_tabs_expose_only_masked_bands(self):
        html = (Path(U.__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-k="layer" title="Band 1 preview with the skin mask applied and 8x contrast', html)
        self.assertIn('data-k="layer2" hidden title="Band 2 preview with the skin mask applied and 8x contrast', html)
        self.assertNotIn('data-k="layerx"', html)
        self.assertNotIn('data-k="layer2x"', html)

    def test_mix_reuse_preserves_skintone_preview(self):
        self.post("/api/load", {"path": str(self.image)})
        req = {**self.request(), "skintone": {"on": True, "hue_amount": 60}}
        self.post("/api/run", req)
        changed = {**req, "combine": {"fs_mix": 35, "dnb_mix": 75}}
        with patch.object(U, "skintone_layers", side_effect=AssertionError("recomputed")):
            self.post("/api/run", changed)
        cached = dict(U.STATE["imgs"])
        U.STATE["preview_cache"] = {}
        self.post("/api/run", changed)
        self.assertEqual(cached, U.STATE["imgs"])

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
        preview_result=self.get("/img/result")[0]
        U.STATE["preview_cache"]={}
        self.post("/api/run",req)
        self.assertEqual(self.get("/img/result")[0],preview_result)
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
        reg,k,mreg=U.get_region("whole",p["S"])
        preview_source=U.source_region(reg.shape,normalized=True)
        mask,total,structure=U.tool_mask(reg,mreg,p,p["S"]*k)
        show=np.ones(reg.shape[:2],np.float32) if total is None else total
        layers=U.skintone_layers(preview_source,show,p,k)
        _,fs_result=ST.composite(layers,show,p["skintone"],p["frequency"])
        _,dnb_result,_,_=D.band_stack(preview_source,p["S"]*k,p["bands"],p["strength"],p["second_band"],
            first_on=p["band1_on"],sigma=p["layer_blur_at_s181"]*p["S"]*k/181,
            edge=p["edge"],tau=p["tau"],max_ev=p["max_ev"],mask=mask,
            shadows=p["shadows"],highlights=p["highlights"],tone_strength=p["tone_strength"],
            protect=p["protect"],protect_mm=p["protect_mm"],protect_thr=p["protect_thr"],
            mask_result=(total,structure))
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
        self.assertAlmostEqual(calls[-1][4]["blur_strength"],55)
        p=U.parse_params(req)
        mask=U.full_skin_mask(p)
        unmixed=U.skintone_layers(U.STATE["source_img"],mask,p)
        for key in ("color","detail","blur"):
            np.testing.assert_array_equal(calls[-1][1][key],FS.rgb16(unmixed[key]))
        self.post("/api/pc/send_layers",{**req,"combine":{"fs_mix":0,"dnb_mix":70}})
        self.assertEqual(calls[-1][0],"fs")
        self.assertEqual(calls[-1][4]["band_mix"],70)
        self.assertEqual(calls[-1][4]["blur_strength"],0)
        for key in ("color","detail","blur"):
            np.testing.assert_array_equal(calls[-1][1][key],FS.rgb16(unmixed[key]))
        self.post("/api/pc/send_layers",{**req,"combine":{"fs_mix":0,"dnb_mix":0}})
        self.assertEqual(calls[-1][0],"fs")
        self.assertFalse(calls[-1][4]["bands"])
        self.post("/api/pc/send_layers",{**req,"combine":{"fs_mix":100,"dnb_mix":0}})
        self.assertEqual(calls[-1][0],"fs")
        self.assertIsNone(calls[-1][4]["band_mask8"])
        self.assertEqual(calls[-1][4]["blur_strength"],100)
        for key in ("color","detail","blur"):
            np.testing.assert_array_equal(calls[-1][1][key],FS.rgb16(unmixed[key]))
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
