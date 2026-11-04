# SHP PhotoCraft D&B external models

No weights are bundled, copied automatically, or required just to start the HTTP UI.
For real image analysis, place these files in `$DNB_MODELS` (default
`~/.local/share/shp-photocraft-tools/dnb/models/`):

| Filename | Source |
|---|---|
| `yunet.onnx` | OpenCV Zoo `face_detection_yunet_2023mar.onnx` (rename locally) |
| `face_landmarks.onnx` | naklitechie/face-landmarks-onnx conversion of Google's Face Landmarker |
| `selfie_multiclass_256x256.onnx` | FreeHugsForRobots/ps-skin-seg conversion |

Obtain the same conversions as the originals; model tensor names/layouts are part of the
existing inference contract. RTMW/YOLOX weights are managed and downloaded by rtmlib when
pose analysis is first requested, not by application startup. Neither model downloads nor
real inference are part of the synthetic tests. Model paths can refer to an explicitly chosen
external directory; do not copy an entire old environment or datasets into this repository.

## Preserved source notices

YuNet (face_detection_yunet_2023mar.onnx): MIT, opencv_zoo / ShiqiYu libfacedetection. 5 landmarks. Detects faces ~10-300 px.
Face Landmarker 478pts (naklitechie/face-landmarks-onnx, converted from Google face_landmarker.task): Apache-2.0, sha256 f38c3321...ac0c
RTMPose-m body7 (17 COCO keypoints) + YOLOX-m HumanArt person detector, via rtmlib 0.0.16: Apache-2.0 code/weights (OpenMMLab). Note: training data (HumanArt, COCO etc.) carries its own terms; review before shipping commercially.
RTMW-x whole-body (133 keypoints: body, feet, face 68, hands) via rtmlib, replaces RTMPose-m for body/pose: Apache-2.0 code/weights (OpenMMLab); training data (COCO-WholeBody cocktail14) has its own terms.
MediaPipe Selfie Multiclass 256x256 (background/hair/body-skin/face-skin/clothes/others), ONNX conversion by FreeHugsForRobots (huggingface.co/FreeHugsForRobots/ps-skin-seg) of Google's selfie_multiclass_256x256.tflite: Apache-2.0 per Google's model card. 16.5 MB, sha256 13961350bb2099688eb96182ffe26f24eb12c77b1df278a2ec95bbdc923136e1. Used by the D&B skin mask and comparison experiment.
