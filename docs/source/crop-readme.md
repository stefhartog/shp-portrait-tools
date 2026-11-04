# shpcrop

Grow a picture to a preset ratio (for example Instagram 4:5) and let **your own ComfyUI** fill the new area with FLUX.2
[dev] GGUF. Same UI as [shpexpand](https://github.com/stefhartog/shpexpand), but no OpenFill. Tested on a 24 GB GPU.

## Run

```
uv run shpcrop            # then open http://127.0.0.1:8768
uv run shpcrop --comfy http://host:8188 --dir ~/Pictures
```

ComfyUI is expected at `http://127.0.0.1:8188`. If it is not running, the page shows a **Start ComfyUI** button that launches
`~/ComfyUI` in the background (log: `comfy.log` in the data directory). That only works for a ComfyUI on this machine.

## Shared appearance

Common styling comes from the small `shp-ui` Python package in
[`shpdnb/shared/shp-ui`](https://github.com/stefhartog/shpdnb/tree/df3316e1918c7a43e467bf9c27431e346732e139/shared/shp-ui).
The dependency is pinned to that Git commit, not a moving branch. Crop serves its installed `common.css` resource at
`/static/shp-ui.css` on the same local server; there is no CDN, extra service, or copied stylesheet. Shared row labels
are 74px; read-only slider values use compact, right-aligned 42px columns. Crop-specific layout, ratio controls,
Generate/Cancel styling, and canvas behavior remain in `shpcrop/ui/index.html`.
The shared `.pane-header` class aligns pane tabs with D&B, including its 2px top inset; tab-button styling stays separate.
The shared package's CSS must be included in its wheel and sdist; crop's wheel continues to include its HTML.

Normal installation fetches the pinned shared-package commit from the existing `shpdnb` repository. When editing
the shared stylesheet locally, use an explicitly chosen package checkout:

```sh
export SHP_UI_SOURCE=/path/to/shpdnb/shared/shp-ui
uv sync
uv pip install --editable "$SHP_UI_SOURCE"
uv run --no-sync shpcrop
```

Use the same editable package checkout in both apps' environments to see shared CSS edits in both. `uv run --no-sync`
preserves this local override; a normal `uv sync`/`uv run` restores the pinned dependency instead. No sibling checkout
path is assumed by the application. To distribute a new shared stylesheet, push its commit first and update the
dependency pin and lockfile in each app. Normal users do not need a local override.

## Use

1. **Get from PhotoCraft** (or pick a file with `--dir`), choose a ratio (or **Free** for any shape), drag the frame or its corners.
2. **Generate** (1 to 4 variations; optional prompt). Pick a result tab, then **Send to PhotoCraft**.

Pipeline (same as `flux2_expand_simple.json`): pad, scale to 0.5 MP, FLUX.2 with the padded image as reference (Turbo LoRA,
8 steps), colour match, 4x upscale back, seam correction, feathered paste-back of the original (the original is untouched
beyond the feather band).

## Settings and presets

Advanced (guidance, steps, megapixels, feathering, diffusion model, upscaler, Turbo LoRA, seam correction) is saved as you
change it. The preset dropdown (top right) loads a saved set; Save overwrites it, Save New stores the current settings under
a name, Delete removes it, and a star marks unsaved changes. "Factory settings" are the defaults.

## Requirements in ComfyUI

Custom nodes: ComfyUI-GGUF, comfyui-inpaint-nodes, `SeamColorCorrect`. Models: `flux2-dev-q3_k_m.gguf` (diffusion),
`mistral_3_small_flux2_fp8.safetensors` (text encoder), `flux2-vae.safetensors`, `Flux_2-Turbo-LoRA_comfyui.safetensors`, and an
upscaler (default `4xNomos8k_atd_jpg.safetensors`). The page lists what is missing.

## Environment variables

`SHPC_COMFY` (ComfyUI URL), `SHPC_COMFY_DIR` (default `~/ComfyUI`), `SHPC_LIMIT` (max px added per side, default 1024),
`SHPC_CLIP`, `SHPC_VAE`, `SHPC_LORA` (file names), `SHPC_DATA` (default `~/.local/share/shpcrop`: settings, presets, results,
log), `SHPC_PC_*` (PhotoCraft bridge).

## Tests

`uv run python tests/test_expand.py` (stub ComfyUI, no GPU needed), or
`uv run --no-sync python tests/test_expand.py` with the editable shared-package override above.
Includes the local shared CSS endpoint, content type, resource bytes, and unknown asset paths.
See `HANDOFF.md` for the design notes.
