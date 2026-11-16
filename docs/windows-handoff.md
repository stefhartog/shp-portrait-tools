# Windows port handoff

## Baseline and scope

Reviewed **2026-11-16** against published `main` in
[`stefhartog/shp-portrait-tools`](https://github.com/stefhartog/shp-portrait-tools),
commit `ea615e74ce49d98f55f4bcd5b87af68f001c7d26` (before this documentation update).
Start from fresh `main` and record its SHA; do not copy a dirty Linux installation.
The repository/product rename does not rename Python packages, commands, environment
variables, existing config/data directories or internal module IDs.
Read [README](../README.md), [HANDOFF](../HANDOFF.md) and [MODELS](../MODELS.md) first.
Older handoff/setup documents still contain historical repository names and machine
paths; use the repository above and configure paths locally.

**Full Windows support is not implemented.** Only the Skin Tools (`dnb`) standalone
service and WebUI have been ported; see [Windows port status](#windows-port-status-skin-tools-standalone)
for what was verified. The rest of this page keeps the original Linux-era analysis, and
the table below describes the baseline before the port unless marked otherwise. The previous handoff's 118 passing
Linux tests and one skipped optional live-desktop test described its older baseline,
not a fresh result for current main. No Windows installation, inference, process
management or PhotoCraft integration has been validated for this update. Do not run
the Linux desktop or editor-handoff installers on Windows.

## Windows port status (Skin Tools standalone)

Branch `stefhartog-skin-tools-windows-port`, started from `23cffaf41f0c1e1477e8296c62836451f7a2951f`.
Scope: Skin Tools (`dnb`, port 8765) standalone service and browser WebUI only. Not touched:
GTK desktop/tray, launcher/supervisor/installers, Crop & Expand/AI Fill, Flatpak, LightCraft,
the Linux-only Google pose worker.

### Verified on Windows

Environment: Windows 11, Python 3.13.12, uv 0.13.8 (`python -m uv`, not on PATH), RTX 3090,
driver 610.88. `uv sync --locked` (no `gpu` extra) succeeds.

- **Import probe:** every `shp_photocraft_tools` module imports except the expected
  Linux/optional ones: `launcher` and `supervisor` (`fcntl`), `desktop.controller` (`gi`),
  `dnb.google_pose_worker` (`resource`), `dnb.texture_experiment` (`matplotlib`, research extra).
- **Service:** `python -m shp_photocraft_tools.modules.dnb.ui_server` serves the WebUI on
  loopback (HTTP 200 for `/`, checked with curl on 8765 and with `--port 0` in a test).
- **Paths** (`shared/platform_paths.py`): on win32 data defaults to
  `%LOCALAPPDATA%\shp-photocraft-tools\<module>`, models to `...\dnb\models`, global settings
  to `%APPDATA%\shp-photocraft-tools`, inbox to a folder under the data directory. Overrides:
  `DNB_DATA`, `SHPC_DATA`, `DNB_MODELS`, `DNB_WATCH_DIRECTORY`/`SHPC_WATCH_DIRECTORY`, and
  generic `WATCH_DIRECTORY`. Linux defaults (including the fixed inbox) are unchanged.
- **Output safety** (`shared/winfs.py`, `shared/local_output_windows.py`, dispatched from
  `local_output.py`): `CREATE_NEW` no-overwrite allocation, reserved-name/ADS/trailing dot
  rejection, `\\?\` extended paths for long names, reparse-point refusal, atomic
  `os.replace` for the preset with an external-change check, cleanup of only its own files.
  The parent folders are held open without `FILE_SHARE_DELETE`; renaming or replacing them
  during a write is refused by the OS instead of detected afterwards. This is the Windows
  analogue of `dir_fd` and is weaker in one respect: identity is verified on the handle, not
  inherited by every later call.
- **Inbox** (`shared/watch_folder.py`): no `/proc/self/fd`; folder pinned the same way,
  sharing violations are retried not recorded, reparse-point or non-directory inboxes are rejected.
- **Port ownership** (`shared/lifecycle.py`): on Windows `BrowserServer` binds with
  `SO_EXCLUSIVEADDRUSE` and without `SO_REUSEADDR`. A negative control showed that plain
  `ThreadingHTTPServer` lets a second process share a live port on Windows. Ownership here
  is only "the server process owns its port"; `uv run` adds a wrapper process, so stop the
  Python child. Supervisor/`owned_process` pid and `/proc` ownership are not ported.
- **GPU:** `dnb_gpu.py` registers `site-packages\nvidia\*\bin[\x86_64]` with
  `os.add_dll_directory`, exposes `device()`, and `estimate_body_scale.py` uses it instead of
  a hardcoded `"cuda"` (CPU when the CUDA provider is unavailable). With CUDA 13/cuDNN 9 wheels
  installed into the venv by hand, ONNX Runtime GPU 1.31.1 reported
  `CUDAExecutionProvider` and ran a synthetic Conv model on it.
- **Pose:** the rtmlib backend remains the default. Google Full raises a clear
  Linux-only error on Windows (no bubblewrap); its worker is untouched.
- **Tests** (commands below): the six-module command gives `Ran 58 tests ... OK (skipped=10)`;
  adding `test_windows_output` (18) and `test_windows_service` (1) gives
  `Ran 77 tests ... OK (skipped=11)`. Skips: 6 need `photocraft-cli`; 2 need the Linux-only
  `supervisor`; 1 symbolic-input test and 1 file-symlink test need the symlink privilege;
  1 replaced-folder test is replaced by a Windows test (folders cannot be replaced while pinned).
  Without the privilege, symlink-based tests use hard links or junctions (`tests/winlinks.py`)
  as stand-ins; they check the same no-overwrite behavior but are not real symlink tests.

```powershell
$env:PYTHONPATH = "tests"
python -m uv run --no-sync python -m unittest test_combine test_transfer_options test_fs_pipeline test_combine_batch test_local_results test_watch_folder -q
python -m uv run --no-sync python -m unittest test_windows_output test_windows_service -q
```

### Verified end to end with real models (2026-11-16)

Standalone service on 8765 with the three ONNX models in `%LOCALAPPDATA%\shp-photocraft-tools\dnb\models`
(SHA-256 checked) and a disposable `DNB_DATA`/`DNB_WATCH_DIRECTORY`. Test image: scikit-image's
`astronaut.png` (public domain NASA photo, 512x512; a real face, not a user photo). Same-origin
JSON calls to `/api/load`, `/api/run` (Skin Mask, FS & Tone, Dodge & Burn, Combine) and `/api/save`
(Dodge & Burn, Combine) all succeeded with no Windows-specific errors:

- Load/analysis 29.8 s on first use, including a ~203 MB rtmlib weight download (YOLOX-m and
  RTMW-x downloaded automatically to `~\.cache\rtmlib`); YuNet found the face (IPD 43 px, scale
  S 43.3, confidence "high", mask 2.8%). Previews took 0.1-0.5 s. Saves wrote the expected PNG
  files and `settings.json` under the data directory.
- **Provider:** `CUDAExecutionProvider` (then CPU) is the provider list in use. `get_providers()` on the
  live face-landmark session and on the rtmlib YOLOX and RTMW sessions returned
  `['CUDAExecutionProvider', 'CPUExecutionProvider']`; ONNX Runtime warned that some shape ops stay on CPU,
  which is normal. The selfie-segmentation session is created with the same list but was not
  inspected individually. YuNet runs through OpenCV's `FaceDetectorYN` on CPU.
- `DNB_DATA` also relocates the default model directory (`<DNB_DATA>\models`); set `DNB_MODELS`
  to keep models elsewhere.
- The six-module test command still gives `Ran 58 tests ... OK (skipped=10)`.

Not covered by that run: portrait accuracy/quality and speed on larger (multi-megapixel) images,
profiles beyond sRGB, multiple faces, CPU-only provider path end to end, WebUI interaction in a
browser, batch/PhotoCraft send.

### Not verified / assumptions / blockers

- **`--extra gpu` on Windows:** fixed by adding `required-environments` (linux, win32) to `[tool.uv]` and relocking. The lock now forks `nvidia-cublas`: Linux keeps 13.8.2.12, win32 uses 13.10.0.20 (13.8.2.12 has no Windows wheel; the other CUDA libs already had Windows wheels). Verified: `uv sync --locked --extra gpu` succeeds, ONNX Runtime 1.31.1 lists `CUDAExecutionProvider`, `add_windows_dll_directories()` finds `nvidia\cudnn\bin` and `nvidia\cu13\bin\x86_64` in the venv. Not re-verified with the lock-installed libs: real model inference (the earlier Conv check used hand-installed wheels). The lock was not tested on Linux.
- **Model weights:** not bundled. Full analysis needs `yunet.onnx`, `face_landmarks.onnx` and
  `selfie_multiclass_256x256.onnx` in `%LOCALAPPDATA%\shp-photocraft-tools\dnb\models` (or
  `DNB_MODELS`) per MODELS.md; rtmlib downloads the pose weights on first use. Inference was
  verified on one public-domain face (see above); accuracy and speed on real-size portraits are **not verified**.
- Not tested: symlink semantics (needs Developer Mode/elevation), CPU-only fallback end to end,
  network/cross-volume paths, locked-file behavior beyond the unit tests, antivirus interaction,
  the WebUI beyond fetching `/`, PhotoCraft/ComfyUI/LightCraft, launcher/tray/desktop.
- `launcher`, `supervisor`, `shared/owned_process.py` and `comfy.py` remain Linux-only.

## Committed feature state

- Skin Tools includes Skin Mask, FS & Tone, Dodge & Burn and Combine, with shared source
  and global settings, local Combine batches and separate Skin Tools/AI Fill PNG inboxes.
  The shell initially selects Skin Mask when available and preserves later user selection.
- Crop & Expand supports FLUX.2 dev, FLUX.1 Fill and, since `5cd5ac4`, FLUX.2 Klein.
  Klein has separate settings, local seam blending/correction and optional compatible
  LoRA support. **Patch Fill in this published baseline uses FLUX.1 Fill only.**
  Uncommitted Klein Patch Fill/reference-preparation work is not included.
- Since `374151a`, pose analysis optionally supports Google Full in an isolated CPU
  worker or OpenCV Zoo MediaPipe ONNX on CPU. rtmlib remains the default. Backend settings
  apply on the next analysis/batch and do not rewrite a loaded source or presets.
  Neither alternative establishes a CPU-only configuration for all Skin Tools inference.
- PhotoCraft transport includes opt-in Linux Flatpak discovery/peer verification;
  the native editor-handoff installer and authenticated LightCraft helper also exist.
  These are not Windows packaging or proof of arbitrary-location, same-file save-back.

## Reusable code and concrete blockers

| Area | Current implementation | Windows work to investigate |
|---|---|---|
| Processing and WebUI | Python image processing plus packaged HTML/CSS/JS; AI Fill (`crop`, Crop & Expand/Patch Fill) and Skin Tools (`dnb`) HTTP services | Start with standalone service/browser tests, not a native desktop rewrite. Imports and synthetic tests still need platform verification. |
| Python/dependencies | `pyproject.toml` requires Python 3.13; `uv.lock` pins NumPy, Pillow, OpenCV, ONNX Runtime GPU and rtmlib | Verify Windows wheels and lock resolution with `uv sync --locked`; report unavailable wheels rather than silently changing versions. Plain pip does not inherit uv's ONNX override. |
| GPU/models | Optional `gpu` extra supplies CUDA 13/cuDNN libraries; `modules/dnb/dnb_gpu.py` preloads libraries; `estimate_body_scale.py` explicitly requests CUDA for Wholebody | Verify Windows DLL discovery, driver/provider compatibility and real inference separately. CPU-only full analysis is not guaranteed. Provide external weights per MODELS; never commit weights or caches. |
| Experimental pose | `modules/dnb/pose_backend.py` requires Linux bubblewrap/namespaces and `bin/python` for Google Full; `scripts/google-pose-requirements.txt` hashes Linux-specific wheels. `opencv_zoo_pose.py` uses existing OpenCV DNN with a pinned two-model pair | Keep rtmlib as baseline. Do not run the Google provisioning instructions unchanged or remove isolation to enable Windows. OpenCV Zoo is a separate candidate for Windows verification, not a verified port; Linux checks covered blank/synthetic input, not portrait accuracy or speed. |
| AI Fill/ComfyUI | External ComfyUI has its own PyTorch environment/models/nodes; `comfy.py` searches `venv/bin/python` or `.venv/bin/python` and uses `/proc` listener inspection. Klein uses core FLUX.2 nodes and a local safetensors shape check for optional LoRAs | Validate against an explicitly started external ComfyUI first. Later support `Scripts/python.exe` and Windows-safe ownership/lifecycle; do not attach to or terminate arbitrary listeners. Follow method-specific MODELS requirements; Klein is not FLUX.2 dev/GGUF and does not require the custom seam node. |
| Native desktop | `desktop/controller.py` runs system Python with GTK3, WebKit2 4.1 and AppIndicator3; GLib Unix signals, GTK file choosers and tray presentation | These are the Linux desktop implementation, not a verified Windows runtime. Choose a Windows host only after browser/service validation; preserve existing UI and lifecycle contracts. |
| Launcher/install/lifetime | `launcher.py` imports `fcntl`, uses file locks and `/usr/bin/python3`; `scripts/install_desktop.py` creates shell wrappers, XDG `.desktop` files and icons. Supervisor/process ownership uses `/proc`, Linux pidfds/signals and session semantics | Replace locks, platform paths, launch/install assets and verified process identity/termination. Standalone module commands avoid launcher import but do not provide equivalent owned-shell/tray management. |
| Output/file safety | `shared/local_output.py` uses directory descriptors, `dir_fd`, `O_DIRECTORY`, `O_NOFOLLOW`, exclusive allocation, directory fsync and atomic manifest replacement | Provide Windows equivalents with no-overwrite and replacement/symlink/reparse-point safety. Test case-insensitive collisions, reserved names, Unicode/spaces, long paths, locked files and cross-volume behavior. Do not remove safety checks just to make writes succeed. |
| Inbox imports | `shared/watch_folder.py` pins directories/files and validates PNG completion/CRC; uses `/proc/self/fd`, POSIX descriptor operations. Both server `WATCH_DIRECTORY` constants currently point to fixed Linux paths | Make inbox locations portable/configurable before enabling Windows imports. Use disposable configured directories, never a developer's real inbox. Retain stable-file detection, association checks, deduplication and cleanup only after unchanged successful import. Missing inboxes currently are not auto-created. |
| Settings/data | `DNB_DATA`, `SHPC_DATA`, `DNB_MODELS`, XDG global config and PhotoCraft config defaults are Unix-oriented | Define Windows config/data/state defaults and migration explicitly; preserve global-vs-preset separation and environment overrides. Use portable user-local placeholders, not copied machine paths. |
| PhotoCraft | `shared/photocraft/bridge.py` uses authenticated localhost JSON-lines TCP and exchange files. `SHP_PC_*` overrides are shared; `DNB_PC_*`/`SHPC_PC_*` take precedence. One spawn path already guards `start_new_session` on win32. Optional `flatpak_peer.py` uses Linux `/proc`, pidfds and Flatpak identity | Verify a Windows PhotoCraft build exposes the same control/automation APIs, path permissions and export capabilities. That single spawn guard is not Windows support. Do not copy Linux Flatpak guard configuration or disable authentication to make a connection succeed; define the Windows trust/process-identity mechanism separately. Configure token/exchange/read-root locally; never copy or commit credentials. |
| LightCraft handoff | `editor_handoff.py` validates input paths against the read root and sends one-shot acknowledged `app.open`; `scripts/install_editor_handoff.py` writes Linux shell wrappers/XDG desktop and native LightCraft settings | Windows needs its own executable/helper discovery, launcher and permission setup. An open acknowledgement is not proof of same-file save-back. Verify that an alongside-original edit can be reopened and saved in place without moving it or granting blanket drive access. |

PNG inboxes have no origin identity. Automatic association deliberately uses the active
PhotoCraft document at processing time, checks available identity/revision/dimensions
around load, and retains files on failure. It cannot prove a same-sized active document
is the one that exported the PNG. Preserve this limitation in Windows UX/docs.

## PhotoCraft/LightCraft integration boundary

These are source-inspected constraints of **PhotoCraft 0.23.1**, not a Windows
end-to-end result or a promise about later PhotoCraft versions:

- Tools Get Image/Send Layers uses authenticated control plus scoped exchange files.
  A reachable port or the GUI's Allow Agent Control switch alone does not configure
  the shared token and filesystem capabilities.
- `--control` and `PHOTOCRAFT_CONTROL_PORT` follow the same startup path and bypass
  ordinary single-instance forwarding. A preferences-only control port can coexist
  with normal single-instance startup, but read/write-root flags **and their environment
  equivalents** are also treated as per-process options and bypass forwarding.
  Moving the current launch flags to environment variables is therefore not a fix.
- Automation `app.open` reads only authorized roots. Its implicit save-back path is
  retained only when the selected read capability is also the single write capability.
  An additional read root does not authorize saving an edit back beside its original.
  Ordinary user File > Open follows a different, trusted UI path; it is not equivalent
  to automation `app.open`.
- LightCraft's intended workflow creates an edit beside the original and opens that
  file in the editor. Do not claim that the current authenticated helper preserves
  arbitrary-folder save-back or reaches an existing ordinary editor instance without
  checking the actual PhotoCraft launch and save behavior.

Source references: PhotoCraft v0.23.1
[`main.rs`](https://github.com/storytold/photocraft/blob/v0.23.1/apps/photocraft/src/main.rs),
[`launch_args.rs`](https://github.com/storytold/photocraft/blob/v0.23.1/apps/photocraft/src/launch_args.rs),
[`workspace.rs`](https://github.com/storytold/photocraft/blob/v0.23.1/crates/automation/src/workspace.rs);
LightCraft v0.21.1
[`convert.rs`](https://github.com/storytold/lightcraft/blob/v0.21.1/crates/engine/src/cmd/convert.rs).
Recheck the installed Windows versions rather than assuming Linux source observations
establish platform behavior.

**Linux Flatpak only:** default LightCraft code attempts `flatpak run --file-forwarding`
for a Flatpak editor. Availability inside the installed sandbox and automatic handoff
were not established. A disposable document-portal grant was verified to write the same
underlying host file, but that alone does not supply PhotoCraft automation save authority
or solve controlled-instance forwarding. Host-spawn bridges, Flatpak permissions and
Linux portal paths are not Windows solutions and are not shipped here as Windows setup.

## Browser-first validation sequence

1. Clone `https://github.com/stefhartog/shp-portrait-tools.git` on Windows, check out
   current `main`, and record `git rev-parse HEAD`.
   Install Git, uv and Python 3.13. Attempt `uv sync --locked`; add `--extra gpu` only
   after checking Windows library/driver availability. Capture actual failures.
2. Set `DNB_DATA`, `SHPC_DATA` and `DNB_MODELS` to disposable/user-local absolute
   Windows directories as appropriate. Prepare external models using MODELS. Import
   each standalone module and open its loopback WebUI before attempting launcher/desktop.
   The documented standalone ports are Skin Tools (`dnb`) 8765 and AI Fill (`crop`) 8768.
3. Use synthetic fixtures first. Translate the Linux test invocation to PowerShell:

   ```powershell
   $env:PYTHONPATH = "tests"
   uv run --no-sync python -m unittest test_combine test_transfer_options test_fs_pipeline -q
   ```

   This is a proposed first probe, not a verified Windows passing command. Some tests
   require an isolated `photocraft-cli`; inspect skip output and test fixtures.
   Browser tests use platform tooling and may require adaptation rather than assuming
   their Linux JavaScriptCore/Firefox harness works unchanged.
4. Port safe filesystem operations with temporary-directory tests. Then run
   `test_combine_batch`, `test_local_results` and `test_watch_folder`; verify mixed
   failures/cancellation, collisions/no-overwrite, file replacement, partial PNG
   retention, successful cleanup, independent inbox routing and active-document changes.
5. Validate real NVIDIA analysis and an external ComfyUI with the selected method's
   models/nodes, then an explicitly configured Windows PhotoCraft instance. Use disposable
   documents to verify Get Image, Send Layers, authentication failure, root rejection,
   existing-instance routing and save-back beside the original, including a different
   drive if required. Preserve serialized control calls and non-retried final insertion
   or file-open handoff when a reply is lost. No live-document test without deliberate
   user setup.
6. Only then choose/implement a Windows desktop host and installation. Preserve one
   controller/supervisor, hide-to-tray, explicit service controls, busy/unsent guards,
   native single/multiple/folder pickers, and wake/show/import from both inboxes.
   A browser alone has no native picker bridge or tray wake controller; batch source
   selection currently requires the desktop's `SHP_OPEN_BATCH_SOURCE_V1` bridge.

## Fresh Copilot session kickoff

> Port this repository to Windows incrementally from the current merged main.
> Read docs/windows-handoff.md, README.md, HANDOFF.md and MODELS.md. Record the baseline
> SHA and inspect the Windows environment first. Reproduce dependency/import failures
> with standalone services and browser UI before selecting a native shell. Preserve
> image math, global settings, PhotoCraft semantics and safe filesystem/process ownership.
> Keep Linux-only Google worker isolation and Flatpak integration separate from Windows
> work; do not copy uncommitted Linux features into the port's baseline.
> Use disposable fixtures/inboxes; do not import/delete real photos, restart installed
> apps, change an existing launcher or commit secrets. Report verified Windows results
> separately from remaining assumptions. Do not claim support from Linux tests alone.
