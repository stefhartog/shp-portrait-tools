# External models and processing dependencies

**No model weights are bundled.** `uv sync` installs Python dependencies, not the manual
D&B models or Crop/ComfyUI models. The desktop installer only validates/references existing
files; it does not fetch them. rtmlib is the exception: it downloads pose weights lazily on
first D&B analysis. Starting an HTTP UI or running synthetic tests does not download weights.

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
application does not bundle an offline replacement or expose a pose-model-path UI.
The downloader defaults to `check_hash=False`. No independently verified full upstream
checksums for these archives are recorded here. On a trusted download machine, record
`sha256sum` of the resulting ONNX files and compare after transfer (integrity of that copy,
not independent proof of upstream authenticity). Do not infer a full checksum from a filename.

D&B's GPU libraries come from `uv sync --locked --extra gpu`, plus a compatible NVIDIA driver.
Some ONNX helpers can fall back to CPU; the pose helper explicitly selects CUDA, so full
CPU-only analysis is not a supported fresh-install guarantee.

## Crop: ComfyUI models and nodes

Crop calls an external [ComfyUI](https://github.com/comfyanonymous/ComfyUI) runtime. Follow
ComfyUI's current GPU/PyTorch installation instructions in its **own** `venv/` or `.venv/`,
then install custom-node dependencies into that environment, not Tools' `.venv`.
For **FLUX.2**, use a ComfyUI revision supporting `ReferenceLatent`,
`EmptyFlux2LatentImage` and `Flux2Scheduler`. **FLUX.1 Fill** instead uses core `UNETLoader`,
`DualCLIPLoader` (type `flux`, device `cpu`), `InpaintModelConditioning` and `KSampler`.
No FLUX.2-specific nodes or models are needed for Fill. The historical environment used ComfyUI 0.43
and a 24 GB GPU; that is context, not a tested minimum VRAM/version guarantee.

| Custom node | Required exported node | Installation source |
|---|---|---|
| ComfyUI-GGUF (FLUX.2 only) | `UnetLoaderGGUF` | [city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF), under `ComfyUI/custom_nodes/`; install its requirements using ComfyUI's Python. |
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
Crop's readiness check requires this node even when seam correction is switched off.

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
