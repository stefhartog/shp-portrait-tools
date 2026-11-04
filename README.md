# SHP PhotoCraft Tools

Two independent local tools in one Python project:

| Tool | Command | Default address |
|---|---|---|
| **SHP PhotoCraft Crop** | `uv run shp-photocraft-crop` | http://127.0.0.1:8768 |
| **SHP PhotoCraft D&B** | `uv run --extra gpu shp-photocraft-dnb` | http://127.0.0.1:8765 |

Crop generates new image area with your own ComfyUI. D&B estimates portrait scale and builds
a 16-bit Soft Light layer with a combined skin/color/light/structure mask. Each keeps its own
UI, server, processing engine, settings and PhotoCraft application semantics. There is no
combined server or plugin framework.

**This is a side-by-side migration, not a cutover.** The original `/home/stef/shpcrop` and
`/home/stef/dnb` projects are untouched. Existing desktop launchers still run those projects.
Do not use the default ports while the original tools are running.

## Setup

Use Python 3.13 and [uv](https://docs.astral.sh/uv/). The root `.python-version` selects 3.13.
All dependencies, entrypoints and packaged resources are defined by the root `pyproject.toml`
and `uv.lock`; there are no sibling-checkout or Git CSS dependencies.

```sh
cd /home/stef/shp-photocraft-tools
uv sync --extra gpu
```

The `gpu` extra retains D&B's pip-distributed CUDA 13/cuDNN libraries. It requires a compatible
NVIDIA driver; missing CUDA libraries produce ONNX Runtime diagnostics. `uv sync` without
the extra is enough for Crop, HTTP startup and CPU-only synthetic checks, not a promise of
GPU inference. `research` adds plotting/notebook dependencies for the retained experiment
scripts; they are not needed by either UI.

Use uv with this root project for GPU dependency resolution: the `onnxruntime` override
prevents rtmlib's CPU distribution from overwriting `onnxruntime-gpu` (both install the same
Python package). A plain `pip install` of the wheel does not inherit uv's project override.

D&B model weights are deliberately **not copied or shipped**. Supply the three ONNX files
listed in [MODELS.md](MODELS.md), using `DNB_MODELS` or the new data directory's `models/`.
The UI and `--help` start without weights; image analysis reports an actionable missing-file
error. Model sessions are lazy. rtmlib downloads/caches its pose weights on first real use;
that did not happen during migration checks.

For Sony autofocus-point subject selection, install ExifTool with Perl on `PATH`, or set
`DNB_EXIFTOOL=/absolute/path/to/exiftool`. No local ExifTool tree is copied. Without ExifTool,
the existing no-AF-point subject-selection behavior applies.

## Run safely alongside the originals

These are separate foreground processes in separate terminals; they do not launch or stop
PhotoCraft, ComfyUI or the old servers automatically:

```sh
uv run --extra gpu shp-photocraft-crop --port 18768 --dir /path/to/pictures
uv run --extra gpu shp-photocraft-dnb --port 18765 --dir /path/to/pictures
```

Addresses: http://127.0.0.1:18768 and http://127.0.0.1:18765. All servers bind only to loopback.
`--port 0` selects an ephemeral free port and prints its actual address.

Compatible aliases are `shpcrop` and `shpdnb`. Module entrypoints also work:

```sh
uv run python -m shp_photocraft_tools.modules.crop.server --port 18768
uv run --extra gpu python -m shp_photocraft_tools.modules.dnb.ui_server --port 18765
```

### Completely isolated preview (no real services)

```sh
uv run --extra gpu python tests/preview.py
```

This prints two free-port URLs. Get/Send use fake PhotoCraft, Crop uses a stub ComfyUI,
and D&B uses synthetic detection/mask data with the real CPU correction engine.
All images, presets and results are temporary. Stop with Ctrl+C. This is a test fixture,
not a replacement service or production launcher.

## Using the tools

**Crop:** Get from PhotoCraft (or choose a file from `--dir`), choose a ratio or Free, then
drag the frame/corners/side handles. Each side can crop or expand, but there must be new area
to generate. Generate creates 1-4 variations; the same button becomes Cancel while working.
Result tabs select the variation used by Send. Results support wheel zoom, drag pan and
double-click fit. Send resizes the source canvas and copies the generated RGBA layer;
Crop retains its best-effort single-undo grouping.

