"""Directional geometry, disabled paths, local outputs and isolated native parity."""
import itertools
import json
from pathlib import Path
import shutil
import threading
import unittest
import weakref
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.dnb import directional_blur as D, ui_server as U, skintone as ST
import test_fs_pipeline as FP
import test_skintone as T
import test_dnb_transfer as DT


def pose(shape):
    h, w = shape[:2]
    kp, sc = np.zeros((17, 2), np.float32), np.ones(17, np.float32)
    kp[11], kp[13] = (w * .2, h * .25), (w * .45, h * .8)
    kp[12], kp[14] = (w * .8, h * .25), (w * .55, h * .8)
    kp[15], kp[16] = (w * .2, h * .95), (w * .8, h * .95)
    kp[5], kp[7], kp[9] = (w * .3, h * .2), (w * .15, h * .4), (w * .35, h * .6)
    kp[6], kp[8], kp[10] = (w * .7, h * .2), (w * .85, h * .4), (w * .65, h * .6)
    return {"kp": kp, "sc": sc}


class GeometryTests(unittest.TestCase):
    def test_upper_arm_width_is_perpendicular_with_taper_range_and_crop_scale(self):
        shape=(320,320)
        image=np.full((*shape,3),.4,np.float32)
        skin=np.ones(shape,np.float32)
        for delta in ([120,0],[0,120],[90,120],[-90,120]):
            axis=np.asarray(delta,dtype=np.float64)
            normal=np.array([-axis[1],axis[0]])/np.linalg.norm(axis)
            start=np.array([160,160])-axis/2
            target=pose(shape)
            target["kp"][5],target["kp"][7]=start,start+axis
            p=D.settings({"directional":{"on":True,"left":False,"right":False,
                "left_upper_arm":True,"pairs":{"upper_arm":{
                    "width":40,"taper":50,"start":20,"end":80,"feather":0,"distance":0}}}})
            _,a,b=D.segments(p,target,shape)[0]
            np.testing.assert_allclose(a,start+axis*.2)
            np.testing.assert_allclose(b,start+axis*.8)
            raster,mask=D.apply(image,skin,p,target,181)
            # Independent cross-product distance to the line, not screen X displacement.
            y,x=np.mgrid[:shape[0],:shape[1]]
            displacement=np.stack((x-a[0],y-a[1]),axis=-1)
            d=b-a
            projection=(displacement*d).sum(-1)/np.dot(d,d)
            perpendicular=np.abs(displacement[...,0]*d[1]-displacement[...,1]*d[0])/np.linalg.norm(d)
            interior=(projection>=0)&(projection<=1)
            expected=np.clip(20*(1-projection*.5)+.5-perpendicular,0,1)
            np.testing.assert_allclose(mask[interior],expected[interior],atol=1e-6)
            # At midpoint the full tapered width is 30px, on either normal side.
            centre=(a+b)/2
            for distance in (-20,-15,-10,0,10,15,20):
                point=centre+normal*distance
                if np.allclose(point,np.round(point)):
                    px,py=np.round(point).astype(int)
                    self.assertAlmostEqual(float(mask[py,px]),float(np.clip(15+.5-abs(distance),0,1)),places=5)
            _,cropped=D.apply(image[30:290,30:290],skin[30:290,30:290],p,target,181,
                               origin=(30,30),source_shape=image.shape)
            np.testing.assert_array_equal(cropped,mask[30:290,30:290])
            small=np.full((160,160,3),.4,np.float32)
            _,scaled_mask=D.apply(small,np.ones((160,160),np.float32),p,target,362,
                                  source_shape=image.shape,scale=.5)
            expected_scaled=D.region(small.shape,a*.5,b*.5,40,50)
            np.testing.assert_array_equal(scaled_mask,expected_scaled)
            np.testing.assert_allclose(raster,image,atol=1e-7)

    def test_independent_region_strength_feather_endpoints_and_overlap_math(self):
        image=np.random.default_rng(703).uniform(.2,.8,(120,120,3)).astype(np.float32)
        skin=np.ones(image.shape[:2],np.float32)
        target=pose(image.shape)
        target["kp"][[11,13,5,7]]=[[25,30],[90,90],[90,30],[25,90]]
        p=D.settings({"directional":{"on":True,"right":False,"left_upper_arm":True,
            "pairs":{"upper_leg":{"distance":12,"width":30,"taper":20,"strength":20,"feather":1,"start":10,"end":90},
                     "upper_arm":{"distance":20,"width":40,"taper":50,"strength":80,"feather":4,"start":20,"end":80}}}})
        self.assertEqual(D.output_strength(p),100)
        raster,mask=D.apply(image,skin,p,target,181)
        total=np.zeros(image.shape[:2],np.float32)
        delta=np.zeros_like(image)
        union=np.zeros_like(total)
        for key,pair in (("left","upper_leg"),("left_upper_arm","upper_arm")):
            single={**p,**{k:k==key for k in D.SEGMENTS}}
            candidate,soft=D.apply(image,skin,single,target,181)
            strength=p["pairs"][pair]["strength"]/100
            delta+=(candidate-image)*strength*soft[...,None]
            total+=soft
            np.maximum(union,soft,out=union)
            _,a,b=D.segments(single,target,image.shape)[0]
            ia,ib=D.SEGMENTS[key][1:]
            origin=target["kp"][ia]
            axis=target["kp"][ib]-origin
            np.testing.assert_allclose(a,origin+axis*p["pairs"][pair]["start"]/100)
            np.testing.assert_allclose(b,origin+axis*p["pairs"][pair]["end"]/100)
        expected=image.copy()
        np.divide(delta,total[...,None],out=delta,where=total[...,None]>0)
        expected+=delta
        np.testing.assert_allclose(raster,expected,atol=5e-4)
        np.testing.assert_allclose(mask,union,atol=1e-6)
        # Native 100% opacity applies the same effective per-region strengths.
        actual=image+(raster-image)*mask[...,None]*D.output_strength(p)/100
        np.testing.assert_allclose(actual,image+delta*union[...,None],atol=5e-4)
        equal=D.settings({"directional":{**p,"pairs":{
            pair:{**v,"strength":37,"feather":2} for pair,v in p["pairs"].items()}}})
        self.assertEqual(D.output_strength(equal),37)
        zero=D.settings({"directional":{"on":True,"right":False,"pairs":{"upper_leg":{"strength":0}}}})
        with patch.object(D,"motion",side_effect=AssertionError("zero strength filtering")):
            untouched,_=D.apply(image,skin,zero,target,181)
        np.testing.assert_allclose(untouched,image,atol=1e-7)
        for values in ({"start":99,"end":99},{"strength":101},{"feather":51},{"end":0},{"start":True}):
            with self.assertRaises(ValueError):
                D.settings({"directional":{"pairs":{"upper_leg":values}}})

    def test_torso_whole_box_percentage_taper_crop_and_geometry_guards(self):
        target=pose((100,100))
        target["kp"][[5,6,11,12]]=[[20,10],[80,10],[30,80],[70,80]]
        target["sc"][:]=0
        p=D.settings({"directional":{"on":True,"left":False,"right":False,"torso":True,
                                    "distance":0,"feather":0}})
        quad,a,b=D.torso_geometry(p,target,(100,100))
        np.testing.assert_array_equal(quad,[[20,10],[80,10],[70,80],[30,80]])
        np.testing.assert_array_equal(a,[50,10])
        np.testing.assert_array_equal(b,[50,80])
        image=np.full((100,100,3),.4,np.float32)
        skin=np.ones(image.shape[:2],np.float32)
        raster,mask=D.apply(image,skin,p,target,181)
        np.testing.assert_allclose(raster,image,atol=1e-7)
        np.testing.assert_array_equal(mask,D.torso_region(image.shape,quad))
        self.assertEqual(mask[11,21],1)
        self.assertEqual(mask[79,31],1)
        self.assertEqual(mask[9,50],0)
        self.assertEqual(mask[81,50],0)
        narrowed=D.settings({"directional":{**p,"torso_controls":{"distance":0,"width":50,"taper":50},
                                          "start":25,"end":75}})
        q,a,b=D.torso_geometry(narrowed,target,image.shape,origin=(10,5),scale=.5)
        self.assertAlmostEqual(np.linalg.norm(q[1]-q[0]),55*.5*.875*.5)
        self.assertAlmostEqual(np.linalg.norm(q[2]-q[3]),45*.5*.625*.5)
        np.testing.assert_array_equal(a,[20,11.25])
        np.testing.assert_array_equal(b,[20,28.75])
        # A rotated quad retains its full-region area and proximal/distal direction.
        rotated={"kp":target["kp"][:,::-1].copy()}
        q,ra,rb=D.torso_geometry(p,rotated,image.shape)
        np.testing.assert_array_equal(ra,[10,50])
        np.testing.assert_array_equal(rb,[80,50])
        np.testing.assert_allclose(D.torso_region(image.shape,q),mask.T,atol=1e-6)
        for problem in ("missing","nan","outside","crossed","degenerate"):
            bad={"kp":target["kp"].copy()}
            if problem=="missing":
                bad["kp"]=bad["kp"][:12]
            elif problem=="nan":
                bad["kp"][5,0]=np.nan
            elif problem=="outside":
                bad["kp"][12,1]=100
            elif problem=="crossed":
                bad["kp"][[11,12]]=bad["kp"][[12,11]]
            else:
                bad["kp"][6]=bad["kp"][5]
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(D.GeometryUnavailable,"torso"):
                    D.torso_geometry(p,bad,image.shape)
                effective,missing=D.effective_settings(p,bad,image.shape)
                self.assertFalse(effective["torso"])
                self.assertIn("torso",missing)
                raster,coverage=D.apply(image,skin,p,bad,181)
                np.testing.assert_array_equal(raster,image)
                self.assertFalse(coverage.any())
        for controls in (None,{"width":0},{"width":201},{"width":True},{"taper":91},{"distance":float("nan")}):
            with self.assertRaises((ValueError,TypeError)):
                D.settings({"directional":{"torso_controls":controls}})

    def test_torso_masked_motion_overlap_border_crop_and_disabled_bypass(self):
        image=np.random.default_rng(67).uniform(.2,.8,(160,160,3)).astype(np.float32)
        target=pose(image.shape)
        target["kp"][[5,6,11,12]]=[[0,5],[159,30],[20,100],[140,120]]
        skin=np.ones(image.shape[:2],np.float32)
        skin[:,:10]=0
        skin[:,10:30]=.3
        p=D.settings({"directional":{"on":True,"torso":True,"width":20,"distance":12,"feather":1,
                                    "torso_controls":{"distance":24,"width":100,"taper":30}}})
        combined,mask=D.apply(image,skin,p,target,181)
        weighted=np.zeros_like(image)
        weights=np.zeros(image.shape[:2],np.float32)
        union=np.zeros(image.shape[:2],np.float32)
        for key in ("left","right","torso"):
            single={**p,**{k:k==key for k in (*D.SEGMENTS,"torso")}}
            raster,_=D.apply(image,skin,single,target,181)
            if key=="torso":
                quad,_,_=D.torso_geometry(p,target,image.shape)
                raw=D.torso_region(image.shape,quad)
            else:
                _,a,b=D.segments(single,target,image.shape)[0]
                raw=D.region(image.shape,a,b,20)
            soft=cv2.GaussianBlur(raw,(9,9),1,borderType=cv2.BORDER_CONSTANT)
            weighted+=raster*soft[...,None]
            weights+=soft
            np.maximum(union,raw,out=union)
        active=weights>0
        np.divide(weighted,weights[...,None],out=weighted,where=active[...,None])
        np.testing.assert_allclose(combined[active],weighted[active],atol=5e-4)
        np.testing.assert_allclose(mask,cv2.GaussianBlur(union,(9,9),1,borderType=cv2.BORDER_CONSTANT)*skin,atol=1e-6)
        self.assertFalse(mask[:,:10].any())
        crop,crop_mask=D.apply(image[10:150,10:150],skin[10:150,10:150],p,target,181,
                               origin=(10,10),source_shape=image.shape)
        np.testing.assert_allclose(crop[20:120,20:120],combined[30:130,30:130],atol=5e-4)
        np.testing.assert_allclose(crop_mask[20:120,20:120],mask[30:130,30:130],atol=1e-6)
        color=np.array([.3,.5,.7],np.float32)
        solid=np.broadcast_to(color,image.shape).copy()
        solid[skin==0]=[1,0,0]
        raster,mask=D.apply(solid,skin,p,target,181)
        np.testing.assert_allclose(raster[mask>0],np.broadcast_to(color,raster[mask>0].shape),atol=3e-6)
        with patch.object(D,"torso_geometry",side_effect=AssertionError("disabled torso")):
            D.apply(image,skin,{**p,"torso":False},target,181)
            self.assertIsNone(D.apply(image,skin,{**p,"on":False},None,181))

    def test_pair_migration_independence_validation_and_overlap(self):
        old={"on":True,"distance":17,"width":23,"taper":35,"feather":1}
        migrated=D.settings({"directional":old})
        for pair in D.PAIRS:
            self.assertEqual(migrated["pairs"][pair],{"distance":17,"width":23,"taper":35,
                                                    "strength":100,"feather":1,"start":0,"end":100})
        image=np.random.default_rng(17).uniform(.1,.9,(100,100,3)).astype(np.float32)
        skin=np.ones(image.shape[:2],np.float32)
        target=pose(image.shape)
        for actual,expected in zip(D.apply(image,skin,migrated,target,181),D.apply(image,skin,old | D.DEFAULTS | old,target,181)):
            np.testing.assert_array_equal(actual,expected)
        changed=D.settings({"directional":{**old,"left_upper_arm":True,"right_upper_arm":True,
                         "pairs":{"upper_arm":{"distance":3,"width":10,"taper":80}}}})
        self.assertEqual(changed["pairs"]["upper_leg"],migrated["pairs"]["upper_leg"])
        raster,mask=D.apply(image,skin,changed,target,181)
        self.assertTrue(np.isfinite(raster).all())
        self.assertGreater(mask.sum(),0)
        weighted=np.zeros_like(image)
        weights=np.zeros(image.shape[:2],np.float32)
        union=np.zeros(image.shape[:2],np.float32)
        for pair,keys in D.PAIRS.items():
            for key in keys:
                if not changed[key]:
                    continue
                single={**changed,**{k:k==key for k in D.SEGMENTS}}
                pixels,_=D.apply(image,skin,single,target,181)
                _,a,b=D.segments(single,target,image.shape)[0]
                config=changed["pairs"][pair]
                coverage=D.region(image.shape,a,b,config["width"],config["taper"])
                soft=cv2.GaussianBlur(coverage,(9,9),1,borderType=cv2.BORDER_CONSTANT)
                weighted+=pixels*soft[...,None]
                weights+=soft
                np.maximum(union,coverage,out=union)
        active=weights>0
        np.divide(weighted,weights[...,None],out=weighted,where=active[...,None])
        np.testing.assert_allclose(raster[active],weighted[active],atol=4e-4)
        np.testing.assert_allclose(mask,cv2.GaussianBlur(union,(9,9),1,borderType=cv2.BORDER_CONSTANT),atol=1e-6)
        for invalid in (None,[],{"bad":{}},{"upper_arm":None},{"upper_arm":{"distance":True}},
                        {"forearm":{"width":0}},{"lower_leg":{"taper":91}},{"upper_leg":{"width":float("nan")}}):
            with self.subTest(invalid=invalid),self.assertRaises((ValueError,TypeError)):
                D.settings({"directional":{"pairs":invalid}})
        unsafe=D.settings({"directional":{"on":True,"left_upper_arm":True,"left":False,"right":False,
                              "pairs":{"upper_arm":{"width":1000}}}})
        with self.assertRaisesRegex(ValueError,"safe processing"):
            D.apply(image,skin,unsafe,target,2000)

    def test_all_limb_axes_proximal_ranges_and_individual_validation(self):
        defaults = D.settings({})
        self.assertEqual({key for key in D.SEGMENTS if defaults[key]}, {"left", "right"})
        image = np.full((100, 100, 3), .4, np.float32)
        skin = np.ones(image.shape[:2], np.float32)
        for key, (label, ia, ib) in D.SEGMENTS.items():
            p = D.settings({"directional": {"on": True, **{k: k == key for k in D.SEGMENTS},
                                           "start": 25, "end": 75, "width": 8,
                                           "taper": 50, "feather": 0, "distance": 4}})
            for a, b in (([10, 50], [90, 50]), ([90, 50], [10, 50]),
                         ([50, 10], [50, 90]), ([10, 10], [90, 90])):
                target = pose(image.shape)
                target["kp"][ia], target["kp"][ib] = a, b
                line, start, end = D.segments(p, target, image.shape)[0]
                axis = np.array(b) - a
                self.assertEqual(line, label)
                np.testing.assert_array_equal(start, np.array(a) + axis * .25)
                np.testing.assert_array_equal(end, np.array(a) + axis * .75)
                scaled = D.segments(p, target, image.shape, origin=(10, 20), scale=.5)[0]
                np.testing.assert_array_equal(scaled[1], (start - [10, 20]) * .5)
                raster, mask = D.apply(image, skin, p, target, 181)
                np.testing.assert_array_equal(mask, D.region(image.shape, start, end, 8, 50))
                np.testing.assert_allclose(raster, image, atol=2e-7)
            target = pose(image.shape)
            target["sc"][:] = 0
            self.assertEqual(len(D.segments(p, target, image.shape)), 1)
            target.pop("sc")
            self.assertEqual(len(D.segments(p, target, image.shape)), 1)
            for joint in (ia, ib):
                for problem in ("nan", "outside", "truncated"):
                    target = pose(image.shape)
                    if problem == "nan":
                        target["kp"][joint, 0] = np.nan
                    elif problem == "outside":
                        target["kp"][joint, 1] = 100
                    else:
                        target["kp"] = target["kp"][:joint]
                    with self.subTest(segment=key, joint=joint, problem=problem), self.assertRaisesRegex(ValueError, label.lower()):
                        D.segments(p, target, image.shape)
            target = pose(image.shape)
            target["kp"][ib] = target["kp"][ia]
            with self.assertRaisesRegex(ValueError, "degenerate"):
                D.segments(p, target, image.shape)
            with self.assertRaisesRegex(ValueError, "boolean"):
                D.settings({"directional": {key: 1}})

    def test_legacy_settings_exact_output_and_all_limb_overlap_borders(self):
        image = np.random.default_rng(81).uniform(.2, .8, (128, 128, 3)).astype(np.float32)
        skin = np.ones(image.shape[:2], np.float32)
        legacy = D.settings({"directional": {"on": True, "distance": 8, "width": 30,
                                            "feather": 1, "taper": 40}})
        old = {k: v for k, v in legacy.items() if k in {"on", "left", "right", *D.BOUNDS}}
        for actual, expected in zip(D.apply(image, skin, legacy, pose(image.shape), 181),
                                    D.apply(image, skin, old, pose(image.shape), 181)):
            np.testing.assert_array_equal(actual, expected)
        all_on = {**legacy, **{key: True for key in D.SEGMENTS}}
        target = pose(image.shape)
        target["kp"][5], target["kp"][7], target["kp"][9] = (0, 0), (64, 64), (127, 127)
        combined, mask = D.apply(image, skin, all_on, target, 181)
        shape = image.shape[:2]
        weights = np.zeros(shape, np.float32)
        mixed = np.zeros_like(image)
        union = np.zeros(shape, np.float32)
        for key in D.SEGMENTS:
            p = {**all_on, **{k: k == key for k in D.SEGMENTS}}
            single, coverage = D.apply(image, skin, p, target, 181)
            _, a, b = D.segments(p, target, shape)[0]
            raw = D.region(shape, a, b, 30, 40)
            soft = cv2.GaussianBlur(raw, (9, 9), 1, borderType=cv2.BORDER_CONSTANT)
            np.maximum(union, raw, out=union)
            mixed += single * soft[..., None]
            weights += soft
        active = weights > 0
        np.divide(mixed, weights[..., None], out=mixed, where=active[..., None])
        np.testing.assert_allclose(combined[active], mixed[active], atol=4e-4)
        np.testing.assert_allclose(mask, cv2.GaussianBlur(union, (9, 9), 1, borderType=cv2.BORDER_CONSTANT), atol=1e-6)
        np.testing.assert_array_equal(combined[~active], image[~active])
        self.assertTrue(np.isfinite(combined).all())
        # Limb count must not allocate/retain a full image per segment.
        large = np.full((1080, 1920, 3), .4, np.float32)
        refs, sizes = [], []
        def filter_once(img, angle, distance, **kwargs):
            self.assertLessEqual(sum(ref() is not None for ref in refs), 1)
            result = img.copy()
            refs.append(weakref.ref(result))
            sizes.append(result.shape)
            return result
        with patch.object(D, "motion", side_effect=filter_once):
            raster, coverage = D.apply(large, np.ones(large.shape[:2], np.float32),
                                        {**all_on,"torso":True}, pose(large.shape), 181)
        self.assertEqual(len(sizes), 9)
        self.assertTrue(all(h <= 1080 and w <= 1920 for h, w, _ in sizes))
        np.testing.assert_allclose(raster, large, atol=2e-7)
        self.assertLessEqual(coverage.max(), 1)
        self.assertTrue(all(ref() is None for ref in refs))

    def test_masked_motion_excludes_clothing_normalizes_soft_support_and_empty_edges(self):
        skin_color = np.array([.28, .46, .68], np.float32)
        image = np.empty((81, 101, 3), np.float32)
        image[:] = [1, 0, .05]
        image[:, 35:66] = skin_color
        support = np.zeros(image.shape[:2], np.float32)
        support[:, 35:66] = np.linspace(.1, 1, 31)
        unmasked = D.motion(image, 0, 40)
        self.assertGreater(np.max(np.abs(unmasked[:, 35:66] - skin_color)), .1)
        for angle in (0, 37, 90):
            actual = D.motion(image, angle, 40, support=support)
            np.testing.assert_allclose(actual[:, 35:66], np.broadcast_to(skin_color, (81, 31, 3)), atol=2e-6)
            # Same linear sampling operator on numerator and denominator, including rotated edges.
            denominator = D.motion(np.repeat(support[..., None], 3, axis=2), angle, 40)[..., 0]
            numerator = D.motion(image * support[..., None], angle, 40)
            expected = image.copy()
            np.divide(numerator, denominator[..., None], out=expected,
                      where=denominator[..., None] > 1e-6)
            np.testing.assert_array_equal(actual, expected)
            np.testing.assert_array_equal(D.motion(image, angle, 40, support=np.zeros_like(support)), image)
        np.testing.assert_array_equal(D.motion(image, 25, 0, support=support), image)
        with self.assertRaisesRegex(ValueError, "finite sampling"):
            D.motion(image, 0, 10, support=np.full(support.shape, np.nan))
        edge_image = np.broadcast_to(skin_color, image.shape).copy()
        np.testing.assert_allclose(D.motion(edge_image, 42, 60, support=support),
                                   edge_image, atol=2e-6)

    def test_tapered_regions_ignore_non_skin_color_and_preserve_no_support(self):
        target = pose((100, 100))
        p = D.settings({"directional": {"on": True, "width": 50, "distance": 40,
                                       "taper": 50, "feather": 2}})
        skin = np.zeros((100, 100), np.float32)
        for _, a, b in D.segments(p, target, skin.shape):
            np.maximum(skin, D.region(skin.shape, a, b, 20, 50), out=skin)
        color = np.array([.3, .5, .7], np.float32)
        image = np.broadcast_to(color, (100, 100, 3)).copy()
        image[skin == 0] = [1, 0, 0]
        raster, mask = D.apply(image, skin, p, target, 181)
        np.testing.assert_allclose(raster[mask > 0], np.broadcast_to(color, raster[mask > 0].shape), atol=2e-6)
        altered = image.copy()
        altered[skin == 0] = [0, 1, 0]
        other, other_mask = D.apply(altered, skin, p, target, 181)
        np.testing.assert_array_equal(other_mask, mask)
        np.testing.assert_array_equal(other[mask > 0], raster[mask > 0])
        empty, empty_mask = D.apply(image, np.zeros_like(skin), p, target, 181)
        np.testing.assert_allclose(empty, image, atol=1e-7)
        self.assertFalse(empty_mask.any())

    def test_settings_and_missing_or_invalid_selected_pose(self):
        self.assertFalse(D.settings({})["on"])
        self.assertEqual(D.settings({})["end"], 100)
        maximum = D.settings({"directional": {"width": 1000, "feather": 50}})
        self.assertEqual(D.scaled(maximum, 362)["width"], 2000)
        self.assertEqual(D.scaled(maximum, 362)["feather"], 100)
        for invalid in (None, {"on": 1}, {"left": "false"}, {"distance": float("nan")},
                        {"width": 0}, {"strength": 101}, {"feather": -1}, {"distance": True},
                        {"width": 1001}, {"feather": 51}, {"length": 9}, {"length": 201},
                        {"length": True}, {"length": float("nan")},
                        {"taper": -1}, {"taper": 91}, {"taper": float("nan")}, {"taper": True},
                        {"on": True, "left": False, "right": False}):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, TypeError)):
                D.settings({"directional": invalid})
        p = D.settings({"directional": {"on": True, "right": False}})
        with self.assertRaisesRegex(ValueError, "no pose"):
            D.segments(p, None, (100, 100))
        for problem in ("outside", "nan", "degenerate"):
            bad = pose((100, 100))
            if problem == "outside":
                bad["kp"][11] = (-1, 10)
            elif problem == "nan":
                bad["kp"][13, 1] = np.nan
            else:
                bad["kp"][13] = bad["kp"][11]
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                D.segments(p, bad, (100, 100))
        bad = pose((100, 100))
        bad["sc"][14] = .1
        self.assertEqual(len(D.segments(p, bad, (100, 100))), 1)
        with self.assertRaisesRegex(ValueError, "safe processing"):
            D.scaled(p, 100000)
        for key, value in (("width", 1000), ("feather", 50)):
            with self.subTest(cap=key), self.assertRaisesRegex(ValueError, "safe processing"):
                D.scaled(D.settings({"directional": {key: value, "distance": 0}}), 2000)
        image = np.full((32, 32, 3), .4, np.float32)
        skin = np.ones((32, 32), np.float32)
        raster, mask = D.apply(image, skin, {**maximum, "on": True, "distance": 0},
                               pose(image.shape), 181)
        self.assertTrue(np.isfinite(mask).all())
        self.assertTrue(np.all((mask > 0) & (mask <= 1)))
        np.testing.assert_allclose(raster, image, atol=1e-7)

    def test_length_hip_anchor_taper_orientation_legacy_masks_and_borders(self):
        image = np.random.default_rng(37).uniform(.2, .8, (100, 100, 3)).astype(np.float32)
        skin = np.ones(image.shape[:2], np.float32)
        target = pose(image.shape)
        target["kp"][11], target["kp"][13] = (20, 50), (60, 50)
        p = D.settings({"directional": {"on": True, "right": False, "width": 10,
                                       "distance": 8, "taper": 50, "feather": 0}})
        original = D.apply(image, skin, p, target, 181)
        legacy = D.apply(image, skin, {k: v for k, v in p.items() if k != "length"}, target, 181)
        for a, b in zip(original, legacy):
            np.testing.assert_array_equal(a, b)
        areas = []
        for length, end in ((25, 30), (50, 40), (100, 60)):
            changed = D.settings({"directional":{**p,"end":length,"pairs":{}}})
            _, a, b = D.segments(changed, target, image.shape)[0]
            np.testing.assert_array_equal(a, [20, 50])
            np.testing.assert_array_equal(b, [end, 50])
            scaled_line = D.segments(changed, target, image.shape, origin=(10, 20), scale=.5)[0]
            np.testing.assert_array_equal(scaled_line[1], [5, 15])
            np.testing.assert_array_equal(scaled_line[2], [(end - 10) / 2, 15])
            raster, mask = D.apply(image, skin, changed, target, 181)
            expected = D.region(image.shape, a, b, 10, 50)
            np.testing.assert_array_equal(mask, expected)
            self.assertAlmostEqual(mask[:, 20].sum(), 10)
            if end < 100:
                self.assertAlmostEqual(mask[:, end].sum(), 5)
            self.assertTrue(np.isfinite(raster).all())
            np.testing.assert_array_equal(raster[mask == 0], image[mask == 0])
            areas.append(mask.sum())
        self.assertTrue(areas[0] < areas[1] < areas[2])
        with patch.object(D, "segments", side_effect=AssertionError("disabled length geometry")):
            self.assertIsNone(D.apply(image, skin, {**p, "on": False, "length": 200}, None, 181))
        huge = pose((20000, 20000))
        with self.assertRaisesRegex(ValueError, "region length.*safe processing"):
            D.segments({**p, "end": 200, "pairs": {}}, huge, (20000, 20000))

    def test_two_endpoint_migration_validation_and_proximal_taper(self):
        for length in (10, 50, 100):
            migrated = D.settings({"directional": {"on": True, "length": length}})
            self.assertEqual((migrated["start"], migrated["end"]), (0, length))
            self.assertNotIn("length", migrated)
        for start, end in ((50, 50), (100, 20), (-1, 100), (0, 101), (99.5, 100)):
            with self.subTest(start=start,end=end), self.assertRaises(ValueError):
                D.settings({"directional": {"start": start, "end": end}})
        for legacy in (150, 200):
            with self.assertRaisesRegex(ValueError, "beyond the knee"):
                D.settings({"directional": {"length": legacy}})
        target=pose((100,100))
        target["kp"][11],target["kp"][13]=(20,50),(80,50)
        p=D.settings({"directional":{"on":True,"right":False,"start":25,"end":75,"width":20,"taper":50,"feather":0,"distance":0}})
        _,a,b=D.segments(p,target,(100,100))[0]
        np.testing.assert_array_equal(a,[35,50])
        np.testing.assert_array_equal(b,[65,50])
        image=np.full((100,100,3),.4,np.float32)
        _,mask=D.apply(image,np.ones((100,100),np.float32),p,target,181)
        self.assertAlmostEqual(mask[:,35].sum(),20)
        self.assertAlmostEqual(mask[:,65].sum(),10)

    def test_taper_endpoint_widths_orientation_zero_equivalence_and_borders(self):
        a, b = np.array([30., 40.]), np.array([90., 40.])
        uniform = D.region((81, 121), a, b, 20)
        tapered = D.region((81, 121), a, b, 20, 50)
        np.testing.assert_array_equal(uniform, D.region((81, 121), a, b, 20, 0))
        self.assertAlmostEqual(tapered[:, 30].sum(), 20)
        self.assertAlmostEqual(tapered[:, 60].sum(), 15)
        self.assertAlmostEqual(tapered[:, 90].sum(), 10)
        reverse = D.region((81, 121), b, a, 20, 50)
        self.assertAlmostEqual(reverse[:, 30].sum(), 10)
        self.assertAlmostEqual(reverse[:, 90].sum(), 20)
        vertical = D.region((121, 81), a[::-1], b[::-1], 20, 50)
        np.testing.assert_array_equal(vertical, tapered.T)
        clipped = D.region((41, 61), np.array([0., 0.]), np.array([60., 40.]), 20, 90)
        self.assertTrue(np.isfinite(clipped).all())
        self.assertTrue(np.all((clipped >= 0) & (clipped <= 1)))
        self.assertEqual(clipped[0, 0], 1)
        self.assertEqual(clipped[-1, -1], 1)

    def test_taper_full_mask_overlap_zero_legacy_and_disabled_bypass(self):
        image = np.random.default_rng(14).uniform(.2, .8, (100, 100, 3)).astype(np.float32)
        skin = np.ones(image.shape[:2], np.float32)
        p = D.settings({"directional": {"on": True, "width": 60, "distance": 12, "feather": 2}})
        legacy = {key: value for key, value in p.items() if key != "taper"}
        full, mask = D.apply(image, skin, p, pose(image.shape), 181)
        old, old_mask = D.apply(image, skin, legacy, pose(image.shape), 181)
        np.testing.assert_array_equal(full, old)
        np.testing.assert_array_equal(mask, old_mask)
        tapered_p=D.settings({"directional": {**p, "taper":50, "pairs":{}}})
        tapered, mt = D.apply(image, skin, tapered_p, pose(image.shape), 181)
        left, ml = D.apply(image, skin, {**tapered_p, "right": False}, pose(image.shape), 181)
        right, mr = D.apply(image, skin, {**tapered_p, "left": False}, pose(image.shape), 181)
        overlap = (ml > .995) & (mr > .995)
        self.assertTrue(overlap.any())
        np.testing.assert_allclose(tapered[overlap], (left[overlap] + right[overlap]) / 2, atol=5e-4)
        self.assertLess(mt.sum(), mask.sum())
        self.assertTrue(np.all(mt <= mask + 1e-6))
        np.testing.assert_array_equal(tapered[mt == 0], image[mt == 0])
        with patch.object(D, "region", side_effect=AssertionError("no disabled taper geometry")):
            self.assertIsNone(D.apply(image, skin, {**p, "on": False, "taper": 90}, None, 181))

    def test_scale_segment_transform_capsule_and_skin_intersection(self):
        p = D.settings({"directional": {"on": True, "width": 12, "feather": 2, "distance": 8}})
        lines = D.segments(p, pose((100, 100)), (100, 100), origin=(10, 20), scale=.5)
        np.testing.assert_array_equal(lines[0][1], [5, 2.5])
        self.assertEqual(D.scaled(p, 362, .5), D.scaled(p, 181))
        capsule = D.region((30, 30), np.array([5., 15.]), np.array([25., 15.]), 8)
        self.assertEqual(capsule[15, 15], 1)
        self.assertEqual(capsule[0, 15], 0)
        np.testing.assert_array_equal(capsule[11], capsule[19])
        image = np.full((100, 100, 3), .4, np.float32)
        skin = np.ones((100, 100), np.float32)
        skin[:, :50] = 0
        raster, mask = D.apply(image, skin, p, pose(image.shape), 181)
        self.assertTrue(np.all(mask[:, :50] == 0))
        self.assertGreater(mask.max(), .9)
        self.assertTrue(np.any((mask > 0) & (mask < 1)))
        np.testing.assert_allclose(raster, image, atol=2e-7)

    def test_motion_axes_distance_zero_constant_and_overlap_average(self):
        impulse = np.zeros((61, 61, 3), np.float32)
        impulse[30, 30] = 1
        horizontal = D.motion(impulse, 0, 20)
        vertical = D.motion(impulse, 90, 20)
        self.assertGreater(np.count_nonzero(horizontal[30, :, 0]), 15)
        self.assertGreater(np.count_nonzero(vertical[:, 30, 0]), 15)
        self.assertLess(np.count_nonzero(horizontal[:, 30, 0]), 3)
        diagonal = D.motion(impulse, 45, 20)
        y, x = np.mgrid[:61, :61]
        self.assertGreater(np.sum(diagonal[..., 0] * (x - 30) * (y - 30)), 10)
        np.testing.assert_array_equal(D.motion(impulse, 30, 0), impulse)
        image = np.random.default_rng(55).uniform(.1, .9, (100, 100, 3)).astype(np.float32)
        skin = np.ones((100, 100), np.float32)
        p = D.settings({"directional": {"on": True, "width": 25, "end": 95, "distance": 12, "feather": 2}})
        original = image.copy()
        both, mask = D.apply(image, skin, p, pose(image.shape), 181)
        left, ml = D.apply(image, skin, {**p, "right": False}, pose(image.shape), 181)
        right, mr = D.apply(image, skin, {**p, "left": False}, pose(image.shape), 181)
        overlap = (ml > .99) & (mr > .99)
        self.assertTrue(overlap.any())
        np.testing.assert_allclose(both[overlap], (left[overlap] + right[overlap]) / 2, atol=5e-4)
        np.testing.assert_array_equal(both[mask == 0], image[mask == 0])
        np.testing.assert_array_equal(image, original)
        self.assertTrue(np.all((both >= .1 - 1e-6) & (both <= .9 + 1e-6)))

    def test_crop_grid_alignment_and_disabled_direct_helper(self):
        image = np.random.default_rng(42).uniform(.2, .8, (160, 160, 3)).astype(np.float32)
        skin = np.ones((160, 160), np.float32)
        p = D.settings({"directional": {"on": True, "width": 12, "length": 80, "distance": 8, "feather": 1}})
        target = pose(image.shape)
        full, mask = D.apply(image, skin, p, target, 181)
        cropped, cropped_mask = D.apply(image[10:150, 10:150], skin[10:150, 10:150], p, target, 181,
                                        origin=(10, 10), source_shape=image.shape)
        np.testing.assert_allclose(cropped[20:120, 20:120], full[30:130, 30:130], atol=3e-4)
        np.testing.assert_allclose(cropped_mask[20:120, 20:120], mask[30:130, 30:130], atol=1e-6)
        with patch.object(D, "segments", side_effect=AssertionError("no geometry")):
            self.assertIsNone(D.apply(image, skin, D.settings({}), None, 181))


