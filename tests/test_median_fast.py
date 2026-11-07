"""Explicit fast-median method: scale, reconstruction, halos and every FS surface."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.dnb import frequency_separation as F, ui_server as U
from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge
from shp_photocraft_tools.shared.photocraft import ControlError
import test_dnb as T
import test_local_results as L


class MedianFastTests(unittest.TestCase):
    def test_small_fractional_preview_radius_and_reconstruction_use_original_source(self):
        img = np.random.default_rng(299).integers(0,65536,(41,57,3),dtype=np.uint16).astype(np.float32)/65535
        for radius in (.2,1,2.5,15):
            layers = F.split(img,"median_fast",radius)
            expected = cv2.medianBlur(np.round(img*255).astype(np.uint8),2*max(1,round(radius))+1)
            np.testing.assert_array_equal(layers["color"],expected.astype(np.float32)/255)
            np.testing.assert_array_equal(layers["detail"],np.clip(.5+(img-layers["color"])*.5,0,1))
            color,detail = (F.rgb16(layers[key]).astype(np.float32)/65535 for key in ("color","detail"))
            self.assertLessEqual(float(np.max(np.abs(np.clip(color+2*detail-1,0,1)-img)))*65535,1.01)

    def test_actual_parallel_stripes_match_single_filter_at_seams_and_outer_borders(self):
        img = np.random.default_rng(87).random((4096,2048,3),dtype=np.float32)
        q = np.round(img*255).astype(np.uint8)
        counts = []
        def executor(*args,**kwargs):
            counts.append(kwargs["max_workers"])
            return ThreadPoolExecutor(*args,**kwargs)
        for r in (15,100):
            with patch.object(F,"ThreadPoolExecutor",side_effect=executor):
                actual = F.median_fast(img,r)
            expected = cv2.medianBlur(q,2*r+1).astype(np.float32)/255
            np.testing.assert_array_equal(actual,expected)
        self.assertEqual(counts,[2,2])
        self.assertFalse(any(t.name.startswith("fs-median") for t in threading.enumerate()))

    def test_validation_defaults_radius_scaling_and_native_refusal(self):
        self.assertEqual(F.settings({})["method"],"gaussian")
        for method in ("gaussian","median","median_fast"):
            f=F.settings({"S":452.5,"frequency":{"method":method,"radius_at_s181":6,"blur_percent":200}})
            self.assertEqual(f["radius"],15)
            self.assertEqual(f["blur_radius"],30)
        with self.assertRaises(ValueError):
            F.settings({"frequency":{"method":"fast"}})
        for a,r in ((np.ones((3,3,3),np.float32)*1.1,15),
                    (np.ones((3,3,3),np.float32)*np.nan,15),
                    (np.ones((3,3,3),np.float32),0)):
            with self.assertRaises(ValueError):
                F.median_fast(a,r)
        with self.assertRaisesRegex(ControlError,"Tools-only"):
            Bridge.test_native_frequency(None,{},None,"median_fast",15)


class MedianFastApiTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    get = T.DnbTests.get
    post = T.DnbTests.post
    send = L.LocalResultTests.send

    def test_preview_save_local_send_photocraft_send_and_presets_retain_new_method(self):
        received = []
        class FakeBridge:
            def fetch(inner,dest):
                return self.image,{"index":1,"name":self.image.name}
            def send_skintone(inner,layers,settings,mask,doc,icc,*,blur_future=None,transfer=None,blur_strength=100,blur_masked=True):
                if blur_future is not None:
                    layers={**layers,"blur":blur_future.result()}
                received.append((layers,mask,transfer))
                return {"layer":1,"document":doc["name"]}
        U.BRIDGE["b"] = FakeBridge()
        p = {**T.PARAMS,"mode":"frequency","frequency":{"method":"median_fast","radius":15,"blur_radius":30},
             "skintone":{"hue_amount":0},"precache_mask":False,
             "frequency_transfer":{"format":"tiff","compressed":False,"opaque_alpha":False,"parallel":False}}
        self.post("/api/load",{"path":str(self.image)})
        self.post("/api/run",p)
        with Image.open(BytesIO(self.get("/img/color")[0])) as image:
            self.assertEqual(image.size,(128,96))
        saved = self.post("/api/save",p)
        self.assertIn("color.png",saved["files"])
        local = self.send(p)
        self.assertTrue(local["saved"])
        self.post("/api/pc/fetch",{})
        self.assertFalse(hasattr(U,"api_native_fs_test"))
        self.post("/api/pc/send",p)
        layers,mask,transfer = received[0]
        expected = F.rgb16(F.split(U.STATE["source_img"],"median_fast",15)["color"])
        np.testing.assert_array_equal(layers["color"],expected)
        self.assertTrue(all(a.dtype==np.uint16 for a in layers.values()))
        self.assertEqual(transfer,p["frequency_transfer"])
        response = self.post("/api/presets/save",{"name":"fast","settings":p})
        self.assertEqual(response["presets"]["fast"]["frequency"]["method"],"median_fast")
        self.assertEqual(response["presets"]["fast"]["frequency_transfer"],p["frequency_transfer"])
