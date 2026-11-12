"""Experimental CPU adapter for the pinned OpenCV Zoo MediaPipe ONNX models.

Preprocessing and decoding are adapted from OpenCV Zoo revision
48b10fa505aeb51fbd127fc60a5c660040e823b7. See licenses/opencv_zoo/NOTICE.md.
"""
import hashlib
from pathlib import Path
import threading

import cv2
import numpy as np

DETECTOR_NAME = "person_detection_mediapipe_2023mar.onnx"
POSE_NAME = "pose_estimation_mediapipe_2023mar.onnx"
DETECTOR_SIZE = 11_990_159
DETECTOR_SHA256 = "47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f"
POSE_SIZE = 5_557_238
POSE_SHA256 = "9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f"
DETECTOR_INPUT = 224
POSE_INPUT = 256
DETECTOR_THRESHOLD = 0.5
POSE_THRESHOLD = 0.5
NMS_THRESHOLD = 0.3
COCO = (0, 2, 5, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)
_THREAD_MODELS = threading.local()


def _verified(path, size, digest):
    if not path.is_file():
        raise FileNotFoundError(f"OpenCV Zoo model not found: {path}")
    if path.stat().st_size != size:
        raise ValueError(f"OpenCV Zoo model size mismatch: {path.name}")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != digest:
        raise ValueError(f"OpenCV Zoo model SHA-256 mismatch: {path.name}")
    return path


def prerequisites(directory):
    folder = Path(directory).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError("OpenCV Zoo needs the dedicated absolute models folder")
    return (
        _verified(folder / DETECTOR_NAME, DETECTOR_SIZE, DETECTOR_SHA256),
        _verified(folder / POSE_NAME, POSE_SIZE, POSE_SHA256),
    )


def _anchors():
    anchors = []
    for stride, copies in ((8, 2), (16, 2), (32, 6)):
        cells = int(np.ceil(DETECTOR_INPUT / stride))
        for y in range(cells):
            for x in range(cells):
                center = ((x + 0.5) / cells, (y + 0.5) / cells)
                anchors.extend((center,) * copies)
    result = np.asarray(anchors, dtype=np.float32)
    if result.shape != (2254, 2):
        raise ValueError("OpenCV Zoo detector anchor configuration is invalid")
    return result


def _net(path):
    net = cv2.dnn.readNet(str(path))
    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    return net


def _models(paths):
    key = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
    cache = getattr(_THREAD_MODELS, "cache", None)
    if cache is None or cache[0] != key:
        cache = (key, _net(paths[0]), _net(paths[1]))
        _THREAD_MODELS.cache = cache
    return cache[1], cache[2]


def _detector_input(image):
    height, width = image.shape[:2]
    ratio = min(DETECTOR_INPUT / height, DETECTOR_INPUT / width)
    resized = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    resized = (resized - 0.5) * 2.0
    resized_h, resized_w = np.maximum(1, (np.asarray((height, width)) * ratio).astype(np.int32))
    resized = cv2.resize(resized, (int(resized_w), int(resized_h)))
    pad_h, pad_w = DETECTOR_INPUT - resized_h, DETECTOR_INPUT - resized_w
    left, top = pad_w // 2, pad_h // 2
    padded = cv2.copyMakeBorder(
        resized, top, pad_h - top, left, pad_w - left,
        cv2.BORDER_CONSTANT, None, (0, 0, 0))
    pad_bias = (np.asarray((left, top), dtype=np.float64) / ratio).astype(np.int32)
    return np.transpose(padded, (2, 0, 1))[None], pad_bias


def _detector_outputs(net):
    names = net.getUnconnectedOutLayersNames()
    outputs = net.forward(names)
    by_shape = {tuple(output.shape): output for output in outputs}
    boxes = by_shape.get((1, 2254, 12))
    scores = by_shape.get((1, 2254, 1))
    if boxes is None or scores is None or len(by_shape) != 2:
        raise ValueError("OpenCV Zoo detector returned unexpected ONNX tensor shapes")
    return boxes[0], scores[0, :, 0]


