"""Independent D&B band inversion, bypass, exports and native stack parity."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from shp_portrait_tools.modules.skin import dodge_burn as D, ui_server as U
import test_skintone as T
import test_dnb_transfer as DT


class BandTests(unittest.TestCase):
    def test_layer_only_process_preserves_change_and_mask_diagnostics(self):
        image = np.random.default_rng(46).uniform(.2, .8, (48, 64, 3)).astype(np.float32)
        mask = np.tile(np.linspace(0, 1, 64, dtype=np.float32), (48, 1))
        for region in (None, mask):
            with self.subTest(masked=region is not None):
                args = (image, 181, [(1.5, 20, 1, .5)], .7)
                expected = D.process(*args, mask=region, protect=.3)
                with patch.object(D, "softlight", side_effect=AssertionError("unused process blend")):
                    actual = D.process(*args, mask=region, protect=.3, render_result=False)
                self.assertIsNone(actual[1])
                for index in (0, 2, 3, 4):
                    np.testing.assert_equal(actual[index], expected[index])

    def test_layer_only_skips_blending_and_preserves_layers_masks_and_diagnostics(self):
        image = np.random.default_rng(45).uniform(.2, .8, (48, 64, 3)).astype(np.float32)
        bands = [(1.5, 20, 1, .5)]
        for masked in (False, True):
            kwargs = {"tau": .2}
            if masked:
                kwargs.update(mask=np.tile(np.linspace(0, 1, 64, dtype=np.float32), (48, 1)),
                              protect=.3, protect_mm=2)
            for first_on, second_on, invert in ((True, False, False), (False, True, False),
                                               (False, True, True), (True, True, True)):
                for sigma in (0, 2):
                    with self.subTest(masked=masked, first=first_on, second=second_on,
                                      invert=invert, sigma=sigma):
                        second = D.second_band_settings({"on": second_on, "invert": invert})
                        expected = D.band_stack(image, 181, bands, .7, second,
                                                first_on=first_on, sigma=sigma, **kwargs)
                        with patch.object(D, "softlight", side_effect=AssertionError("unused blend")), \
                             patch.object(D, "compute_change_band", wraps=D.compute_change_band) as analysis:
                            actual = D.band_stack(image, 181, bands, .7, second,
                                                  first_on=first_on, sigma=sigma,
                                                  render_result=False, **kwargs)
                        self.assertIsNone(actual[1])
                        self.assertEqual(analysis.call_count,
                                         (int(first_on) + int(second_on)) * (2 if masked else 1))
                        self.assertEqual([i for i, _ in actual[0]], [i for i, _ in expected[0]])
                        for (_, a), (_, b) in zip(actual[0], expected[0]):
                            np.testing.assert_array_equal(a, b)
                        np.testing.assert_equal(actual[2], expected[2])
                        np.testing.assert_equal(actual[3], expected[3])

    def test_legacy_disabled_exact_bypass_inversion_neutral_and_mask_reuse(self):
        image=np.random.default_rng(44).uniform(.2,.8,(48,64,3)).astype(np.float32)
        bands=[(1.5,20,1,.5)]
        off=D.second_band_settings()
        legacy=D.process(image,181,bands,.7,tau=.2)
        layers,result,_,mask=D.band_stack(image,181,bands,.7,off,tau=.2)
        np.testing.assert_array_equal(layers[0][1],legacy[0])
        np.testing.assert_array_equal(result,legacy[1])
        active=D.second_band_settings({"on":True,"strength":.7,"lo":1.5,"hi":20})
        reduction=D.band_stack(image,181,bands,.7,active,first_on=False,tau=.2)
        enhancement=D.band_stack(image,181,bands,.7,{**active,"invert":True},first_on=False,tau=.2)
        np.testing.assert_array_equal(enhancement[0][0][1],1-reduction[0][0][1])
        self.assertLess(np.sum((reduction[1]-image)*(enhancement[1]-image)),0)
        with patch.object(D,"process",side_effect=AssertionError("disabled band ran")):
            layers,unchanged,_,_=D.band_stack(image,181,bands,.7,off,first_on=False)
        self.assertEqual(layers,[])
        np.testing.assert_array_equal(unchanged,image)
        flat=np.full_like(image,.4)
        normal=D.band_stack(flat,181,bands,.7,active,first_on=False)
        invert=D.band_stack(flat,181,bands,.7,{**active,"invert":True},first_on=False)
        np.testing.assert_allclose(normal[0][0][1],.5,atol=2e-6)
        np.testing.assert_allclose(invert[1],flat,atol=2e-5)
        for raw in ({"on":1},{"invert":"yes"},{"strength":float("nan")},{"lo":50,"hi":2}):
            with self.assertRaises(ValueError):D.second_band_settings(raw)
        with patch.object(D,"process",wraps=D.process) as process:
            D.band_stack(image,181,bands,.7,active,first_on=False)
            self.assertEqual(process.call_count,1)
        before=image.copy()
        D.band_stack(image,181,bands,.7,active,sigma=2,mask=np.full(image.shape[:2],.5,np.float32))
        np.testing.assert_array_equal(image,before)


class LocalTests(unittest.TestCase):
    setUp=T.SkintoneApiTests.setUp
    close_server=T.SkintoneApiTests.close_server
    get=T.SkintoneApiTests.get
    post=T.SkintoneApiTests.post

    def test_preview_save_local_send_cache_and_independent_settings(self):
        self.post("/api/load",{"path":str(self.image)})
        req={**T.T.PARAMS,"mode":"dnb","S":181,"view":"whole","precache_mask":False,
             "band1_on":False,"second_band":{"on":True,"invert":True,"lo":2,"hi":20,"strength":.7}}
        preview=self.post("/api/run",req)
        self.assertIn("layer2",U.STATE["imgs"])
        self.assertTrue(self.post("/api/run",req)["cached"])
        self.assertFalse(self.post("/api/run",{**req,"second_band":{**req["second_band"],"invert":False}}).get("cached",False))
        self.post("/api/run",req)
        p=U.parse_params(req)
        layers,result,total=U.dnb_full_result(p,layered=True)
        self.assertEqual([i for i,_ in layers],[2])
        for key,pixels in (("layer2",layers[0][1]),("layer2m",.5+(layers[0][1]-.5)*total)):
            actual=cv2.imdecode(np.frombuffer(self.get("/img/"+key+"x")[0],np.uint8),0)
            np.testing.assert_array_equal(actual,U.u8(.5+(pixels-.5)*8))
            np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(self.get("/img/"+key)[0],np.uint8),0),U.u8(pixels))
        saved=self.post("/api/save",req)
        folder=Path(saved["dir"])
        layerfile=next(folder.glob("*_4_softlight_band2.png"))
        np.testing.assert_array_equal(cv2.imread(str(layerfile),-1)[...,0],np.round(layers[0][1]*65535).astype(np.uint16))
        self.assertTrue(json.loads((folder/"settings.json").read_text())["second_band"]["invert"])
        self.assertFalse(list(folder.glob("*_3_softlight_layer.png")))
        self.assertEqual(U.mask_settings_key(p,None),U.mask_settings_key(U.parse_params({**req,"second_band":{"on":False}}),None))
        response=U.api_send_result(req)
        self.assertTrue(Path(response["path"]).is_file())
        with self.assertRaisesRegex(ValueError,"boolean"):U.parse_params({**req,"band1_on":1})


@unittest.skipUnless(shutil.which("photocraft-cli"),"photocraft-cli unavailable")
class NativeTests(unittest.TestCase):
    setUp=DT.TransferTests.setUp
    close_process=DT.TransferTests.close_process
    new_target=DT.TransferTests.new_target
    save_pixels=DT.TransferTests.save_pixels

    def test_separate_layers_order_masks_profile_and_native_pixels(self):
        for profile in ("srgb","display-p3","prophoto-compat"):
            target=self.new_target(profile)
            path,_=self.bridge.fetch(self.root/"band-input")
            from PIL import Image
            with Image.open(path) as image:icc=image.info["icc_profile"]
            source=cv2.imread(str(path),-1)[...,:3].astype(np.float32)/65535
            skin=np.tile(np.linspace(0,1,8,dtype=np.float32),(8,1))
            for invert in (False,True):
                layers,result,_,mask=D.band_stack(source,181,[(1.5,20,1,.5)],.7,
                    D.second_band_settings({"on":True,"invert":invert,"strength":.5}),mask=skin,tau=.2)
                payload=[(index,np.repeat(np.round(pixels*65535).astype(np.uint16)[...,None],3,axis=2)) for index,pixels in layers]
                sent=self.bridge.send_bands(payload,U.u8(mask),target,source_icc=icc)
                rows=self.client.run("document.inspect")["layers"]
                self.assertIn("band 2",rows[0]["name"])
                self.assertIn("band 1",rows[1]["name"])
                self.assertTrue(rows[0]["hasMask"] and rows[1]["hasMask"])
                actual=self.save_pixels("band-result.png")
                np.testing.assert_allclose(actual[...,:3],np.round(result*65535),atol=4)
                for lid in reversed(sent["layers"]):self.client.run("layer.delete",layer=lid)
            self.client.run("file.close",document=target["index"])

    def test_second_insert_failure_rolls_back_completed_band(self):
        from shp_portrait_tools.modules.skin.photocraft_bridge import ControlError
        target=self.new_target("srgb")
        path,_=self.bridge.fetch(self.root/"rollback-input")
        from PIL import Image
        with Image.open(path) as image:icc=image.info["icc_profile"]
        before=self.client.run("layer.list")
        pixels=np.full((8,8,3),30000,np.uint16)
        send=self.bridge.send
        def fail_second(*args,**kwargs):
            if "band 2" in kwargs.get("name",""):raise ControlError("synthetic second-band import failure")
            return send(*args,**kwargs)
        with patch.object(self.bridge,"send",side_effect=fail_second),self.assertRaisesRegex(ControlError,"synthetic"):
            self.bridge.send_bands([(1,pixels),(2,pixels)],np.full((8,8),255,np.uint8),target,source_icc=icc)
        self.assertEqual(self.client.run("layer.list"),before)
        self.assertEqual(list(self.bridge.exchange.iterdir()),[])
