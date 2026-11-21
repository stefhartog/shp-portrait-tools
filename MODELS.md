# External models and processing dependencies

**No model weights are bundled.** `uv sync` installs Python dependencies, not the manual
D&B models. The desktop installer only validates/references existing
files; it does not fetch them. rtmlib is the exception: it downloads pose weights lazily on
first D&B analysis. Starting an HTTP UI or running synthetic tests does not download weights.

**Skin Tools** tabs Skin Mask, FS & Tone, Dodge & Burn and Combine share this same
source analysis and model set. Crop & Expand and Patch Fill are now the separate
[SHP AI Fill](https://github.com/stefhartog/shp-ai-fill) app.
Directional Blur reuses the selected subject's existing pose coordinates; its per-region
controls and missing-geometry bypass introduce no additional inference or model weights.
Combine reuses existing FS/D&B engines. No Blemish/ClearFrame research model is a release
dependency. Use the root [README](README.md) and [handoff](HANDOFF.md) for current behavior,
not the frozen `docs/source/` installation notes.

## D&B: three manually supplied ONNX files

Create `~/.local/share/shp-portrait-tools/skin/models/` and download the following files there.
Use actual binary downloads, not Git LFS pointer text or a hosting site's HTML page.
Links below pin the ONNX conversions inspected on 2026-11-05.

| Installed filename | Download / upstream |
|---|---|
| `yunet.onnx` | [OpenCV Zoo YuNet binary](https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/face_detection_yunet_2023mar.onnx); rename `face_detection_yunet_2023mar.onnx` to `yunet.onnx`. |
| `face_landmarks.onnx` | [naklitechie/face-landmarks-onnx binary](https://huggingface.co/naklitechie/face-landmarks-onnx/resolve/575c33816c840c5b156398adb485e4f8d138adc2/face_landmarks.onnx), Google's Face Landmarker converted to ONNX. |
| `selfie_multiclass_256x256.onnx` | [FreeHugsForRobots/ps-skin-seg binary](https://huggingface.co/FreeHugsForRobots/ps-skin-seg/resolve/f3a72025451688fb893083991fdfd4562814a4dd/selfie_multiclass_256x256.onnx). |

These are specific conversions, not interchangeable models with similar names. Tensor names,
layouts and class order are part of the inference contract. Sizes are respectively 232,589,
4,920,995 and 16,454,497 bytes (21,608,081 bytes total).

SHA-256 values below were verified against both preserved local binaries and upstream
Git LFS SHA-256 metadata. After downloading, verify before analysis:

```sh
cd "$HOME/.local/share/shp-portrait-tools/skin/models"
sha256sum -c <<'EOF'
8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  yunet.onnx
f38c3321ceffbc9e95103480ad38cc3f52e7e1bde2bcee7cd9355d0b9138ac0c  face_landmarks.onnx
13961350bb2099688eb96182ffe26f24eb12c77b1df278a2ec95bbdc923136e1  selfie_multiclass_256x256.onnx
EOF
```

For standalone D&B, set `SKIN_MODELS=/absolute/path/to/models` to use another directory.
Otherwise it uses `$SKIN_DATA/models`, with D&B data defaulting to the path above.
The supervised launcher instead uses the absolute `models` value in `launcher.json`;
installer `--models /absolute/path/to/models` writes that value and requires all three files.
No old project folder, symlink, environment or dataset is needed.

## D&B: automatically downloaded pose weights and offline use

`estimate_body_scale.py` lazily creates `rtmlib.Wholebody(mode="balanced",
backend="onnxruntime", device="cuda")`. The installed rtmlib selects:

| Purpose | Upstream archive |
|---|---|
| YOLOX-m HumanArt detector, 640x640 | [yolox_m_8xb8-300e_humanart-c2c7a14a.zip](https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/yolox_m_8xb8-300e_humanart-c2c7a14a.zip) |
| RTMW-x whole-body, 192x256 | [rtmw-dw-x-l_simcc-cocktail14_270e-256x192_20231122.zip](https://download.openmmlab.com/mmpose/v1/projects/rtmw/onnx_sdk/rtmw-dw-x-l_simcc-cocktail14_270e-256x192_20231122.zip) |

The installed downloader also supports a [Tau-J/RTMPose mirror](https://huggingface.co/Tau-J/RTMPose)
when the OpenMMLab host fails. These selections/cache details describe the current locked
dependency; review them after upgrading rtmlib.

Default cache is `~/.cache/rtmlib/hub/checkpoints/`. With `XDG_CACHE_HOME`, it is
`$XDG_CACHE_HOME/rtmlib/hub/checkpoints/`; `TORCH_HOME` takes precedence and changes it to
`$TORCH_HOME/hub/checkpoints/`. It is **not** `SKIN_MODELS`.
ZIPs are extracted and the contained `end2end.onnx` is renamed to the archive basename
with `.onnx`; successful extraction removes the ZIP and temporary extraction directory.
Cached ONNX files are reused without a network request.

For offline deployment, while online, explicitly prefetch without creating inference sessions:

```sh
uv run --no-sync python - <<'PY'
from rtmlib.tools.file import download_checkpoint
from rtmlib import Wholebody
for role in ("det", "pose"):
    print(download_checkpoint(Wholebody.MODE["balanced"][role]))
PY
```

This command intentionally downloads weights; UI startup does not. Copy those two resulting
ONNX files into the same cache layout for the account/environment that will run D&B offline,
preserving their filenames. An uncached offline first analysis fails to download; the
default rtmlib backend does not bundle an offline replacement or expose its model paths in the UI.
The downloader defaults to `check_hash=False`. No independently verified full upstream
checksums for these archives are recorded here. On a trusted download machine, record
`sha256sum` of the resulting ONNX files and compare after transfer (integrity of that copy,
not independent proof of upstream authenticity). Do not infer a full checksum from a filename.

D&B's GPU libraries come from `uv sync --locked --extra gpu`, plus a compatible NVIDIA driver.
Some ONNX helpers can fall back to CPU; the pose helper explicitly selects CUDA, so full
CPU-only analysis is not a supported fresh-install guarantee.

## Optional Google Pose Landmarker Full (experimental)

The default above remains rtmlib; Google is explicit opt-in, not an automatic substitute.
The separate worker requires Linux x86_64, CPython 3.13, bubblewrap with working
unprivileged user/network/PID namespaces, and system EGL/GLES libraries even for CPU
execution. These are optional prerequisites, not installed or enabled by Tools. The
tested host had Python 3.13.18 and glibc 2.44; no fresh-distro/Flatpak minimum is verified.
The MediaPipe wheel declares manylinux_2_28. Do not disable your system's namespace
policy to bypass an error; leave the default backend selected if isolation is unavailable.

Prepare a **new dedicated virtualenv**, never the Tools/ComfyUI environment. These
commands are explicit networked dependency installation, not an automatic app operation:

```sh
uv venv --python 3.13 --no-config "$HOME/.local/share/shp-google-pose-runtime"
uv pip sync --python "$HOME/.local/share/shp-google-pose-runtime/bin/python" \
  --only-binary :all: --require-hashes --no-config \
  scripts/google-pose-requirements.txt
```

The manifest pins the exact 19 Linux x86_64/CPython 3.13 wheels from the isolated
compatibility check, including `mediapipe==1.2.2`; hashes intentionally refuse other
platform artifacts. Dependency acquisition uses your network. MediaPipe is not added
to the parent app's dependencies or imported in that process.
Review and retain supplied runtime/dependency LICENSE and NOTICE files, including
MediaPipe's third-party and metrics notices, before any binary redistribution.

Manually obtain the unchanged official
[Full float16 v1 task](https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task)
and keep it in a deliberate local path. Expected size is **9,398,198 bytes** and SHA-256:

```text
5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1
```

This is Google's official `.task`, not an ONNX conversion. Tools checks size/hash
and mounts a reverified private snapshot read-only. No model is downloaded on selection
or inference. Google's linked BlazePose model card states Apache-2.0 for the weights;
the archive itself has no embedded licence. Runtime redistribution carries additional
third-party obligations and is not approved by this experimental integration.

In Settings & Info > Pose analysis backend, choose Google, enter the full
`.../bin/python` path and model path, and save. Settings persist under `pose` in global
`settings.json` (`backend`, `python`, `model`), outside presets. Only the next source
analysis/batch uses the choice; existing loaded work is unchanged.
CPU IMAGE mode, two-person cap, 33-to-17 mapping, missing whole-body cues and
confidence limitations are described in README. The three independent ONNX files
above remain required; this does not replace face/iris/skin inference.

MediaPipe's actual wheel NOTICE discloses metrics transmission and developer consent
responsibilities. No public telemetry-off setting is verified. All Google inference
runs in a networkless/filesystem-restricted worker, not the networked app. OS-enforced
egress blocking is not a claim telemetry generation is disabled. Missing isolation,
runtime/model or malformed outputs produce errors; no fallback is attempted.
Only procedural negative inference has run; portrait accuracy and performance remain
unvalidated. Switching back to rtmlib requires no worker/model and restores its existing
behavior on the next Get/load.

## Optional OpenCV Zoo MediaPipe ONNX pose backend (experimental)

The default remains rtmlib; this is a separate opt-in alternative alongside
Google Full, not a replacement or automatic fallback. It uses OpenCV DNN on
CPU in the existing Tools process. It needs no Google Tasks runtime, new Python
dependency, CUDA inference or external model service. The adapter itself makes
no network requests; that is not a general claim that the rest of Tools has no
network activity.

The pair is pinned to OpenCV Zoo revision
[`48b10fa505aeb51fbd127fc60a5c660040e823b7`](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models)
and belongs in a dedicated `opencv-zoo/` folder under `SKIN_MODELS` (default
`~/.local/share/shp-portrait-tools/skin/models/opencv-zoo/`):

| File | Size | SHA-256 | Pinned upstream |
|---|---:|---|---|
| `person_detection_mediapipe_2023mar.onnx` | 11,990,159 bytes | `47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f` | [detector](https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx) |
| `pose_estimation_mediapipe_2023mar.onnx` | 5,557,238 bytes | `9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f` | [pose](https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/pose_estimation_mediapipe/pose_estimation_mediapipe_2023mar.onnx) |

Tools verifies both exact sizes and hashes before use. It never downloads or
changes model files. If `SKIN_MODELS` is customized, use its `opencv-zoo/`
subdirectory or select another absolute model-directory path in Settings & Info.
An empty model-directory field defaults to that location. This pair does not
replace the separate face detector/landmark and skin-segmentation ONNX files.

The adapter follows the pinned wrappers' 224-square detector normalization,
anchor decoding, 256-square person crop/rotation and original-image coordinate
restoration. It matches output tensors by shape (the detector outputs were in
the opposite order from the wrapper's positional assumption in the tested
OpenCV build) and uses XYWH rectangles for OpenCV NMS. The 33 pose landmarks
map to COCO17. Only visible/present, in-frame joints are usable; confidence
below 0.5 is rejected. No 68-point face or 21-point hand observations are
invented, so independent face/iris cues remain available and Directional Blur
rejects missing/low-confidence geometry.

OpenCV 5.0.0's DNN CPU path loaded and ran both models on procedural blank
input, and synthetic tests cover preprocessing, output mapping, crop/coordinate
restoration, confidence rejection and multiple detections. No positive portrait
accuracy, speed, OpenCV 4.x, ONNX Runtime or CUDA result has been established.
This MediaPipe/BlazePose-family model is not known to improve on rtmlib or
Google Full. Both model directories state Apache-2.0; upstream conversion
attribution and the license are retained in
[`licenses/opencv_zoo/`](licenses/opencv_zoo/NOTICE.md). Exact original
detector variant and conversion-tool versions remain unresolved.

Select **OpenCV Zoo MediaPipe (experimental, CPU)** in Settings & Info >
Pose analysis backend. Save validates the model pair; the setting applies only
to the next Get/load or batch start, not a source already analyzed. Errors are
explicit and do not silently fall back. Switching back to rtmlib needs no model
path and restores the existing default behavior.

## AI Fill: ComfyUI models and nodes

Moved with AI Fill to [SHP AI Fill](https://github.com/stefhartog/shp-ai-fill/blob/main/MODELS.md),
including the `seam-color-correct` ComfyUI node.

## Model notices

YuNet: MIT, OpenCV Zoo / ShiqiYu libfacedetection, five facial landmarks.
Face Landmarker 478pts: ONNX conversion by naklitechie from Google's
`face_landmarker.task`, Apache-2.0 per its source notice.
MediaPipe Selfie Multiclass 256x256: conversion by FreeHugsForRobots of Google's TFLite
model, Apache-2.0 per the model card; class order is background/hair/body-skin/face-skin/clothes/others.
RTMW-x / YOLOX-m: OpenMMLab/rtmlib code and source notices identify Apache-2.0;
HumanArt, COCO and COCO-WholeBody/cocktail14 training data carry their own terms.
These notices do not replace reviewing the exact upstream artifact licenses before redistribution
or commercial deployment. No weights are bundled, and copying the local node does not
establish additional license rights.
