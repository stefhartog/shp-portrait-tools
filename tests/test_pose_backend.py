"""Synthetic pose mapping, source routing and real network namespace boundaries."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from shp_photocraft_tools.modules.dnb import (
    pose_backend as P, estimate_body_scale as B, combine_scale as C,
    directional_blur as D, opencv_zoo_pose as Z, skin_mask_compare as M, ui_server as U,
)


def landmarks():
    return [[.1 + i * .01, .2 + i * .01, .9, .8] for i in range(33)]


class MappingTests(unittest.TestCase):
    def test_exact_anatomical_mapping_and_original_pixel_coordinates(self):
        source = landmarks()
        kp, scores = P.map_poses({"poses": [source]}, 1000, 500)
        self.assertEqual(kp.shape, (1, 17, 2))
        np.testing.assert_allclose(kp[0], np.array(source)[list(P.COCO), :2] * [1000, 500])
        np.testing.assert_allclose(scores, .8)
        self.assertEqual(B.extra_cues(kp[0], scores[0], use_hand=True), [])

    def test_cropped_missing_and_low_visibility_or_presence_are_not_geometry(self):
        source = landmarks()
        for i, bad in ((11, [-.1, .2, .9, .9]), (13, [.2, 1, .9, .9]),
                       (15, [.2, .3, .49, .9]), (23, [.2, .3, .9, .49])):
            source[i] = bad
        kp, scores = P.map_poses({"poses": [source]}, 100, 100)
        for i in (5, 7, 9, 11):
            self.assertTrue(np.isnan(kp[0, i]).all())
            self.assertEqual(scores[0, i], 0)
        pose = {"kp": kp[0], "sc": scores[0], "backend": P.GOOGLE}
        p = D.settings({"directional": {"on": True, "left": False, "right": False,
                                      "left_upper_arm": True}})
        effective, reasons = D.effective_settings(p, pose, (100, 100))
        self.assertFalse(effective["on"])
        self.assertIn("left_upper_arm", reasons)
        with self.assertRaises(D.GeometryUnavailable):
            D.torso_geometry(p, pose, (100, 100))

    def test_google_scores_required_without_altering_legacy_policy(self):
        pose = {"kp": np.arange(34).reshape(17, 2), "sc": np.zeros(17)}
        p = D.settings({"directional": {"on": True, "right": False}})
        self.assertEqual(len(D.segments(p, pose, (100, 100))), 1)
        for backend in P.SCORED_BACKENDS:
            pose["backend"] = backend
            with self.assertRaises(D.GeometryUnavailable):
                D.segments(p, pose, (100, 100))
        pose.pop("sc")
        with self.assertRaises(D.GeometryUnavailable):
            D.segments(p, pose, (100, 100))

    def test_empty_multiple_and_unusable_people(self):
        kp, scores = P.map_poses({"poses": []}, 100, 100)
        self.assertEqual(kp.shape, (0, 17, 2))
        self.assertEqual(scores.shape, (0, 17))
        first = landmarks()
        second = [[x + .4, y, v, p] for x, y, v, p in first]
        kp, _ = P.map_poses({"poses": [first, second]}, 100, 100)
        self.assertEqual(len(kp), 2)
        self.assertGreater(kp[1, 0, 0], kp[0, 0, 0])
        self.assertEqual(len(P.map_poses({"poses": [[[.2, .2, 0, 0]] * 33]}, 100, 100)[0]), 0)

    def test_invalid_worker_output_is_an_error_not_an_empty_detection(self):
        for value in (None, {}, {"poses": "bad"}, {"poses": [[]]}, {"poses": [landmarks()] * 3},
                      {"poses": [[[0, 0, float("nan"), 1]] * 33]},
                      {"poses": [[[0, 0, 1.1, 1]] * 33]}):
            with self.subTest(value=str(value)[:80]), self.assertRaises((ValueError, TypeError)):
                P.map_poses(value, 100, 100)

    def test_legacy_default_and_experimental_resize_routing(self):
        image = np.zeros((2000, 1000, 3), np.uint8)
        kp = np.ones((1, 17, 2), np.float32) * 100
        sc = np.ones((1, 17), np.float32)
        with patch.object(B, "body") as body:
            body.return_value.return_value = (kp, sc)
            actual, _ = B.detect(image)
            body.assert_called_once()
            np.testing.assert_allclose(actual, 125)
        for backend in P.SCORED_BACKENDS:
            with patch.object(P, "detect", return_value=(kp, sc)) as detector, patch.object(B, "body") as body:
                actual, _ = B.detect(image, {"backend": backend})
                body.assert_not_called()
                self.assertEqual(detector.call_args.args[0].shape, (1600, 800, 3))
                np.testing.assert_allclose(actual, 125)

    def test_independent_face_cues_and_one_to_one_experimental_body_association(self):
        image = np.zeros((100, 200, 3), np.uint8)
        kp = np.zeros((2, 17, 2), np.float32)
        kp[0] = [30, 30]
        kp[1] = [150, 30]
        sc = np.ones((2, 17), np.float32)
        faces = [np.array([20, 20, 20, 20]), np.array([140, 20, 20, 20])]
        face_info = (None, {"yaw_deg": 0}, .9)
        for backend in P.SCORED_BACKENDS:
            config = {"backend": backend}
            with patch.object(C.F, "detect", return_value=faces), \
                 patch.object(C, "face_cues", return_value=([("face_ipd", 12, .08)], face_info)), \
                 patch.object(B, "detect", return_value=(kp, sc)) as detect, \
                 patch.object(C.exif_af, "af_point", return_value=(150, 30)):
                subjects, bodies, _, target, _ = C.build_subjects("synthetic", image, config)
                self.assertEqual([s[3] for s in subjects], [0, 1])
                self.assertEqual(target, 1)
                self.assertIn(("face_ipd", 12, .08), subjects[0][5])
                detect.assert_called_once_with(image, config)
                chosen = M.pick_target("synthetic", image, pose_config=config)
                self.assertIn("AF target", chosen["desc"])
                np.testing.assert_array_equal(chosen["kp"], kp[1])
                box = M.person_box(chosen, image.shape)
                self.assertGreater(box[0], 100)
                self.assertIsNone(C.match_body(faces[0], bodies, excluded={0}))


class SettingsTests(unittest.TestCase):
    def test_persistence_validation_and_current_source_cache_not_mixed(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(U, "GLOBAL_SETTINGS", Path(directory) / "settings.json"), \
             patch.object(U, "PRESETS", Path(directory) / "presets.json"), \
             patch.dict(U.STATE, {"pose_backend": "rtmlib", "preview_cache": {"old": 1}}):
            initial = U.read_global_settings()
            self.assertEqual(initial["pose"], P.DEFAULTS)
            config = {"backend": P.GOOGLE, "python": "/runtime/bin/python", "model": "/full.task"}
            with patch.object(P, "prerequisites") as check:
                U.api_global_settings({"layer_transfer": initial["layer_transfer"], "pose": config})
                check.assert_called_once_with(config)
            saved = U.read_global_settings()
            self.assertEqual(saved["pose"], config)
            self.assertEqual(U.STATE["pose_backend"], "rtmlib")
            self.assertEqual(U.STATE["preview_cache"], {"old": 1})
            opencv = {"backend": P.OPENCV, "python": "", "model": "/models/opencv-zoo"}
            with patch.object(P, "prerequisites") as check:
                U.api_global_settings({"layer_transfer": initial["layer_transfer"], "pose": opencv})
                check.assert_called_once_with(opencv)
            self.assertEqual(U.read_global_settings()["pose"], opencv)
            with patch.object(P, "prerequisites", side_effect=ValueError("missing isolation")):
                with self.assertRaisesRegex(ValueError, "missing isolation"):
                    U.api_global_settings({"layer_transfer": initial["layer_transfer"], "pose": config})
            self.assertEqual(U.read_global_settings()["pose"], opencv)
            U.api_global_settings({"layer_transfer": initial["layer_transfer"], "pose": P.DEFAULTS})
            self.assertEqual(U.read_global_settings()["pose"]["backend"], "rtmlib")
            self.assertNotIn("pose", U.module_settings({"mode": "mask", "pose": config}))
        for invalid in ("google", {"backend": "typo"}, {"model": False}, {"extra": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                P.settings(invalid)
        self.assertEqual(P.settings({"backend": P.OPENCV})["model"], str(P.OPENCV_MODEL_DIR))
        self.assertEqual(P.settings({"backend": P.OPENCV})["python"], "")

    def test_opencv_model_verification_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            detector = root / Z.DETECTOR_NAME
            pose = root / Z.POSE_NAME
            detector.write_bytes(b"detector")
            pose.write_bytes(b"pose")
            with patch.object(Z, "DETECTOR_SIZE", len(b"detector")), \
                 patch.object(Z, "DETECTOR_SHA256", hashlib.sha256(b"detector").hexdigest()), \
                 patch.object(Z, "POSE_SIZE", len(b"pose")), \
                 patch.object(Z, "POSE_SHA256", hashlib.sha256(b"pose").hexdigest()):
                self.assertEqual(Z.prerequisites(root), (detector, pose))
                pose.write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "size mismatch|SHA-256 mismatch"):
                    Z.prerequisites(root)

    def test_missing_isolation_runtime_and_model_fail_before_spawn(self):
        with patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "bubblewrap"):
                P.prerequisites({"backend": P.GOOGLE})
        with self.assertRaisesRegex(ValueError, "executable"):
            P.prerequisites({"backend": P.GOOGLE, "python": "/missing/runtime"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            python = root / "bin/python"
            python.symlink_to(shutil.which("python3"))
            (root / "pyvenv.cfg").write_text(f"home = {python.resolve().parent}\n")
            with self.assertRaisesRegex(ValueError, "manually provisioned"):
                P.prerequisites({"backend": P.GOOGLE, "python": str(python), "model": "/missing/task"})
            model = root / "model.task"
            model.write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                P.prerequisites({"backend": P.GOOGLE, "python": str(python), "model": str(model)})

    def test_source_configuration_is_explicit_and_failures_leave_previous_state(self):
        image = np.ones((10, 10, 3), np.float32)
        config = {"backend": P.GOOGLE, "python": "/test", "model": "/test"}
        old_image = U.STATE.get("img")
        with patch.object(M, "pick_target", return_value=None) as pick:
            loaded, info = U.finish_loaded_image(
                Path(__file__), image, image, np.uint8(image), "srgb", None, None,
                True, None, "", 10, 10, time.perf_counter(), publish=False, pose_config=config)
            self.assertEqual(info["pose_backend"], P.GOOGLE)
            self.assertEqual(loaded["pose_backend"], P.GOOGLE)
            self.assertIsNone(loaded["pose"])
            self.assertIs(U.STATE.get("img"), old_image)
            self.assertEqual(pick.call_args.kwargs["pose_config"], config)
        with patch.object(M, "pick_target", side_effect=ValueError("worker failed")):
            with self.assertRaisesRegex(ValueError, "worker failed"):
                U.finish_loaded_image(
                    Path(__file__), image, image, np.uint8(image), "srgb", None, None,
                    True, None, "", 10, 10, time.perf_counter(), pose_config=config)
            self.assertIs(U.STATE.get("img"), old_image)

    def test_new_source_clears_old_backend_caches_and_scores_invalidate_google_previews(self):
        from test_dnb import PARAMS
        image = np.ones((10, 10, 3), np.float32)
        with patch.dict(U.STATE), patch.dict(U.BRIDGE), patch.object(M, "pick_target", return_value=None):
            U.STATE.update(landmark_generation=5, pose_backend=P.GOOGLE, preview_cache={"old": 1})
            info = U.finish_loaded_image(
                Path(__file__), image, image, np.uint8(image), "srgb", None, None,
                True, None, "", 10, 10, time.perf_counter(), pose_config=P.DEFAULTS)
            self.assertEqual(info["pose_backend"], "rtmlib")
            self.assertEqual(U.STATE["landmark_generation"], 6)
            self.assertEqual(U.STATE["preview_cache"], {})
            self.assertIsNone(U.STATE["full_mask_cache"])
            U.STATE["region_box"] = (0, 0, 10, 10)
            U.STATE["pose"] = {"kp": np.ones((17, 2)), "sc": np.ones(17), "backend": P.GOOGLE}
            params = U.parse_params({**PARAMS, "mode": "frequency", "directional": {"on": True}})
            before = U.preview_key(params, "whole")
            U.STATE["pose"]["sc"][13] = .1
            self.assertNotEqual(U.preview_key(params, "whole"), before)

    def test_failed_google_analysis_does_not_change_loaded_colour_contract(self):
        image = np.zeros((10, 10, 3), np.float32)
        config = {"backend": P.GOOGLE, "python": "/test", "model": "/test"}
        prepared = (Path(__file__), image, image, np.uint8(image), "adobe", None, None, True, None, "")
        with patch.dict(U.D.COLOUR, {"mode": "srgb"}), \
             patch.object(U, "read_global_settings", return_value={"pose": config}), \
             patch.object(U, "prepare_loaded_image", return_value=prepared) as prepare, \
             patch.object(M, "pick_target", side_effect=ValueError("worker failed")):
            with self.assertRaisesRegex(ValueError, "worker failed"):
                U.api_load({"path": "synthetic"})
            prepare.assert_called_once_with({"path": "synthetic"}, set_colour=False)
            self.assertEqual(U.D.COLOUR["mode"], "srgb")

    def test_failed_or_malformed_worker_never_falls_back_and_timeout_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            model.write_bytes(b"synthetic model")
            config = {"backend": P.GOOGLE}
            prerequisite = (Path("/test/python"), root, root, model)
            with patch.object(P, "prerequisites", return_value=prerequisite), \
                 patch.object(P, "MODEL_SHA256", hashlib.sha256(model.read_bytes()).hexdigest()), \
                 patch.object(B, "body") as legacy, \
                 patch.object(P.subprocess, "run") as run:
                run.return_value.returncode = 1
                with self.assertRaisesRegex(ValueError, "Isolated Google pose failed"):
                    B.detect(np.zeros((10, 10, 3), np.uint8), config)
                legacy.assert_not_called()
                self.assertTrue(run.call_args.kwargs["close_fds"])
                self.assertEqual(run.call_args.kwargs["env"], {"PATH": os.defpath})
                run.side_effect = subprocess.TimeoutExpired("worker", 90)
                with self.assertRaisesRegex(ValueError, "90 seconds"):
                    P.detect(np.zeros((10, 10, 3), np.uint8), config)
                legacy.assert_not_called()

    def test_opencv_adapter_preprocess_decode_and_multiple_people(self):
        anchors = Z._anchors()
        self.assertEqual(anchors.shape, (2254, 2))
        np.testing.assert_allclose(anchors[0], [1 / 56, 1 / 56])
        np.testing.assert_allclose(anchors[1568], [1 / 28, 1 / 28])
        np.testing.assert_allclose(anchors[1960], [1 / 14, 1 / 14])
        image = np.full((100, 200, 3), (0, 0, 255), np.uint8)
        blob, pad = Z._detector_input(image)
        self.assertEqual(blob.shape, (1, 3, 224, 224))
        self.assertEqual(blob.dtype, np.float32)
        self.assertEqual(tuple(pad), (0, 49))
        np.testing.assert_allclose(blob[0, :, 100, 34], [1, -1, -1], atol=1e-5)
        boxes = np.zeros((2254, 12), np.float32)
        scores = np.full(2254, -100, np.float32)
        for index, center_x in ((0, 60), (2, 180)):
            anchor = anchors[index]
            boxes[index, :2] = (np.array([center_x, 50]) / 200 - anchor) * Z.DETECTOR_INPUT
            boxes[index, 2:4] = [40, 100]
            points = np.array([[center_x, 150], [center_x, 20], [center_x, 70], [center_x, 50]])
            boxes[index, 4:] = (points / 200 - anchor).reshape(-1) * Z.DETECTOR_INPUT
            scores[index] = 10
        detections = Z._detections(boxes, scores, (200, 100), np.array([0, 50]))
        self.assertEqual(detections.shape, (2, 13))
        self.assertEqual(len(np.unique(detections[:, 4])), 2)

    def test_opencv_pose_decode_confidence_and_cropped_landmarks(self):
        image = np.zeros((256, 256, 3), np.uint8)
        person = np.array([0, 0, 256, 256, 128, 180, 128, 40, 128, 100, 128, 70, .9], np.float32)
        prepared = Z._pose_input(image, person)
        self.assertIsNotNone(prepared)
        blob, box, angle, rotation, pad = prepared
        self.assertEqual(blob.shape, (1, 256, 256, 3))
        landmarks = np.zeros((1, 195), np.float32)
        landmarks.reshape(39, 5)[:, :2] = 128
        landmarks.reshape(39, 5)[:, 3:5] = 10
        confidence = np.array([[.9]], np.float32)
        outputs = [landmarks, confidence, np.zeros((1, 256, 256, 1), np.float32),
                   np.zeros((1, 64, 64, 39), np.float32), np.zeros((1, 117), np.float32)]
        points, scores = Z._pose_landmarks(outputs, box, angle, rotation, pad, (256, 256))
        self.assertEqual(points.shape, (17, 2))
        self.assertTrue(np.all(scores > .99))
        self.assertTrue(np.all((points >= 0) & (points < 256)))
        np.testing.assert_allclose(points, np.broadcast_to([128, 180], points.shape), atol=1e-4)
        outputs[1][0, 0] = .49
        self.assertIsNone(Z._pose_landmarks(outputs, box, angle, rotation, pad, (256, 256)))
        outputs[1][0, 0] = .9
        landmarks.reshape(39, 5)[23, 3] = -10
        points, scores = Z._pose_landmarks(outputs, box, angle, rotation, pad, (256, 256))
        self.assertTrue(np.isnan(points[11]).all())
        self.assertEqual(scores[11], 0)
        landmarks.reshape(39, 5)[23, 3] = 10
        landmarks.reshape(39, 5)[23, 0] = 300
        points, scores = Z._pose_landmarks(outputs, box, angle, rotation, pad, (256, 256))
        self.assertTrue(np.isnan(points[11]).all())
        self.assertEqual(scores[11], 0)

    def test_opencv_full_route_keeps_multiple_people_and_maps_scores(self):
        anchors = Z._anchors()
        detector_boxes = np.zeros((1, 2254, 12), np.float32)
        detector_scores = np.full((1, 2254, 1), -100, np.float32)
        for index, center_x in ((0, 60), (2, 180)):
            anchor = anchors[index]
            detector_boxes[0, index, :2] = (np.array([center_x, 128]) / 256 - anchor) * Z.DETECTOR_INPUT
            detector_boxes[0, index, 2:4] = [30, 60]
            points = np.array([[center_x, 180], [center_x, 40], [center_x, 100], [center_x, 70]])
            detector_boxes[0, index, 4:] = (points / 256 - anchor).reshape(-1) * Z.DETECTOR_INPUT
            detector_scores[0, index, 0] = 10

        class FakeDetector:
            def setInput(self, _):
                pass

            def getUnconnectedOutLayersNames(self):
                return ("score", "boxes")

            def forward(self, _):
                return [detector_scores, detector_boxes]

        class FakePose:
            def setInput(self, _):
                pass

            def getUnconnectedOutLayersNames(self):
                return tuple(range(5))

            def forward(self, _):
                points = np.zeros((1, 195), np.float32)
                points.reshape(39, 5)[:, :2] = 128
                points.reshape(39, 5)[:, 3:5] = 10
                return [np.zeros((1, 256, 256, 1), np.float32),
                        np.zeros((1, 117), np.float32),
                        np.array([[.9]], np.float32), points,
                        np.zeros((1, 64, 64, 39), np.float32)]

        with patch.object(Z, "prerequisites", return_value=(Path("/detector"), Path("/pose"))), \
             patch.object(Z, "_models", return_value=(FakeDetector(), FakePose())):
            points, scores = Z.detect(np.zeros((256, 256, 3), np.uint8), "/models")
        self.assertEqual(points.shape, (2, 17, 2))
        self.assertEqual(scores.shape, (2, 17))
        self.assertGreater(abs(float(points[0, 0, 0] - points[1, 0, 0])), 50)
        self.assertTrue(np.all(scores > .99))

    @unittest.skipUnless(os.environ.get("SHP_TEST_OPENCV_MODEL_DIR"),
                         "explicit OpenCV Zoo model directory not supplied")
    def test_actual_opencv_models_on_synthetic_non_person_image(self):
        points, scores = Z.detect(
            np.full((256, 256, 3), 127, np.uint8), os.environ["SHP_TEST_OPENCV_MODEL_DIR"])
        self.assertEqual(points.shape, (0, 17, 2))
        self.assertEqual(scores.shape, (0, 17))


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap not installed")
class IsolationTests(unittest.TestCase):
    def test_namespace_denies_host_network_home_environment_and_socket_fds(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.set_inheritable(True)
            root = Path(directory)
            secret = root / "parent-secret"
            secret.write_text("synthetic")
            model = root / "model"
            model.touch()
            result = root / "result"
            result.touch()
            code = """
