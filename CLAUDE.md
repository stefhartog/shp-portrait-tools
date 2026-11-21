# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SHP Portrait Tools: a local desktop companion for portrait retouching on Linux and Windows.
Python package `shp_portrait_tools` (Python 3.13, managed with uv; renamed from `shp_photocraft_tools`
on 2026-11-21). An internal rename to Portrait/Skin Tools names is in progress, one piece at a time
(see `TRANSITION-install-and-split.md` Phase 1b if present). Distribution `shp-portrait-tools`; commands
`shp-portrait-tools` and `shp-skin-tools` (old `shp-photocraft-tools`/`shp-photocraft-dnb`/`shpdnb` remain as
aliases for one release). Data/config/state folders are `shp-portrait-tools`; `shared/platform_paths.py`
falls back to an unmigrated `shp-photocraft-tools` folder and `migrate_legacy_roots()` (run by the launcher,
`shp-skin-tools` and `install_desktop.py`, never at import) moves only app-owned entries. The per-module PhotoCraft
control folders (`photocraft_config_dir()`) and AI Fill's `crop` data keep the old name. Still
unchanged: app identity strings (`shp-photocraft-tools` window class/icons/health), output filename suffixes
(`shpdnb` etc.).
Never rename the Photoshop connector ID `shp.skin-tools.connector`, or anything PhotoCraft relies on.

- **Skin Tools** = module `skin` (`modules/skin`, supervisor API `/api/modules/skin/<action>`, `SKIN_*` env vars;
  it was `dnb` before 2026-11-21). Skin Mask, FS & Tone, Dodge & Burn, Combine — one shared service. `dnb` is still the Dodge & Burn
  *feature* name (the `dnb` mode/tab, `shpdnb` outputs, `DNB_LAYER_BACKEND`, `dnb-*` UI preferences) and the
  PhotoCraft namespace (`detect_config("DNB", "dnb")`, `DNB_PC_*`, the `photocraft-dnb` LightCraft wrapper): keep
  those. Old `dnb` launcher.json keys, `DNB_DATA`/`DNB_MODELS`/`DNB_EXIFTOOL` and the
  `<data>/dnb` folder still work as fallbacks; `migrate_legacy_roots()` renames that folder to `skin`.
- **AI Fill** (former module `crop`: Crop & Expand, Patch Fill, ComfyUI) was split out to the separate
  `shp-ai-fill` repo on 2026-11-21. `launcher.read_config` still accepts and ignores legacy `crop`/`comfy`
  entries in existing `launcher.json` files; don't reintroduce AI Fill code here.
- Editors: **PhotoCraft** (host editor, `shared/photocraft/`) and **Photoshop** (Windows 28.0 preview,
  UXP connector in `plugins/photoshop/`)

Key docs (read the relevant one before non-trivial changes):
- `docs/skin-tools-architecture.md` — Skin Tools module boundaries, request flow, lock ordering, focused test selectors
- `HANDOFF.md` — current-state behavior contracts for the whole app (lifecycle, pose backends, etc.)
- `docs/linux-todo.md` — **Linux is knowingly broken** since the Windows-first desktop changes; what to do there
- `docs/windows-handoff.md` — Windows port status, what is and isn't verified
- `docs/photoshop.md` — Photoshop connector wire protocol and live-validation notes
- `MODELS.md` — required ONNX models. `docs/source/` is frozen history, not current guidance.

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

- Known-good Windows subset: `test_combine test_transfer_options test_fs_pipeline test_combine_batch test_local_results test_windows_output test_windows_service`.
  Some tests skip on Windows (need `photocraft-cli`, Linux-only `supervisor`, or symlink privilege).
- `SHP_TEST_DNB_CUDA=1` enables CUDA hardware parity checks in `test_cuda_bands`.
- Photoshop connector mock tests (Node): `node tests\photoshop_plugin.cjs`
  (or `python -m uv tool run --from nodejs-wheel node tests\photoshop_plugin.cjs`). Mocks do not prove live Photoshop behavior.
- Several UI tests run only with Node on PATH (they skip otherwise); `test_dnb_progress`'s big harness needs Linux
  JavaScriptCore, `test_status_layout` needs Firefox, and `tests/desktop_smoke.py` is an opt-in Linux GTK smoke
  (`SHP_DESKTOP_SMOKE=1`) that still drives the removed AI Fill module and needs updating.
- No linter/formatter is configured.

Running the app:

```powershell
python -m uv run --no-sync shp-portrait-tools --init-config   # once; writes %APPDATA%\shp-portrait-tools\launcher.json
python -m uv run --no-sync shp-portrait-tools                 # supervisor + Skin Tools + desktop window
python -m uv run --no-sync shp-portrait-tools --no-browser    # services only; http://127.0.0.1:18760/ (status page)
python -m uv run --no-sync shp-skin-tools --dir <folder>      # standalone Skin Tools web UI (port 8765)
```

Do not restart, kill, or reinstall a running Tools instance without the user's explicit OK: running
backends keep their loaded Python, and a restart can discard loaded/unsent work. A browser refresh does
not pick up Python changes.

## Architecture

```
launcher.py ─► supervisor.py (port 18760: status page at /, health/status/controls/quit/leases; no UI proxy)
   │                └─► modules/skin/ui_server.py   (owned worker 18765: the Skin Tools page, loaded directly)
   └─► desktop/ (Windows: windows_controller.Host = host.DesktopHost + WebView2/Win32/pystray;
                Linux: GTK3/WebKit2 controller.py — broken since the shell removal, see docs/linux-todo.md)
```

- Each module is a plain HTTP server (`ThreadingHTTPServer`-based `BrowserServer` from `shared/lifecycle.py`)
  serving a single-file UI (`modules/skin/ui/index.html`) plus shared assets from `shared/ui/`
  (no CDN). The page owns its tab bar (Skin Mask, FS & Tone, Dodge & Burn, Combine, Settings & Info) and the
  Settings & Info view; there is no shell any more. The supervisor manages exactly one worker (`SkinWorker`;
  `MODULE`/`SERVER_MODULE` in `launcher.py`). Its API keeps the per-module shape (`/api/status` → `modules.skin`,
  `/api/modules/skin/<action>`) used by the tray hosts. `/` is `supervisor.STATUS_PAGE`: it shows starting/stopped/
  error and moves on to the worker page when running, handing over the old origin's `shp-tools-theme`/`dnb-*`
  localStorage once (the page imports them in its `data-prefs-import` head script). The supervised worker counts
  browser tabs open on it (`ui_clients` in owner status) so `keep_alive()` holds the supervisor up for them.
- `desktop/host.py` `DesktopHost`: platform-neutral window geometry, status polling, tray model, module controls
  and the quit/restart/reload confirmation flow (with the 90 s drain wait). It reads the page origin from the
  supervisor's `/api/health` `ports.skin`, allows navigation to both origins (`trusted_origin`), and shows the
  status page again when Skin Tools stops or restarts (`follow_worker`). Platform hosts subclass it and add only
  dialogs/pickers, webview hooks, the tray widget and process helpers (`own_identity`, `process_alive`,
  `spawn_detached`). Covered by `tests/test_desktop_host.py` with a fake platform.
  File pickers: `PickerApi` (only `pick_image`/`pick_images`/`pick_folder`) is the window's pywebview `js_api`;
  the Skin Tools page reaches it as `window.top.pywebview.api` and asks Folder-or-Images
  in its own `#batchSourceDialog`. Without the bridge (Linux GTK host) the page falls back to the old
  `prompt('SHP_OPEN_*')` protocol. The Windows host no longer bridges prompts; its WebView2 dialog hook only
  gives `alert`/`confirm` native message boxes.
- `shared/health.py`, `supervision.py`, `lifecycle.py`: ownership capabilities, work admission/draining,
  revision guards and leases. `owned_process.py` / `windows_process.py`: identity-pinned child processes
  (Windows children run under `pythonw.exe` to avoid console windows).
- `shared/platform_paths.py`: per-OS data/model/config locations; overridable with `SKIN_DATA`,
  `SKIN_MODELS`, `SHP_TOOLS_DATA`. `launcher.py` and `supervisor.py` import `fcntl`
  and the Linux desktop controller imports `gi`, so those don't import on Windows.
- `shared/local_output.py` (+ `local_output_windows.py`, `winfs.py`): exclusive, never-overwrite numbered
  output files; Windows path pins folders via handles.
- Skin Tools (`modules/skin/`): `ui_server.py` wires routes; `image_session.py` owns the published source and
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
talks to the Skin Tools service at `http://localhost:18765` (the manifest's network domain must match `BASE` in
`main.js`; the Node test asserts this). Packaged `.ccx` builds are tracked under
`plugins/photoshop/releases/<version>/`.