class IntegrationTests(unittest.TestCase):
    setUp = FP.PipelineTests.setUp
    bridge = FP.PipelineTests.bridge
    params = FP.PipelineTests.params

    def test_availability_is_independent_and_cache_includes_new_selections(self):
        U.STATE.update(pose=pose(self.source.shape), masks={"present": np.ones(self.mask.shape)},
                       centre=(10, 10), region_box=(0, 0, 64, 48))
        self.assertEqual(set(U.directional_availability()), {*D.SEGMENTS, "torso"})
        self.assertTrue(all(v == "Available" for v in U.directional_availability().values()))
        U.STATE["pose"]["sc"][9] = .1
        availability = U.directional_availability()
        self.assertEqual(availability["left_forearm"], "Available")
        U.STATE["pose"]["kp"][9,0]=np.nan
        availability=U.directional_availability()
        self.assertIn("left forearm", availability["left_forearm"])
        self.assertEqual(availability["left_upper_arm"], "Available")
        self.assertEqual(availability["left"], "Available")
        p = self.params()
        p["directional"]["on"] = True
        before = U.preview_key(p, "whole")
        for pair in D.PAIRS:
            changed={**p,"directional":{**p["directional"],"pairs":{
                **p["directional"]["pairs"],pair:{"distance":13,"width":29,"taper":71}}}}
            self.assertNotEqual(U.preview_key(changed,"whole"),before)
            self.assertEqual(U.mask_settings_key(changed,None),U.mask_settings_key(p,None))
        torso={**p,"directional":{**p["directional"],"torso":True,
                                 "torso_controls":{"distance":19,"width":65,"taper":40}}}
        self.assertNotEqual(U.preview_key(torso,"whole"),before)
        for key,value in (("distance",21),("width",75),("taper",50)):
            changed={**torso,"directional":{**torso["directional"],"torso_controls":{
                **torso["directional"]["torso_controls"],key:value}}}
            self.assertNotEqual(U.preview_key(changed,"whole"),U.preview_key(torso,"whole"))
            self.assertEqual(U.mask_settings_key(changed,None),U.mask_settings_key(p,None))
        for key in set(D.SEGMENTS) - {"left", "right"}:
            changed = {**p, "directional": {**p["directional"], key: True}}
            self.assertNotEqual(U.preview_key(changed, "whole"), before)
            self.assertEqual(U.mask_settings_key(changed, None), U.mask_settings_key(p, None))
        with patch.dict(U.STATE, masks=None):
            self.assertTrue(all(v=="Available" for k, v in U.directional_availability().items()
                                if k != "left_forearm"))

    def test_disabled_skips_all_geometry_filter_and_cache_keys(self):
        p = self.params()
        with patch.object(D, "apply", side_effect=AssertionError("disabled")), \
                patch.object(D, "segments", side_effect=AssertionError("disabled")):
            U.skintone_layers(self.source, self.mask, p)
            with patch.object(U, "bridge", return_value=self.bridge("off")), \
                    patch.object(U, "full_skin_mask", return_value=self.mask):
                U.send_skintone_parallel(p)
        with patch.dict(U.STATE, centre=(10, 10), region_box=(0, 0, 64, 48)):
            changed = {**p, "directional": {**p["directional"], "distance": 111}}
            self.assertEqual(U.preview_key(p, "whole"), U.preview_key(changed, "whole"))
            enabled = {**changed, "directional": {**changed["directional"], "on": True}}
            self.assertNotEqual(U.preview_key(p, "whole"), U.preview_key(enabled, "whole"))
            self.assertEqual(U.mask_settings_key(p, None), U.mask_settings_key(enabled, None))
            U.STATE["pose"] = pose(self.source.shape)
            before = U.preview_key(enabled, "whole")
            U.STATE["pose"]["kp"][13, 0] += 1
            self.assertNotEqual(before, U.preview_key(enabled, "whole"))
            tapered = {**enabled, "directional": {**enabled["directional"], "taper": 50}}
            self.assertNotEqual(U.preview_key(enabled, "whole"), U.preview_key(tapered, "whole"))
            self.assertEqual(U.mask_settings_key(enabled, None), U.mask_settings_key(tapered, None))
            longer = {**enabled, "directional": {**enabled["directional"], "start": 10, "end": 95}}
            self.assertNotEqual(U.preview_key(enabled, "whole"), U.preview_key(longer, "whole"))
            self.assertEqual(U.mask_settings_key(enabled, None), U.mask_settings_key(longer, None))
            self.assertEqual(U.preview_key(p, "whole"), U.preview_key(
                {**p, "directional": {**p["directional"], "length": 150}}, "whole"))

    def test_combinations_full_strength_native_payload_and_serial_control(self):
        U.STATE.update(pose=pose(self.source.shape), masks={"present": np.ones(self.mask.shape)})
        source_before, skin_before = self.source.copy(), self.mask.copy()
        component_before = U.STATE["masks"]["present"].copy()
        self.mask.flags.writeable = False
        for blur, hue in itertools.product((False, True), repeat=2):
            p = self.params()
            p["frequency"].update(on=blur, blur_strength=34)
            p["skintone"].update(on=hue, hue_amount=41)
            p["directional"].update(on=True, distance=12, width=15, start=10, end=90, strength=37, feather=2, taper=50,
                                    **{key: True for key in D.SEGMENTS})
            p["directional"]["pairs"]={pair:{"distance":3+i*3,"width":10+i*3,"taper":i*20,
                                            "strength":20+i*20,"feather":i*.5,"start":i*5,"end":100-i*5}
                                        for i,pair in enumerate(D.PAIRS)}
            p["directional"].update(torso=True,torso_controls={"distance":7,"width":85,"taper":25,"strength":65,"feather":2,"start":15,"end":85})
            b = self.bridge("enabled")
            with patch.object(U, "bridge", return_value=b), patch.object(U, "full_skin_mask", return_value=self.mask):
                U.send_skintone_parallel(p)
            props = [args for cmd, args in b.client.calls if cmd == "layer.setProps"]
            directional = next(a for a in props if a.get("name") == "Directional Blur")
            self.assertAlmostEqual(directional["opacity"], 1)
            self.assertEqual(any(a.get("name") == "Blur" for a in props), blur)
            self.assertEqual(any(a.get("name") == "Hue" for a in props), hue)
            self.assertEqual(set(b.client.control_threads), {threading.get_ident()})
            self.assertEqual(list(b.exchange.iterdir()), [])
            np.testing.assert_array_equal(self.source, source_before)
            np.testing.assert_array_equal(self.mask, skin_before)
            np.testing.assert_array_equal(U.STATE["masks"]["present"], component_before)
            layers = U.skintone_layers(self.source, self.mask, p)
            native = U.skintone_layers(self.source, self.mask, p, native=True)
            np.testing.assert_array_equal(native["directional"], U.FS.rgb16(layers["directional"]))
            self.assertFalse(np.shares_memory(layers["directional_mask"], self.mask))
            self.assertFalse(np.shares_memory(layers["directional"], self.source))
            different = U.skintone_layers(self.source, self.mask,
                                         {**p, "directional": {**p["directional"], "strength": 100}})
            np.testing.assert_array_equal(layers["directional"], different["directional"])
            self.assertEqual(layers["directional_strength"], 100)
            layers["directional_mask"][:] = 0
            layers["directional"][:] = 0
            np.testing.assert_array_equal(self.source, source_before)
            np.testing.assert_array_equal(self.mask, skin_before)
            with patch.dict(U.STATE, masks=None), self.assertRaisesRegex(ValueError, "no shared skin mask"):
                U.skintone_layers(self.source, self.mask, p)
        with patch.dict(U.STATE, pose=None), patch.object(D, "motion", side_effect=AssertionError("missing pose filtered")):
            self.assertNotIn("directional",U.skintone_layers(self.source, self.mask, p))


