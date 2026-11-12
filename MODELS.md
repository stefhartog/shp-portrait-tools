# External models and processing dependencies

**No model weights are bundled.** `uv sync` installs Python dependencies, not the manual
D&B models or Crop/ComfyUI models. The desktop installer only validates/references existing
files; it does not fetch them. rtmlib is the exception: it downloads pose weights lazily on
first D&B analysis. Starting an HTTP UI or running synthetic tests does not download weights.

**Skin Tools** tabs Skin Mask, FS & Tone, Dodge & Burn and Combine share this same
source analysis and model set. The separate **AI Fill** module contains Crop & Expand
and Patch Fill and uses ComfyUI; it does not use this D&B model set.
Directional Blur reuses the selected subject's existing pose coordinates; its per-region
controls and missing-geometry bypass introduce no additional inference or model weights.
Combine reuses existing FS/D&B engines. No Blemish/ClearFrame research model is a release
dependency. Use the root [README](README.md) and [handoff](HANDOFF.md) for current behavior,
not the frozen `docs/source/` installation notes.

## D&B: three manually supplied ONNX files

Create `~/.local/share/shp-photocraft-tools/dnb/models/` and download the following files there.
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
cd "$HOME/.local/share/shp-photocraft-tools/dnb/models"
sha256sum -c <<'EOF'
8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  yunet.onnx
f38c3321ceffbc9e95103480ad38cc3f52e7e1bde2bcee7cd9355d0b9138ac0c  face_landmarks.onnx
13961350bb2099688eb96182ffe26f24eb12c77b1df278a2ec95bbdc923136e1  selfie_multiclass_256x256.onnx
EOF
```

For standalone D&B, set `DNB_MODELS=/absolute/path/to/models` to use another directory.
Otherwise it uses `$DNB_DATA/models`, with D&B data defaulting to the path above.
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
`$TORCH_HOME/hub/checkpoints/`. It is **not** `DNB_MODELS`.
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
and belongs in a dedicated `opencv-zoo/` folder under `DNB_MODELS` (default
`~/.local/share/shp-photocraft-tools/dnb/models/opencv-zoo/`):

| File | Size | SHA-256 | Pinned upstream |
|---|---:|---|---|
| `person_detection_mediapipe_2023mar.onnx` | 11,990,159 bytes | `47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f` | [detector](https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx) |
| `pose_estimation_mediapipe_2023mar.onnx` | 5,557,238 bytes | `9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f` | [pose](https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/pose_estimation_mediapipe/pose_estimation_mediapipe_2023mar.onnx) |

Tools verifies both exact sizes and hashes before use. It never downloads or
changes model files. If `DNB_MODELS` is customized, use its `opencv-zoo/`
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

## Crop: ComfyUI models and nodes

Crop calls an external [ComfyUI](https://github.com/comfyanonymous/ComfyUI) runtime. Follow
ComfyUI's current GPU/PyTorch installation instructions in its **own** `venv/` or `.venv/`,
then install custom-node dependencies into that environment, not Tools' `.venv`.
For **FLUX.2 dev**, use a ComfyUI revision supporting `ReferenceLatent`,
`EmptyFlux2LatentImage` and `Flux2Scheduler`. **FLUX.1 Fill** instead uses core `UNETLoader`,
`DualCLIPLoader` (type `flux`, device `cpu`), `InpaintModelConditioning` and `KSampler`.
No FLUX.2-specific nodes or models are needed for Fill. The historical environment used ComfyUI 0.43
and a 24 GB GPU; that is context, not a tested minimum VRAM/version guarantee.

| Custom node | Required exported node | Installation source |
|---|---|---|
| ComfyUI-GGUF (FLUX.2 dev only) | `UnetLoaderGGUF` | [city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF), under `ComfyUI/custom_nodes/`; install its requirements using ComfyUI's Python. |
| comfyui-inpaint-nodes | `INPAINT_ColorMatch` | [Acly/comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes), under `custom_nodes/`; follow its dependency instructions. Other inpainting weights are not used by this graph. |
| seam-color-correct | `SeamColorCorrect` | Repo-managed copy: [`comfyui/custom_nodes/seam-color-correct/`](comfyui/custom_nodes/seam-color-correct/), included in source checkout/release. Install as below. |

### Installing the included seam node

From the Tools checkout (or extracted source release), copy the whole package to a fresh
ComfyUI installation. The following refuses to overwrite an existing node:

```sh
COMFY_DIR=/absolute/path/to/ComfyUI
test -d "$COMFY_DIR/custom_nodes" &&
  test ! -e "$COMFY_DIR/custom_nodes/seam-color-correct" &&
  cp -R comfyui/custom_nodes/seam-color-correct "$COMFY_DIR/custom_nodes/"
