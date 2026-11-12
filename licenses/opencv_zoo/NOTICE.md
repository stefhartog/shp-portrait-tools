# OpenCV Zoo MediaPipe ONNX models

The optional MediaPipe person detector and pose model are OpenCV Zoo artifacts
from revision [`48b10fa505aeb51fbd127fc60a5c660040e823b7`](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models).
The detector and pose model directories each state that all files in that directory
are licensed under Apache-2.0; the license text is preserved in this directory.

The pinned [detector README](https://github.com/opencv/opencv_zoo/blob/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/person_detection_mediapipe/README.md)
attributes its model to the PINTO model zoo or a TFLite conversion using
`tflite2tensorflow` and `onnx-simplifier`, and its SSD anchors to
`GenMediaPipePalmDectionSSDAnchors`. The pinned [pose README](https://github.com/opencv/opencv_zoo/blob/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/pose_estimation_mediapipe/README.md)
attributes its TFLite-to-ONNX conversion to `tensorflow-onnx` and
`onnx-simplifier`, and references Google's MediaPipe Pose/BlazePose materials.
The exact original detector variant and conversion-tool versions were not
independently established.

`src/shp_photocraft_tools/modules/dnb/opencv_zoo_pose.py` adapts the pinned
OpenCV Zoo wrappers' preprocessing, detector decoding, pose crop/rotation and
coordinate restoration. It additionally matches ONNX outputs by tensor shape,
uses XYWH rectangles for OpenCV NMS, validates model hashes and invalidates
missing/low-confidence body joints. These changes do not alter the upstream
model binaries.

The model files are provisioned separately under the user's data directory and
are not included in this repository. Before redistributing model binaries,
review the upstream attribution and license obligations for each artifact.
