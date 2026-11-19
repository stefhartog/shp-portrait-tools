# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SHP Portrait Tools: a local desktop companion for portrait retouching on Linux and Windows.
Python package `shp_photocraft_tools` (Python 3.13, managed with uv). Public names differ from
internal IDs — keep internal IDs, routes, commands, and data/config paths unchanged:

- **Skin Tools** = module `dnb` (Skin Mask, FS & Tone, Dodge & Burn, Combine — one shared service)
- **AI Fill** = module `crop` (Crop & Expand, Patch Fill — needs an external ComfyUI; Linux only)
- Editors: **PhotoCraft** (host editor, `shared/photocraft/`) and **Photoshop** (Windows 28.0 preview,
  UXP connector in `plugins/photoshop/`)

Key docs (read the relevant one before non-trivial changes):
- `docs/skin-tools-architecture.md` — Skin Tools module boundaries, request flow, lock ordering, focused test selectors
- `HANDOFF.md` — current-state behavior contracts for the whole app (shell, lifecycle, pose backends, etc.)
- `docs/windows-handoff.md` — Windows port status, what is and isn't verified
- `docs/photoshop.md` — Photoshop connector wire protocol and live-validation notes
- `MODELS.md` — required ONNX models / ComfyUI nodes. `docs/source/` is frozen history, not current guidance.

## Commands

On this Windows machine `uv` is not on PATH: use `python -m uv ...`. Always run with `--no-sync`
after the initial sync.

```powershell
python -m uv sync --locked --extra gpu                       # install (gpu = CUDA/cuDNN user-space libs)
python -m uv run --no-sync python tests/run.py               # full suite, isolated data/config roots via env vars
```

Single test file / test (tests import helpers such as `winlinks` from `tests/`, so set PYTHONPATH):

```powershell
$env:PYTHONPATH = "tests"
python -m uv run --no-sync python -m unittest test_combine -q
python -m uv run --no-sync python -m unittest test_local_results.LocalResultTests.test_one_full_resolution_file_per_mode_with_profile_and_precision
```

- Known-good Windows subset: `test_combine test_transfer_options test_fs_pipeline test_combine_batch test_local_results test_watch_folder test_windows_output test_windows_service`.
  Some tests skip on Windows (need `photocraft-cli`, Linux-only `supervisor`, or symlink privilege).
- `SHP_TEST_DNB_CUDA=1` enables CUDA hardware parity checks in `test_cuda_bands`.
- Photoshop connector mock tests (Node): `node tests\photoshop_plugin.cjs`
  (or `python -m uv tool run --from nodejs-wheel node tests\photoshop_plugin.cjs`). Mocks do not prove live Photoshop behavior.
- No linter/formatter is configured.

Running the app:

```powershell
python -m uv run --no-sync shp-photocraft-tools --init-config   # once; writes %APPDATA%\shp-photocraft-tools\launcher.json
python -m uv run --no-sync shp-photocraft-tools                 # supervised shell + desktop window
python -m uv run --no-sync shp-photocraft-tools --no-browser    # services only; http://127.0.0.1:18760/
python -m uv run --no-sync shp-photocraft-dnb --dir <folder>    # standalone Skin Tools web UI (port 8765)
python -m uv run --no-sync shp-photocraft-crop --dir <folder> --comfy http://127.0.0.1:8188   # standalone AI Fill (8768)
```

Do not restart, kill, or reinstall a running Tools instance without the user's explicit OK: running
backends keep their loaded Python, and a restart can discard loaded/unsent work. A browser refresh does
not pick up Python changes.

## Architecture

```
launcher.py ─► supervisor.py ─► shell/ (port 18760: tab UI, fixed proxy, mounted iframes)
   │                ├─► modules/dnb/ui_server.py   (owned backend 18765)
   │                ├─► modules/crop/server.py     (owned backend 18768)
   │                └─► comfy.py                    (optional owned ComfyUI 18788, Linux)
   └─► desktop/ (Linux: GTK3/WebKit2 controller.py; Windows: windows_controller.py, pywebview + pystray)
```

- Each module is a plain HTTP server (`ThreadingHTTPServer`-based `BrowserServer` from `shared/lifecycle.py`)
  serving a single-file UI (`modules/*/ui/index.html`) plus shared assets from `shared/ui/`
  (no CDN). The shell talks to module iframes via origin-checked `postMessage` (e.g. `shp-tools-crop-mode` with sequence acks).
- `shared/health.py`, `supervision.py`, `lifecycle.py`: ownership capabilities, work admission/draining,
  revision guards and leases. `owned_process.py` / `windows_process.py`: identity-pinned child processes
  (Windows children run under `pythonw.exe` to avoid console windows).
- `shared/platform_paths.py`: per-OS data/model/config locations; overridable with `DNB_DATA`, `SHPC_DATA`,
  `DNB_MODELS`, `SHP_TOOLS_DATA`, `*_WATCH_DIRECTORY`. `launcher.py` and `supervisor.py` import `fcntl`
  and the Linux desktop controller imports `gi`, so those don't import on Windows.
- `shared/local_output.py` (+ `local_output_windows.py`, `winfs.py`): exclusive, never-overwrite numbered
  output files; Windows path pins folders via handles.
- Skin Tools (`modules/dnb/`): `ui_server.py` wires routes; `image_session.py` owns the published source and
  its generation; `preview_service.py` handles `/api/run`; `processing_service.py` assembles full-resolution
  results; `cache_coordinator.py` / `mask_precache.py` / `layer_cache.py` handle background preparation;
  `transfer_service.py` + `photoshop_bridge.py` / `photocraft_bridge.py` handle Save/Send. Algorithms live in
  `skin_mask.py`, `frequency_separation.py`, `skintone.py`, `directional_blur.py`, `dodge_burn.py`;
  optional CUDA D&B in `cuda_bands.py` + `dnb_cuda.cu` (NVRTC, falls back to CPU).
- Scale **S** (pixels per 63 mm) is estimated per subject (`estimate_body_scale.py`, `estimate_scale.py`)
  and drives size-based controls. Pose backend default is rtmlib; Google (Linux, bubblewrap-sandboxed worker)
  and OpenCV Zoo are optional (`pose_backend.py`).

### Invariants to preserve (from the architecture docs)

- Keep route names, JSON fields, progress/error behavior, output filenames, bit depth, blend modes and
  mask placement stable unless the task explicitly changes the contract.
- Never bypass source-generation checks, editor pairing, or document validation; stale background jobs
  must not publish results.
- Preserve native RGB samples and ICC profiles where outputs require them; preview-only effects must not
  change full-resolution Send pixels.
- Lock ordering: full-resolution gate before the preview lock; the mask calculation lock is separate.
  Keep expensive background work outside the preview lock.
- Cache keys use exact dependencies; mix-only changes reuse components, pixel-affecting changes don't.

## Photoshop connector

`plugins/photoshop/` is a UXP plugin (`main.js`, `protocol.js`, `profile_store.js`, `manifest.json`) that
talks to the dnb service at `http://localhost:18765` (the manifest's network domain must match `BASE` in
`main.js`; the Node test asserts this). Packaged `.ccx` builds are tracked under
`plugins/photoshop/releases/<version>/`.