```

An existing destination must be reviewed manually; do not overwrite a working custom node
without comparing it. The package imports only `torch` and `torch.nn.functional`, supplied
by ComfyUI's own PyTorch environment; do not add torch to Tools' environment for this node.
It registers `SeamColorCorrect` with IMAGE/IMAGE/MASK inputs and an IMAGE output.
Restart **ComfyUI**, when no generation is active, after installing the node. Tools does not
perform that restart. Once ComfyUI is running, verify registration without generation:

```sh
curl -fsS http://127.0.0.1:8188/object_info/SeamColorCorrect
```

Use port 18788 instead for the Tools-managed instance. The JSON must contain a
`SeamColorCorrect` entry; an empty object or startup import error means it has not loaded.
Dev/Fill readiness requires this node; Klein's local seam correction does not.

### FLUX.2 Klein: third expansion method

| File | ComfyUI folder | Upstream |
|---|---|---|
| `flux-2-klein-9b.safetensors` | `models/diffusion_models/` | [BFL FLUX.2 Klein 9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B); choose the actual installed safetensors filename in Tools |
| `qwen_3_8b_fp8mixed.safetensors` | `models/text_encoders/` | [Comfy-Org Klein encoder](https://huggingface.co/Comfy-Org/flux2-klein-9B/tree/main/split_files/text_encoders) |
| `flux2-vae.safetensors` | `models/vae/` | Same FLUX.2 VAE as dev |

Klein uses core `UNETLoader`, `CLIPLoader` type `flux2`, `VAEEncode`,
`ReferenceLatent`, `SetLatentNoiseMask`, `CFGGuider`, `Flux2Scheduler`, Euler and
standard image/mask scaling nodes. No GGUF, Telea/blur custom nodes or custom
colour-correction node is required. Neural upscale needs its loader only if
selected; Lanczos is the default.

Its settings are independent `klein_*` fields: 4 steps, 2 MP, feather 32,
seam correction off and LoRA off. Optional `klein_lora_name` /
`klein_lora_strength` select local Klein-specific weights under `models/loras`
(strength default 1). Tools checks recognised safetensors projection widths
without allocating GPU tensors. Dev Turbo (width 6144) and FLUX.1 Turbo (3072)
do not fit Klein 9B (4096); errors are explicit rather than silently skipping.
Local inspection uses `SHPC_COMFY_DIR` (default `~/ComfyUI`); unknown formats or
external model locations fail conservatively. Compatible shapes do not prove
training family, effectiveness or a recommended step count. Follow the adapter's
publisher guidance and upstream licences; Tools downloads no models.

The copied `__init__.py` is byte-identical to the existing local node at the time of copying:
SHA-256 `f14e335bf4ffb87117f767392bb23adef07441c72678b426ca362fd362c161e8`.
Its docstring explicitly identifies a port of **OpenFill's `quality.seam_field`**; this is
not a claim of an independently original algorithm. The source carried no separate license
file, and this copy introduces no invented license grant. Review rights before redistribution.
Only source was copied, not Python caches, weights or the ComfyUI environment.

### FLUX.2 models (existing method)

Place the following files under the ComfyUI model directories, or equivalent locations
registered through ComfyUI's model-path configuration. Names/case must match the dropdown
selection or environment variable exactly:

| File used by default | ComfyUI location | Source |
|---|---|---|
| `flux2-dev-q3_k_m.gguf` | `models/unet/` (GGUF loader) | [city96/FLUX.2-dev-gguf](https://huggingface.co/city96/FLUX.2-dev-gguf): upstream filename is `flux2-dev-Q3_K_M.gguf`; rename locally to the lowercase default or select its actual name in Crop. |
| `mistral_3_small_flux2_fp8.safetensors` | `models/text_encoders/` | [Comfy-Org/Flux-2-Dev](https://huggingface.co/Comfy-Org/Flux-2-Dev/tree/main/split_files/text_encoders) |
| `flux2-vae.safetensors` | `models/vae/` | [Comfy-Org/Flux-2-Dev](https://huggingface.co/Comfy-Org/Flux-2-Dev/tree/main/split_files/vae) |
| `Flux_2-Turbo-LoRA_comfyui.safetensors` | `models/loras/` | [Comfy-Org/Flux-2-Dev](https://huggingface.co/Comfy-Org/Flux-2-Dev/tree/main/split_files/loras); optional only when **turbo lora** is off. |
| `4xNomos8k_atd_jpg.safetensors` | `models/upscale_models/` | [Phips/4xNomos8k_atd_jpg](https://huggingface.co/Phips/4xNomos8k_atd_jpg) |

These source listings/filenames were inspected; multi-GB weights were not downloaded for
this documentation update. No verified full Crop weight checksums are recorded here.
Pin the chosen upstream revision, compare the downloaded file's SHA-256 with that revision's
published LFS `oid sha256` (when available), and retain the digest with your deployment.
Review upstream model licenses/access conditions, especially [FLUX.2-dev](https://huggingface.co/black-forest-labs/FLUX.2-dev).

Crop's model/upscaler dropdowns choose alternative installed files. FLUX.2 also offers
**Lanczos (no model)**: selecting it skips neural upscaling and does not require the upscale
model or `UpscaleModelLoader` / `ImageUpscaleWithModel` nodes. Its existing neural upscaler
remains the default. `SHPC_CLIP`, `SHPC_VAE`
and `SHPC_LORA` override the encoder/VAE/LoRA filenames; pass them in the Tools launch
environment as well when using the supervisor. There is no model auto-download in Crop.
Its `/api/status` queries ComfyUI `/object_info` and reports missing nodes/models for the selected method before
Generate is enabled; it is a presence check, not an inference/VRAM compatibility test.

### FLUX.1 Fill models (additional method)

Select **FLUX.1 Fill** in Crop's method dropdown. This is native Fill, not FLUX.1 dev/Kontext,
FLUX.2, GGUF, or a Nunchaku checkpoint. It uses the full Fill weights cast by `UNETLoader`
to `fp8_e4m3fn` at load time; it does not require a separate pre-quantized FP8 diffusion file.

| File used by default | ComfyUI location | Source |
|---|---|---|
| `flux1-fill-dev.safetensors` | `models/diffusion_models/` | [black-forest-labs/FLUX.1-Fill-dev](https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev) |
| `clip_l.safetensors` | `models/text_encoders/` | [comfyanonymous/flux_text_encoders](https://huggingface.co/comfyanonymous/flux_text_encoders) |
| `t5xxl_fp8_e4m3fn_scaled.safetensors` | `models/text_encoders/` | Same text-encoder source; use the **scaled** FP8 file. |
| `ae.safetensors` | `models/vae/` | Same BFL Fill source; not `flux2-vae.safetensors`. |

These filenames were verified in the installed ComfyUI and upstream file listings; the full
weights were not downloaded or rehashed for this addition. Follow the revision/LFS verification
procedure above for a fresh download. Review the Fill model's license/access conditions,
including its non-commercial terms, before use.

The existing inpaint colour-match and included seam node are required for Fill. Crop converts
the source to sRGB before upload, so the standalone workflow's `LoadImageSRGB` custom loader
is not required. The Fill graph scales the feather mask to the 0.5 MP generation canvas,
uses `InpaintModelConditioning` with `noise_mask=false`, then Euler/simple sampling, CFG 1,
denoise 1 and native guidance 30 (28 steps). Colour match uses the resized mask to exclude
the new area from statistics. Lanczos resizing restores final resolution before seam correction
and feathered original paste-back. No LoRA, comparison branches or Nunchaku.
Fill defaults to **Lanczos (no model)**; optionally choose an installed neural model in its
upscaler dropdown. This inserts `UpscaleModelLoader` / `ImageUpscaleWithModel` after colour
matching, then retains exact-size Lanczos resizing before seam correction and paste-back.
Only that option requires the selected model under `models/upscale_models/` and both upscale
nodes. It uses the same models as FLUX.2 but persists independently as `fill_upscale`
(`lanczos` by default). Additional time/VRAM and detail changes are possible.

Settings `fill_guidance`, `fill_steps`, `fill_megapixels`, `fill_feathering` and `fill_unet`
are independent of existing FLUX.2 fields. Fill defaults are 30 / 28 / 0.5 / 64 /
`flux1-fill-dev.safetensors`. `SHPC_FILL_CLIP_L`, `SHPC_FILL_T5` and `SHPC_FILL_VAE`
override its encoder/VAE filenames, without affecting FLUX.2. The Fill **text encoding**
selector persists `fill_clip_device`: `cpu` (default) or `default` (ComfyUI GPU/auto
placement and offloading). The original proven workflow uses CPU; GPU fit/performance is
not guaranteed, even with smaller diffusion weights. This choice is included in presets.
With either option,
this is not CPU-only diffusion and does not guarantee a particular GPU memory budget.
The model dropdown reflects `UNETLoader`'s files: choose genuine full FLUX.1 Fill weights,
not another architecture or the experimental `svdq` Nunchaku weights.

Standalone Crop defaults to `http://127.0.0.1:8188` (`--comfy` / `SHPC_COMFY` override).
For supervised use, installer `--comfy-dir /absolute/path/to/ComfyUI` configures a dedicated
Tools-owned process on 18788 using that checkout's existing Python, nodes and models.
Tools does not change or install that checkout. See [README.md](README.md) for launcher,
data paths, PhotoCraft integration and browser-only alternatives.

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