def _detections(box_output, score_output, image_size, pad_bias):
    anchors = _anchors()
    boxes_delta = np.asarray(box_output, dtype=np.float64)
    scores = np.asarray(score_output, dtype=np.float64).reshape(-1)
    if boxes_delta.shape != (2254, 12) or scores.shape != (2254,):
        raise ValueError("OpenCV Zoo detector output dimensions are invalid")
    if not np.isfinite(boxes_delta).all() or not np.isfinite(scores).all():
        raise ValueError("OpenCV Zoo detector returned non-finite output")
    logits = np.clip(scores, -100, 100)
    scores = 1.0 / (1.0 + np.exp(-logits))
    scale = float(max(image_size))
    center_delta = boxes_delta[:, :2] / DETECTOR_INPUT
    wh_delta = boxes_delta[:, 2:4] / DETECTOR_INPUT
    top_left = (center_delta - wh_delta / 2 + anchors) * scale
    bottom_right = (center_delta + wh_delta / 2 + anchors) * scale
    corners = np.concatenate((top_left, bottom_right), axis=1)
    corners -= [pad_bias[0], pad_bias[1], pad_bias[0], pad_bias[1]]
    widths = corners[:, 2] - corners[:, 0]
    heights = corners[:, 3] - corners[:, 1]
    valid = (scores >= DETECTOR_THRESHOLD) & (widths > 0) & (heights > 0)
    indices = np.flatnonzero(valid)
    if not len(indices):
        return np.empty((0, 13), dtype=np.float32)
    rects = np.column_stack((corners[indices, :2], widths[indices], heights[indices]))
    kept = cv2.dnn.NMSBoxes(rects.tolist(), scores[indices].tolist(),
                            DETECTOR_THRESHOLD, NMS_THRESHOLD, top_k=5000)
    kept = np.asarray(kept, dtype=np.int64).reshape(-1)
    if not len(kept):
        return np.empty((0, 13), dtype=np.float32)
    selected = indices[kept]
    landmarks = boxes_delta[selected, 4:].reshape(-1, 4, 2) / DETECTOR_INPUT
    landmarks += anchors[selected, None, :]
    landmarks = landmarks * scale - pad_bias
    return np.column_stack((corners[selected], landmarks.reshape(-1, 8), scores[selected])).astype(np.float32)


def _pose_input(image, person):
    landmarks = person[4:12].reshape(4, 2).astype(np.float64, copy=True)
    hip, full_body = landmarks[:2]
    full_dist = float(np.linalg.norm(hip - full_body))
    if (not np.isfinite(landmarks).all() or not np.isfinite(full_dist)
            or full_dist < 1 or full_dist > 2 * max(image.shape[:2])):
        return None
    full_box = np.array([hip - full_dist, hip + full_dist], dtype=np.int32)
    center = full_box.sum(axis=0) / 2
    half_size = (full_box[1] - full_box[0]) / 2
    full_box = np.array([center - half_size, center + half_size], dtype=np.int32)
    person_box = full_box.copy()
    person_box[:, 0] = np.clip(person_box[:, 0], 0, image.shape[1])
    person_box[:, 1] = np.clip(person_box[:, 1], 0, image.shape[0])
    if np.any(person_box[1] <= person_box[0]):
        return None
    crop = image[person_box[0, 1]:person_box[1, 1],
                 person_box[0, 0]:person_box[1, 0]]
    left, top = person_box[0] - full_box[0]
    right, bottom = full_box[1] - person_box[1]
    crop = cv2.copyMakeBorder(crop, int(top), int(bottom), int(left), int(right),
                              cv2.BORDER_CONSTANT, None, (0, 0, 0))
    pad_bias = person_box[0] - [left, top]
    hip -= pad_bias
    full_body -= pad_bias
    radians = np.pi / 2 - np.arctan2(-(full_body[1] - hip[1]), full_body[0] - hip[0])
    radians -= 2 * np.pi * np.floor((radians + np.pi) / (2 * np.pi))
    angle = float(np.rad2deg(radians))
    rotation = cv2.getRotationMatrix2D(hip, angle, 1.0)
    rotated = cv2.warpAffine(crop, rotation, (crop.shape[1], crop.shape[0]))
    blob = cv2.resize(rotated, (POSE_INPUT, POSE_INPUT), interpolation=cv2.INTER_AREA).astype(np.float32)
    blob = cv2.cvtColor(blob, cv2.COLOR_BGR2RGB) / 255.0
    rotated_box = np.array([[0, 0], [crop.shape[1], crop.shape[0]]], dtype=np.int32)
    return blob[None], rotated_box, angle, rotation, pad_bias