import json, os, socket
from pathlib import Path
with socket.socket() as s:
 s.settimeout(1);blocked=s.connect_ex(('127.0.0.1', PORT))!=0
with socket.socket() as s:
 s.settimeout(1);external=s.connect_ex(('192.0.2.1',443))==101
fds=[os.readlink(p) for p in Path('/proc/self/fd').glob('*') if p.exists()]
Path('/result.json').write_text(json.dumps({
 'blocked':blocked,'external':external,'secret':Path(SECRET).exists(),
 'env':os.environ.get('SHP_TEST_SECRET'),'sockets':[p for p in fds if p.startswith('socket:')],
 'interfaces':socket.if_nameindex()}))
""".replace("PORT", str(listener.getsockname()[1])).replace("SECRET)", repr(str(secret)) + ")")
            args = P.command(Path("/usr/bin/python3"), root / "empty-runtime", Path("/usr"), model, result)
            # Create an empty, dedicated mount; only the synthetic payload below runs.
            (root / "empty-runtime").mkdir()
            args[-1:] = ["-c", code]
            process = subprocess.run(args, input=b"", capture_output=True, timeout=20,
                                     close_fds=True, env={"PATH": os.defpath, "SHP_TEST_SECRET": "hidden"})
            if process.returncode and b"Operation not permitted" in process.stderr:
                self.skipTest("Host disables unprivileged namespaces")
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            report = json.loads(result.read_text())
            self.assertTrue(report["blocked"])
            self.assertTrue(report["external"])
            self.assertFalse(report["secret"])
            self.assertIsNone(report["env"])
            self.assertEqual(report["sockets"], [])
            self.assertEqual(report["interfaces"], [[1, "lo"]])
            with socket.create_connection(listener.getsockname(), timeout=1):
                pass

    @unittest.skipUnless(os.environ.get("SHP_TEST_GOOGLE_PYTHON") and os.environ.get("SHP_TEST_GOOGLE_MODEL"),
                         "explicit isolated test runtime and model not supplied")
    def test_actual_worker_on_synthetic_non_person_image(self):
        config = {"backend": P.GOOGLE, "python": os.environ["SHP_TEST_GOOGLE_PYTHON"],
                  "model": os.environ["SHP_TEST_GOOGLE_MODEL"]}
        kp, scores = P.detect(np.full((256, 256, 3), 127, np.uint8), config)
        self.assertEqual(kp.shape, (0, 17, 2))
        self.assertEqual(scores.shape, (0, 17))