The ComfyUI pipeline, models, color matching, seam correction and paste-back are unchanged.
Default URL: `http://127.0.0.1:8188`; override with `--comfy` or `SHPC_COMFY`.
Required custom nodes: ComfyUI-GGUF, comfyui-inpaint-nodes and `SeamColorCorrect`.
See the retained [Crop technical notes](docs/source/crop-handoff.md) for graph details and
model filenames. The explicit Start ComfyUI action remains; it is not invoked by startup or tests.

**D&B:** Get from PhotoCraft to analyze and preview in real time. Detail level, Skin mask,
Structure protection, Light range, Color range and Image analysis remain independent
collapsible sections. The mask checkboxes and light/color pickers remain visible on their
summary rows even when collapsed; collapsing does not alter settings.

The synchronized previews retain wheel zoom/drag pan, double-click reset, Shift-click crop
changes and Ctrl-click tab cycling. Send copies a normal 16-bit RGB Soft Light layer with
its mask into the fetched document in one copy operation; it does not resize the canvas.
Hidden manual load/apply/save controls and all corresponding endpoints remain available.
Saved output is the full-resolution result JPEG, combined mask PNG, unmasked 16-bit RGB
layer PNG and settings JSON. ICC behavior and correction math are unchanged.

## Data, settings and compatibility

| Setting | Default in this repository |
|---|---|
| Crop settings, presets, inbox, results, ComfyUI log | `~/.local/share/shp-photocraft-tools/crop/` |
| D&B presets (including Default), inbox, results | `~/.local/share/shp-photocraft-tools/dnb/` |
| D&B external models | `<D&B data>/models/` |
| Fallback PhotoCraft config/log | `~/.config/shp-photocraft-tools/{crop,dnb}/` |

`SHPC_DATA`, `DNB_DATA`, `DNB_MODELS` and `DNB_EXIFTOOL` override the corresponding locations.
Paths are independent of the working directory and installed package, including wheel installs.
No existing presets, settings, photos, tokens or caches are imported automatically.
Preset schemas are retained: Crop `{presets,last}` plus `settings.json`; D&B `{presets,default}`.
An intentional later copy of your JSON files to the new data locations can reuse them without
conversion. Pointing the new tools at the originals' data would share writes, so do not do
that during side-by-side testing.

Crop retains `SHPC_COMFY`, `SHPC_COMFY_DIR`, `SHPC_LIMIT`, `SHPC_CLIP`, `SHPC_VAE`, `SHPC_LORA`
and `SHPC_PC_*`. D&B retains `DNB_PC_*`. Shared PhotoCraft overrides are `SHP_PC_PORT`,
`SHP_PC_TOKEN_FILE`, `SHP_PC_EXCHANGE` and `SHP_PC_READ_ROOT`; a module's corresponding
`SHPC_PC_*`/`DNB_PC_*` value takes precedence.

Without explicit connection overrides, both still use port 7878 and the existing OpenFill
token/exchange when present. Credentials are read at connection time, never embedded or copied.
Get exports a merged duplicate and closes it; Send intentionally changes the target document.
Do not press Get/Send against a real PhotoCraft merely to validate the migration.

Browser preference keys are retained (`shpx-*`, `dnb-*`). A different port is a different
localStorage origin, so the side-by-side preview starts with separate browser preferences.

## Layout

```text
src/shp_photocraft_tools/
  shared/ui/            one packaged common.css and resource loader
  shared/photocraft/    transport/auth/config/session/fetch/launch
  modules/crop/         Crop server, graph/client, canvas-send logic, UI
  modules/dnb/          D&B server, engine, inference helpers, mask-send logic, UI
tests/                  stub/synthetic integration and regression checks
docs/source/            frozen technical notes from the original projects
```

Both servers expose the exact same local packaged CSS at `/static/shp-ui.css`.
Common pane headers, 74px labels and 42px readouts live there; module-specific UI rules stay
in each HTML file. No CDN, remote CSS package, additional stylesheet server or runtime
source-repository path is involved.

## Checks and builds

```sh
uv run --extra gpu python tests/run.py
uv build
```

Checks use temporary paths and ephemeral ports, stub ComfyUI/PhotoCraft, and synthetic CPU
image processing. They cover Crop geometry/free crop, graph/client/cancel, variations,
settings/presets, CSS, transport auth/reconnect, desktop/headless fetch, separate Send/undo
semantics, cleanup, D&B previews/masks/color picking/ICC/full-resolution save and presets.
They never run model inference or launch real external applications.

The wheel and sdist include both HTML files and the one shared stylesheet.
See [HANDOFF.md](HANDOFF.md) for migration boundaries and [PROVENANCE.md](PROVENANCE.md)
for the exact source revisions. Model notices are retained in [MODELS.md](MODELS.md).