class LocalTests(unittest.TestCase):
    setUp = T.SkintoneApiTests.setUp
    close_server = T.SkintoneApiTests.close_server
    get = T.SkintoneApiTests.get
    post = T.SkintoneApiTests.post

    def test_upper_arm_overlay_keeps_perpendicular_width_in_all_orientations(self):
        self.post("/api/load",{"path":str(self.image)})
        h,w=U.STATE["img"].shape[:2]
        U.STATE["masks"]={"A":np.ones((h,w),np.float32),
                         "features":np.zeros((h,w),np.float32),
                         "colour":np.ones((h,w),np.float32)}
        req={"mode":"frequency","S":181,"view":"whole","precache_mask":False,
             "frequency":{"on":False,"radius":2},"skintone":{"on":False},
             "mask":{"on":False},"tone":{"on":False},"color":{"on":False},
             "structure":{"protect":0},"directional":{"on":True,"left":False,"right":False,
             "left_upper_arm":True,"pairs":{"upper_arm":{"width":8,"taper":40,
                 "start":20,"end":80,"feather":0,"distance":0}}}}
        for a,b in (([w*.2,h*.5],[w*.8,h*.5]),([w*.5,h*.2],[w*.5,h*.8]),
                    ([w*.2,h*.2],[w*.8,h*.8])):
            target=pose((h,w))
            target["kp"][5],target["kp"][7]=a,b
            U.STATE["pose"]=target
            self.post("/api/run",req)
            p=D.settings(req)
            _,start,end=D.segments(p,target,(h,w))[0]
            mask=U.u8(D.region((h,w),start,end,8,40)).astype(np.float32)/255
            expected=U.u8(U.STATE["source_img"]*(1-.45*mask[...,None])+
                          np.array([.78,.46,.2],np.float32)*.45*mask[...,None])
            actual=cv2.imdecode(np.frombuffer(self.get("/img/directional_region")[0],np.uint8),1)
            np.testing.assert_array_equal(actual,expected)

    def test_foreground_preview_mask_stage_and_cache_bypass(self):
        self.post("/api/load", {"path": str(self.image)})
        for mode in ("mask","frequency","dnb"):
            req={**T.T.PARAMS,"mode":mode,"S":181,"view":"whole","precache_mask":False}
            with patch.object(U.progress,"call",wraps=U.progress.call) as stages:
                self.post("/api/run",req)
                self.assertIn("Calculating preview skin mask",[call.args[0] for call in stages.call_args_list])
            with patch.object(U.progress,"call",wraps=U.progress.call) as stages:
                result=self.post("/api/run",req)
                self.assertTrue(result["cached"])
                self.assertNotIn("Calculating preview skin mask",[call.args[0] for call in stages.call_args_list])

    def test_new_load_clears_directional_previews_and_disabled_filter_never_runs(self):
        U.STATE["imgs"] = {"orig": b"stale result", "mask": b"stale region",
                           "directional_region": b"stale overlay"}
        U.STATE["preview_cache"] = {"frequency": {"stale": True}}
        self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(U.STATE["imgs"], {})
        self.assertEqual(U.STATE["preview_cache"], {})
        source = U.STATE["source_img"].copy()
        req = {"mode": "frequency", "S": 181, "view": "whole", "precache_mask": False,
               "frequency": {"on": False, "radius": 2}, "skintone": {"on": False},
               "directional": {"on": False, "distance": 300, "taper": 90},
               "mask": {"on": False}, "tone": {"on": False}, "color": {"on": False},
               "structure": {"protect": 0}}
        with patch.object(U.DB, "apply", side_effect=AssertionError("disabled directional filter ran")):
            self.post("/api/run", req)
        self.assertNotIn("directional_region", U.STATE["imgs"])
        np.testing.assert_array_equal(U.STATE["source_img"], source)
        expected = U.encode(U.u8(U.source_region(U.STATE["last"]["reg"].shape, normalized=True)),
                            ".jpg", 92, U.STATE["source_icc"])
        self.assertEqual(U.STATE["imgs"]["orig"], expected)
        mask = cv2.imdecode(np.frombuffer(U.STATE["imgs"]["mask"], np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertTrue(np.all(mask == 255))

    def test_directional_never_changes_source_shared_masks_original_or_mask_across_modes(self):
        self.post("/api/load", {"path": str(self.image)})
        U.STATE["pose"] = pose(U.STATE["img"].shape)
        shape = U.STATE["img"].shape[:2]
        U.STATE["masks"] = {"A": np.full(shape, .9, np.float32),
                           "features": np.zeros(shape, np.float32),
                           "colour": np.full(shape, .8, np.float32)}
        req = {"mode": "frequency", "S": 181, "view": "whole", "precache_mask": False,
               "frequency": {"radius": 2, "blur_radius": 4}, "skintone": {"on": False},
               "mask": {"on": True, "feather": 0}, "tone": {"on": False}, "color": {"on": False},
               "structure": {"protect": 0}}
        originals = {key: U.STATE[key].copy() for key in ("img", "source_img")}
        components = {key: a.copy() for key, a in U.STATE["masks"].items()}
        p = U.parse_params(req)
        cached = U.cached_final_skin_mask(p)[0]
        before_mask = cached.copy()
        self.assertFalse(cached.flags.writeable)
        self.post("/api/run", req)
        baseline_orig, baseline_mask = U.STATE["imgs"]["orig"], U.STATE["imgs"]["mask"]
        for taper in (0, 50, 90):
            enabled = {**req, "directional": {"on": True, "width": 30, "distance": 20, "taper": taper}}
            self.post("/api/run", enabled)
            self.assertEqual(U.STATE["imgs"]["orig"], baseline_orig)
            self.assertEqual(U.STATE["imgs"]["mask"], baseline_mask)
            self.assertIn("directional_region", U.STATE["imgs"])
            self.post("/api/save", enabled)
            U.api_send_result(enabled)
            np.testing.assert_array_equal(U.cached_final_skin_mask(p)[0], before_mask)
            for mode in ("mask", "dnb", "frequency"):
                self.post("/api/run", {**T.T.PARAMS, **req, "mode": mode})
                self.assertNotIn("directional_region", U.STATE["imgs"])
                self.assertEqual(U.STATE["imgs"]["mask"], baseline_mask)
            for key, array in originals.items():
                np.testing.assert_array_equal(U.STATE[key], array)
            for key, array in components.items():
                np.testing.assert_array_equal(U.STATE["masks"][key], array)

    def test_preview_region_local_send_save_recipe_and_missing_pose(self):
        loaded = self.post("/api/load", {"path": str(self.image)})
        self.assertEqual(set(loaded["directional_pose"]), {*D.SEGMENTS, "torso"})
        self.assertIn("no pose", loaded["directional_pose"]["left"])
        U.STATE["pose"] = pose(U.STATE["img"].shape)
        U.STATE["masks"] = {"A": np.ones(U.STATE["img"].shape[:2], np.float32),
                           "features": np.ones(U.STATE["img"].shape[:2], np.float32),
                           "colour": np.ones(U.STATE["img"].shape[:2], np.float32)}
        req = {"mode": "frequency", "S": 181, "view": "whole", "frequency": {"on": False, "radius": 2},
               "skintone": {"on": False}, "directional": {"on": True, "width": 20, "start": 10, "end": 90, "distance": 10, "strength": 35, "taper": 50}}
        req["directional"].update({key: True for key in D.SEGMENTS})
        req["directional"]["pairs"]={"upper_arm":{"distance":2,"width":15,"taper":70,"strength":70,"feather":2,"start":20,"end":70},
                                     "lower_leg":{"distance":8,"width":25,"taper":20}}
        req["directional"].update(torso=True,torso_controls={"distance":5,"width":80,"taper":20})
        self.post("/api/run", req)
        self.assertIn("directional_region", U.STATE["imgs"])
        p = U.parse_params(req)
        mask = U.full_skin_mask(p)
        layers = U.skintone_layers(U.STATE["source_img"], mask, p)
        coverage = layers["directional_mask"][..., None]
        overlay = U.STATE["source_img"] * (1 - .45 * coverage) + np.array([.78, .46, .2], np.float32) * .45 * coverage
        actual_region = cv2.imdecode(np.frombuffer(self.get("/img/directional_region")[0], np.uint8), 1)
        np.testing.assert_array_equal(actual_region, U.u8(overlay))
        expected = ST.composite(layers, mask, p["skintone"], p["frequency"])[1]
        actual = cv2.imdecode(np.frombuffer(self.get("/img/result")[0], np.uint8), 1)
        np.testing.assert_array_equal(actual, U.u8(expected))
        result = U.api_send_result(dict(req, generation=loaded["generation"]))
        np.testing.assert_array_equal(cv2.imread(result["path"], cv2.IMREAD_UNCHANGED)[..., :3], U.FS.rgb16(expected))
        saved = self.post("/api/save", req)
        self.assertIn("directional.png", saved["files"])
        self.assertIn("directional_mask.png", saved["files"])
        np.testing.assert_array_equal(cv2.imread(str(Path(saved["dir"]) / "directional_mask.png"), -1),
                                      U.u8(layers["directional_mask"]))
        self.assertEqual(json.loads((Path(saved["dir"]) / "settings.json").read_text())["directional"]["end"], 90)
        self.assertEqual(json.loads((Path(saved["dir"]) / "settings.json").read_text())["directional"]["pairs"]["upper_arm"],
                         {"distance":2,"width":15,"taper":70,"strength":70,"feather":2,"start":20,"end":70})
        self.assertEqual(json.loads((Path(saved["dir"]) / "settings.json").read_text())["directional"]["torso_controls"],
                         {"distance":5,"width":80,"taper":20})
        recipe = json.loads((Path(saved["dir"]) / "settings.json").read_text())["layer_recipe"]
        self.assertIn("Directional Blur (Normal, opacity 100%", recipe)
        np.testing.assert_array_equal(cv2.imread(str(Path(saved["dir"]) / "result.png"), -1)[..., :3],
                                      U.FS.rgb16(expected))
        U.STATE["pose"] = None
        U.STATE["preview_cache"] = {}
        skipped=U.api_send_result(dict(req, generation=loaded["generation"]))
        self.assertTrue(Path(skipped["path"]).is_file())
        saved=self.post("/api/save",req)
        self.assertNotIn("directional.png",saved["files"])
        self.assertNotIn("Directional Blur (",json.loads((Path(saved["dir"])/"settings.json").read_text())["layer_recipe"])
        U.STATE["pose"] = pose(U.STATE["img"].shape)
        fresh = self.post("/api/load", {"path": str(self.image)})
        self.assertIsNone(U.STATE["pose"])
        self.assertNotEqual(fresh["generation"], loaded["generation"])

    def test_preset_missing_geometry_effective_selection_preserves_settings_and_other_effects(self):
        self.post("/api/load",{"path":str(self.image)})
        shape=U.STATE["img"].shape
        available=pose(shape)
        U.STATE["masks"]={"A":np.ones(shape[:2],np.float32),
                          "features":np.ones(shape[:2],np.float32),"colour":np.ones(shape[:2],np.float32)}
        req={**T.T.PARAMS,"mode":"frequency","view":"whole","S":181,"precache_mask":False,
             "skintone":{"on":False},"directional":{"on":True,"left":True,"right":True,
             "left_upper_arm":True,"torso":True,"width":15,"distance":5,
             "pairs":{"upper_arm":{"strength":20},"upper_leg":{"strength":70}},
             "torso_controls":{"strength":40}}}
        self.post("/api/presets/save",{"name":"Missing limbs","settings":req})
        p=U.parse_params(req)
        requested=json.loads(json.dumps(p["directional"]))
        partial={"kp":available["kp"].copy()}
        partial["kp"][7]=np.nan
        partial["kp"][12]=np.nan
        U.STATE["pose"]=partial
        effective,missing=D.effective_settings(p["directional"],partial,shape)
        self.assertTrue(effective["left"])
        self.assertFalse(effective["right"])
        self.assertFalse(effective["torso"])
        self.assertFalse(effective["left_upper_arm"])
        self.assertEqual(set(missing),{"right","left_upper_arm","torso"})
        self.assertEqual(D.output_strength(effective),70)
        mask=U.full_skin_mask(p)
        layers=U.skintone_layers(U.STATE["source_img"],mask,p)
        reference=U.skintone_layers(U.STATE["source_img"],mask,{**p,"directional":effective})
        self.assertEqual(layers["directional_strength"],70)
        for key in layers:
            np.testing.assert_array_equal(layers[key],reference[key])
        self.assertEqual(p["directional"],requested)
        for current in (partial,None,available):
            U.STATE["pose"]=current
            for mode in ("frequency","combine","dnb","frequency"):
                self.post("/api/run",{**req,"mode":mode})
                if mode=="frequency":
                    self.assertEqual("directional_region" in U.STATE["imgs"],current is not None)
            self.assertTrue(self.post("/api/run",req)["cached"])
            self.post("/api/save",req)
            U.api_send_result(req)
        U.STATE["pose"]=None
        with patch.object(D,"motion",side_effect=AssertionError("missing geometry filtered")):
            disabled=U.skintone_layers(U.STATE["source_img"],mask,p,native=True)
        self.assertNotIn("directional",disabled)
        self.assertEqual(p["directional"],requested)
        self.assertEqual(json.loads(self.get("/api/presets")[0])["presets"]["Missing limbs"],req)
        self.post("/api/load",{"path":str(self.image)})
        self.post("/api/run",req)
        self.assertNotIn("directional_region",U.STATE["imgs"])
        U.STATE["pose"]=available
        U.STATE["masks"]={"A":np.ones(shape[:2],np.float32),
                          "features":np.ones(shape[:2],np.float32),"colour":np.ones(shape[:2],np.float32)}
        self.post("/api/run",req)
        self.assertIn("directional_region",U.STATE["imgs"])
        with self.assertRaises(ValueError):
            U.parse_params({**req,"directional":{"on":True,"width":0}})
        with self.assertRaisesRegex(ValueError,"finite shared skin mask"):
            D.apply(U.STATE["source_img"],np.full(shape[:2],np.nan),p["directional"],None,181)


@unittest.skipUnless(shutil.which("photocraft-cli"), "photocraft-cli unavailable")
class NativeTests(unittest.TestCase):
    setUp = DT.TransferTests.setUp
    close_process = DT.TransferTests.close_process
    new_target = DT.TransferTests.new_target
    save_pixels = DT.TransferTests.save_pixels

    def test_order_masks_opacity_pixels_and_profile_parity_all_effect_combinations(self):
        raw = np.random.default_rng(85).uniform(.25, .75, (8, 8, 3)).astype(np.float32)
        mask = np.tile(np.array([0, 32, 64, 128, 192, 224, 255, 255], np.uint8), (8, 1))
        for profile in ("srgb", "display-p3", "prophoto-compat"):
            for blur, hue in itertools.product((False, True), repeat=2):
                target = self.new_target(profile)
                path, _ = self.bridge.fetch(self.root / "input")
                with Image.open(path) as image:
                    icc = image.info["icc_profile"]
                layers = {key: U.FS.rgb16(a) for key, a in U.FS.split(raw, "gaussian", 1).items()}
                if blur:
                    layers["blur"] = U.FS.rgb16(cv2.GaussianBlur(raw, (0, 0), 2))
                f = {"blur_strength": 34, "blur_mask": True}
                base = ST.base({key: a.astype(np.float32) / 65535 for key, a in layers.items()}, mask / 255, f)
                p = D.settings({"directional": {"on": True, "distance": 4, "width": 3, "start": 10, "end": 90, "feather": .5, "taper": 50,
                                               "torso":True,"torso_controls":{"distance":2,"width":100,"taper":15,"strength":55,"feather":1,"start":0,"end":90},
                                               "pairs":{"upper_arm":{"distance":2,"width":2,"taper":20,"strength":25,"feather":0,"start":10,"end":80},
                                                        "forearm":{"distance":3,"width":4,"taper":70,"strength":75,"feather":1,"start":20,"end":90}},
                                               **{key: True for key in D.SEGMENTS}}})
                target_pose=pose(raw.shape)
                target_pose["kp"][[5,6,11,12]]=[[1,1],[6,1],[1,3],[6,3]]
                pixels, region = D.apply(base, mask / 255, p, target_pose, 181)
                raster, region8 = U.FS.rgb16(pixels), U.u8(region)
                settings = ST.settings({"skintone": {"on": hue, "hue_width": 1, "hue_amount": 41}})
                for strength in (0, 37, 100):
                    self.bridge.send_skintone(layers, settings, mask, target, icc, include_blur=blur,
                                             blur_strength=34, directional=(raster, region8, strength),
                                             transfer={"format": "tiff", "opaque_alpha": False})
                    inspection = self.client.run("document.inspect")
                    children = inspection["layers"][0]["children"]
                    names = ["Detail"] + (["Hue"] if hue else []) + ["Directional Blur"] + (["Blur"] if blur else []) + ["Color"]
                    self.assertEqual([a["name"] for a in children], names)
                    direction = next(a for a in children if a["name"] == "Directional Blur")
                    self.assertAlmostEqual(direction["opacity"], strength / 100)
                    self.assertTrue(direction["hasMask"])
                    normalized = {key: a.astype(np.float32) / 65535 for key, a in layers.items()}
                    normalized.update(directional=raster.astype(np.float32) / 65535,
                                      directional_mask=region8 / 255, directional_strength=strength)
                    expected = ST.composite(normalized, mask / 255, settings, f)[1]
                    actual = self.save_pixels("direction.png")
                    np.testing.assert_allclose(actual[..., :3], U.FS.rgb16(expected), atol=3)
                    if profile == "srgb" and not blur and not hue and strength == 100:
                        for child in children:
                            self.client.run("layer.setProps", layer=child["id"],
                                            visible=child["id"] == direction["id"])
                        background = inspection["layers"][1]["id"]
                        self.client.run("layer.setProps", layer=background, visible=False)
                        isolated = self.save_pixels("direction-only.png")
                        np.testing.assert_array_equal(isolated[..., 3], region8.astype(np.uint16) * 257)
                        np.testing.assert_array_equal(isolated[..., :3][region8 > 0], raster[region8 > 0])
                        self.client.run("layer.setProps", layer=background, visible=True)
                    # Remove this group's contribution before checking the next strength.
                    self.client.run("layer.delete", layer=inspection["layers"][0]["id"])
                    self.assertEqual(list(self.bridge.exchange.iterdir()), [])
                self.client.run("file.close", document=target["index"])