def _pose_outputs(net):
    outputs = net.forward(net.getUnconnectedOutLayersNames())
    by_shape = {tuple(output.shape): output for output in outputs}
    expected = ((1, 195), (1, 1), (1, 256, 256, 1), (1, 64, 64, 39), (1, 117))
    if len(by_shape) != len(expected) or any(shape not in by_shape for shape in expected):
        raise ValueError("OpenCV Zoo pose model returned unexpected ONNX tensor shapes")
    return [by_shape[shape] for shape in expected]


def _pose_landmarks(outputs, rotated_box, angle, rotation, pad_bias, image_size):
    landmarks_output, confidence_output, _, _, _ = outputs
    confidence = float(np.asarray(confidence_output).reshape(-1)[0])
    if not np.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("OpenCV Zoo pose model returned invalid confidence")
    if confidence < POSE_THRESHOLD:
        return None
    landmarks = np.asarray(landmarks_output, dtype=np.float64).reshape(39, 5).copy()
    if not np.isfinite(landmarks).all():
        raise ValueError("OpenCV Zoo pose model returned non-finite landmarks")
    logits = np.clip(landmarks[:, 3:5], -100, 100)
    landmarks[:, 3:5] = 1.0 / (1.0 + np.exp(-logits))
    scale = (rotated_box[1] - rotated_box[0]) / POSE_INPUT
    landmarks[:, :2] = (landmarks[:, :2] - POSE_INPUT / 2) * scale
    coordinate_rotation = cv2.getRotationMatrix2D((0, 0), angle, 1.0)
    rotated_landmarks = landmarks[:, :2] @ coordinate_rotation[:, :2]
    rotation_component = np.array([[rotation[0, 0], rotation[1, 0]],
                                   [rotation[0, 1], rotation[1, 1]]])
    translation = np.array([rotation[0, 2], rotation[1, 2]])
    inverse_translation = -rotation_component @ translation
    inverse_rotation = np.column_stack((rotation_component, inverse_translation))
    center = np.append(rotated_box.sum(axis=0) / 2, 1)
    original_center = np.array([center @ inverse_rotation[0], center @ inverse_rotation[1]])
    landmarks[:, :2] = rotated_landmarks + original_center + pad_bias
    width, height = image_size
    body = landmarks[list(COCO)]
    valid = ((body[:, 3:5] >= 0.5).all(axis=1)
             & (body[:, 0] >= 0) & (body[:, 1] >= 0)
             & (body[:, 0] < width) & (body[:, 1] < height))
    points = body[:, :2].astype(np.float32)
    scores = np.minimum(body[:, 3], body[:, 4]).astype(np.float32)
    points[~valid] = np.nan
    scores[~valid] = 0
    return points, scores


def detect(image, directory):
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3
            or min(image.shape[:2]) < 1 or max(image.shape[:2]) > 1600):
        raise ValueError("OpenCV Zoo pose expects a BGR8 image with maximum side 1600")
    paths = prerequisites(directory)
    detector, pose_model = _models(paths)
    input_blob, pad_bias = _detector_input(image)
    detector.setInput(input_blob)
    boxes, scores = _detector_outputs(detector)
    persons = _detections(boxes, scores, (image.shape[1], image.shape[0]), pad_bias)
    poses, confidences = [], []
    for person in persons:
        prepared = _pose_input(image, person)
        if prepared is None:
            continue
        blob, rotated_box, angle, rotation, pose_pad = prepared
        pose_model.setInput(blob)
        outputs = _pose_outputs(pose_model)
        decoded = _pose_landmarks(outputs, rotated_box, angle, rotation, pose_pad,
                                  (image.shape[1], image.shape[0]))
        if decoded is not None:
            points, confidence = decoded
            poses.append(points)
            confidences.append(confidence)
    if not poses:
        return np.empty((0, 17, 2), dtype=np.float32), np.empty((0, 17), dtype=np.float32)
    return np.asarray(poses, dtype=np.float32), np.asarray(confidences, dtype=np.float32)
