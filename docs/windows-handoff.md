#### No-console launch and Start Menu shortcut

Why `python -m uv run --no-sync shp-photocraft-tools` used to leave extra terminal windows: the venv
`python.exe` is a console-subsystem redirector that starts the real interpreter as a child. The
supervisor and the desktop host were spawned detached (no console), so each real child allocated a
brand-new console, which Windows Terminal showed as a window (observed: the redirector had no console
host, its child had a conhost + a Windows Terminal window, for the supervisor and the desktop host; the
`uv`/console-script wrapper is your own terminal). `CREATE_NO_WINDOW` on the redirector does not reach
its child. Fix: the supervisor, services and desktop host are started with `pythonw.exe` (GUI
subsystem, never gets a console) via `shared/windows_process.background_python()`; stdout/stderr still go to the
log files. `--check`/`--init-config` still run from a console with `python.exe`. Linux is unchanged. The
launcher compares `python.exe` and `pythonw.exe` of the same venv as the same interpreter. Under
`pythonw` a startup failure is appended to `<data>\state\launcher.log` and shown in a MessageBox.

```powershell
python -m uv run --no-sync python scripts/install_windows_shortcut.py --dry-run     # preview, changes nothing
python -m uv run --no-sync python scripts/install_windows_shortcut.py --desktop     # Start Menu (+ Desktop) shortcut
python -m uv run --no-sync python scripts/install_windows_shortcut.py --remove --desktop
```

It never overwrites an existing shortcut without `--force`; `--output-dir <dir>` writes elsewhere (used
by the tests). Target `<repo>\.venv\Scripts\pythonw.exe`, arguments `-m shp_portrait_tools.launcher
--config %APPDATA%\shp-portrait-tools\launcher.json`, start in `<repo>`, icon drawn from
`desktop/shp-photocraft-tools.svg` into `shp-photocraft-tools.ico` next to `launcher.json`. Run
`--init-config` first.

The Windows host sets the explicit taskbar AppUserModelID `SHP.PortraitTools` before any native
GUI initialization; the shortcut installer writes the same `System.AppUserModel.ID` to each shortcut.
The host runs in a separate `pythonw.exe` process, so shortcut metadata alone is not sufficient.
Its WinForms/WebView2 window receives the same multi-size ICO as the launcher through
`webview.start(icon=...)`, rather than the Python executable's default icon. The shared Pillow
renderer preserves the existing launcher's SVG shapes/palette and renders the grayscale
`desktop/shp-photocraft-tools-tray.svg` variant for pystray; this is not a browser favicon change.
ICO frames use native DIB encoding because WinForms/System.Drawing can misdecode PNG-compressed
frames even when Explorer displays them correctly.
Existing shortcuts can have just their AppID updated with
`desktop.windows_identity.shortcut_app_id(path, APP_ID)` without recreating them or changing their
target, arguments, working directory, icon or user properties. Fully quit Tools from the tray and
relaunch the normal shortcut to load a host update (Reload UI does not replace the host).
Explorer may retain an older pinned shortcut's identity; only if grouping is still stale after
that relaunch, the user can unpin the old item and pin the updated normal shortcut again.
Shortcut metadata and an isolated native-window check do not by themselves prove grouping on a
user's live taskbar.

The normal shortcut/supervisor starts the same Skin Tools service that includes the Photoshop
connector backend; no alternate startup command or new shortcut arguments are needed. Choose
**Settings & Info > Source editor** in Skin Tools. That choice is stored as `source_editor` in the
existing `%APPDATA%\shp-portrait-tools\settings.json`, preserving other settings and the PhotoCraft
transport. Older settings default to PhotoCraft. Restarts restore the choice, not a fetched document
or connection; Photoshop must reconnect before Get Image, and disconnected Photoshop never silently
falls back to PhotoCraft. The shortcut must target a durable installation containing the changes;
quit Tools from its tray when work is finished, close other Tools browser windows, and open the
updated shortcut to load the new service. An old controller's Restart or Reload UI does not
switch to the new shortcut target.
The session setup guide records this machine's user-local release, shortcut backups, and guarded
rollback; no existing launcher configuration, pairing credentials or running process was changed.

Verified (real runs, disposable data dir, real models read-only): launching
`pythonw.exe -m shp_portrait_tools.launcher --config ...` as the shortcut does leaves only
`pythonw.exe` processes (no conhost/OpenConsole/cmd/python.exe, no console-class window), shows the
WebUI window, Skin Tools answers 200 on 18765; `tests/windows_host_smoke.py` passes (tray created, quit
leaves nothing listening); a bad config under pythonw logs and shows the MessageBox; the installer's
dry-run/create/force/remove run in a temp dir (unit tests). Not verified: the `.lnk` itself launched from the
Start Menu/Explorer, the shortcut icon rendering, the real tray right-click Quit (the same code path is
smoke-tested), and that killing the desktop host leaves the supervisor until its lease expires
(pre-existing behaviour; it was still running after ~50 s).
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

**Full Windows support is not implemented.** The Skin Tools (`dnb`) standalone
service and WebUI have been ported; see [Windows port status](#windows-port-status-skin-tools-standalone)
for what was verified. The rest of this page keeps the original Linux-era analysis, and
the table below describes the baseline before the port unless marked otherwise. The previous handoff's 118 passing
Linux tests and one skipped optional live-desktop test described its older baseline,
not a fresh result for current main. PhotoCraft transport verification is described below;
the full Skin Tools UI-to-editor workflow has not yet been verified. Do not run
the Linux desktop or editor-handoff installers on Windows.

## Windows port status (Skin Tools standalone)

Branch `stefhartog-skin-tools-windows-port`, started from `23cffaf41f0c1e1477e8296c62836451f7a2951f`.
Scope of the first port: Skin Tools (`dnb`, port 8765) standalone service and browser WebUI. The
Windows desktop host (launcher/supervisor/pywebview/pystray) was added later, see below. Still not
touched: GTK desktop/tray, installers, Crop & Expand/AI Fill, Flatpak, LightCraft,
the Linux-only Google pose worker.

### Verified on Windows

Environment: Windows 11, Python 3.13.12, uv 0.13.8 (`python -m uv`, not on PATH), RTX 3090,
driver 610.88. `uv sync --locked` (no `gpu` extra) succeeds.

When updating an environment that previously installed both OpenCV wheels, run
`python -m uv sync --locked --reinstall-package opencv-contrib-python` once
(add `--extra gpu` if used). Removing the overlapping non-contrib wheel can
remove shared `cv2` files; the reinstall restores them. The lock excludes
rtmlib's redundant `opencv-python` dependency, but pip does not apply uv
overrides and dependency checks still report that requirement as missing.

- **Import probe:** every `shp_portrait_tools` module imports except the expected
  Linux/optional ones: `launcher` and `supervisor` (`fcntl`), `desktop.controller` (`gi`),
  `dnb.google_pose_worker` (`resource`), `dnb.texture_experiment` (`matplotlib`, research extra).
- **Service:** `python -m shp_portrait_tools.modules.skin.ui_server` serves the WebUI on
  loopback (HTTP 200 for `/`, checked with curl on 8765 and with `--port 0` in a test).
    In a standalone browser (no `window.__shpNativeFileDialog`) Image analysis shows the image
    list + path box + Load; the desktop window hides them. Verified with headless Edge and
    `/api/load` on a synthetic copy of the scikit-image astronaut.- **Paths** (`shared/platform_paths.py`): on win32 data defaults to
  `%LOCALAPPDATA%\shp-portrait-tools\<module>`, models to `...\dnb\models`, global settings
  to `%APPDATA%\shp-portrait-tools`, inbox to a folder under the data directory. Overrides:
  `SKIN_DATA`, `SHPC_DATA`, `SKIN_MODELS`, `SKIN_WATCH_DIRECTORY`/`SHPC_WATCH_DIRECTORY`, and
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

Standalone service on 8765 with the three ONNX models in `%LOCALAPPDATA%\shp-portrait-tools\skin\models`
(SHA-256 checked) and a disposable `SKIN_DATA`/`SKIN_WATCH_DIRECTORY`. Test image: scikit-image's
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
- `SKIN_DATA` also relocates the default model directory (`<SKIN_DATA>\models`); set `SKIN_MODELS`
  to keep models elsewhere.
- The six-module test command still gives `Ran 58 tests ... OK (skipped=10)`.

Not covered by that run: portrait accuracy/quality and speed on larger (multi-megapixel) images,
profiles beyond sRGB, multiple faces, CPU-only provider path end to end, WebUI interaction in a
browser, batch/PhotoCraft send.

### Not verified / assumptions / blockers

- **`--extra gpu` on Windows:** fixed by adding `required-environments` (linux, win32) to `[tool.uv]` and relocking. The lock now forks `nvidia-cublas`: Linux keeps 13.8.2.12, win32 uses 13.10.0.20 (13.8.2.12 has no Windows wheel; the other CUDA libs already had Windows wheels). Verified: `uv sync --locked --extra gpu` succeeds, ONNX Runtime 1.31.1 lists `CUDAExecutionProvider`, `add_windows_dll_directories()` finds `nvidia\cudnn\bin` and `nvidia\cu13\bin\x86_64` in the venv. Not re-verified with the lock-installed libs: real model inference (the earlier Conv check used hand-installed wheels). The lock was not tested on Linux.
- **Model weights:** not bundled. Full analysis needs `yunet.onnx`, `face_landmarks.onnx` and
  `selfie_multiclass_256x256.onnx` in `%LOCALAPPDATA%\shp-portrait-tools\skin\models` (or
  `SKIN_MODELS`) per MODELS.md; rtmlib downloads the pose weights on first use. Inference was
  verified on one public-domain face (see above); accuracy and speed on real-size portraits are **not verified**.
- Not tested: symlink semantics (needs Developer Mode/elevation), CPU-only fallback end to end,
  network/cross-volume paths, locked-file behavior beyond the unit tests, antivirus interaction,
  the WebUI beyond fetching `/`, PhotoCraft/ComfyUI/LightCraft, launcher/tray/desktop.
- `comfy.py` remains Linux-only (rejected in the Windows launcher config). The launcher,
  supervisor and ownership primitives now have Windows paths; see the desktop host section.

## Windows desktop host design (pywebview + pystray)

Decision: one Windows host process shows the existing shell in a pywebview (WebView2) window
and a pystray tray icon. The WebUI, shell protocol (`/api/status`, `/api/modules/*`,
`/api/ui/*`, `/api/ui/native-quit`) and the supervisor that owns the services are reused, so the
only new UI-side code is `desktop/windows_controller.py`; the Linux GTK controller is not modified.
Scope: Skin Tools (`dnb`, 18765 in the launcher) first. AI Fill (`crop`) is wired only because the
supervisor already treats both modules uniformly (tray rows, start/stop); it is off by default in
the Windows config and not verified (it needs a ComfyUI that is not ported).

| Linux responsibility | Windows mapping |
|---|---|
| Launcher single-flight lock (`fcntl.flock`) | `shared/filelock.py`: `msvcrt.locking` on a lock file (same call sites as `flock`) |
| `/usr/bin/python3`, XDG paths | `sys.executable`; `%LOCALAPPDATA%`/`%APPDATA%` via `platform_paths`; `launcher.json` written explicitly by `--init-config`, never implicitly |
| Single desktop instance (GTK application ID) | Named mutex per shell URL; a second launch signals a named event so the first window shows itself, then exits |
| Process identity (`/proc` session id + start ticks) | `(pid, creation FILETIME)` from `GetProcessTimes`; recorded at spawn and rechecked before any stop |
| Ownership/termination (session + pidfds) | Each service the supervisor spawns is placed in its own Job Object (`CREATE_NEW_PROCESS_GROUP`); only that job is ever terminated, and only after pid + creation time are verified. Port listeners the host did not start are never touched. Graceful owner-protocol stop stays first. |
| Child exits when supervisor dies (`getppid`) | Supervisor pid + creation time passed in the environment and polled (Windows keeps a stale parent pid) |
| Hide to tray / tray menu | Window close hides to the pystray icon; menu mirrors the GTK one (service rows, Show/Hide, Restart, Force Restart, Quit) |
| Busy / unsent-edit guards | Same `/api/status` busy and `(instance, revision, loaded)` signature checks and confirmation dialogs before native-quit; shared helpers moved to `desktop/protocol.py` |
| Native pickers | WebView2 `ScriptDialogOpening` answers the same `SHP_OPEN_IMAGE_V1`, `SHP_OPEN_BATCH_FOLDER_V1`, `SHP_OPEN_BATCH_IMAGES_V1`, `SHP_OPEN_BATCH_SOURCE_V1` prompts with pywebview file/folder dialogs; `__shpNativeFileDialog` is injected for the app origin in all frames and `__shpOwnedDesktop` in the top frame via `AddScriptToExecuteOnDocumentCreated` |
| Wake/show/import from both inboxes | Same 3 s poll of `/<module>/api/watch-folder`; a new ready token shows the window |
| SIGTERM quit | `SIGBREAK`/Ctrl+C request the same confirmed quit path; tray Quit is primary |

Known weakening versus Linux: only the services the supervisor spawns are job-contained (spawned
suspended, assigned to their own Job Object, then resumed, so nothing can escape in a gap). The
launcher-spawned supervisor and the desktop host are not in a job; they are identified by
`SHP_SERVER_INSTANCE`/`(pid, creation time)` and the supervisor exits its children when it exits.
Closing the window hides it (tray Quit is the only way to stop services; Ctrl+C/SIGBREAK is not
handled while the WinForms loop runs, so a killed host leaves the supervisor running until its
lifetime lease expires).

### Windows desktop host: implemented

Commands (from the worktree; nothing is installed, no shortcut/registry/Start menu is touched):

```powershell
python -m uv sync --locked --extra gpu           # includes win32-only pywebview + pystray
python -m uv run --no-sync shp-portrait-tools --init-config   # once; writes %APPDATA%\shp-portrait-tools\launcher.json
python -m uv run --no-sync shp-portrait-tools                  # starts shell + Skin Tools and opens the window
python -m uv run --no-sync shp-portrait-tools --no-browser     # services only; open http://127.0.0.1:18760/
```

`launcher.json` defaults: models `%LOCALAPPDATA%\shp-portrait-tools\skin\models`, ports crop
18768 / dnb 18765, shell 18760, startup `{crop: false, dnb: true}`; `comfy` is rejected on Windows.
`SHP_TOOLS_DATA` relocates the data/state tree (used by the smoke test). Logs: `<data>\state\*.log`
and `desktop.log`. Requires the Edge WebView2 Runtime (`--check` reports it).

Optional shortcut recipe (documented only, NOT executed): a `.lnk` whose target is
`<worktree>\.venv\Scripts\pythonw.exe`, arguments `-m shp_portrait_tools.launcher --config
%APPDATA%\shp-portrait-tools\launcher.json`, start in `<worktree>`.

Code: `shared/filelock.py` (msvcrt lock), `shared/windows_process.py` (identity, jobs,
single-instance mutex/event), platform dispatch in `shared/owned_process.py` and
`shared/supervision.py`, Windows paths in `launcher.py`/`supervisor.py`,
`desktop/protocol.py`, `desktop/windows_controller.py`. Linux paths are behind `sys.platform` guards.

#### Verified (real run, `tests/windows_host_smoke.py`, disposable data dir/inbox, real models read-only)

Against real WebView2 (157.0.4341.46), pythonnet and pystray: shell and Skin Tools iframe load in
the pywebview window; `__shpOwnedDesktop` (top) and `__shpNativeFileDialog` (shell and iframe) are
set; the tray icon is created (`run_detached` reports ready); all four `SHP_OPEN_*` prompts
round-trip through the real `ScriptDialogOpening` handler with a fake picker (the answers match
the GTK formats); hide/show toggle the form's visibility; Quit through the real
`/api/ui/native-quit` stops the supervisor and Skin Tools with no listener left on 18760/18765
and no stray python processes. Unit tests (`tests/test_windows_host.py`): file lock, job adoption,
termination, identity-mismatch refusal, single-instance mutex/event, prompt protocol. Regression
set (`test_combine ... test_windows_service`, plus the new tests): 84 OK, skipped 9.
`test_launcher`/`test_supervisor` are Linux-oriented and still have Windows failures (6 of 21).

Findings: WebView2 raises `ScriptDialogOpening` only when `AreDefaultScriptDialogsEnabled` is
false, so the host disables them and renders alert/confirm itself with `MessageBox`; the venv
`python.exe` is a redirector that spawns the real interpreter, so ownership uses job membership
rather than `Popen.pid`; closed loopback ports take ~2 s to refuse on Windows (probe timeout 5 s).

#### Not verified (try in the GUI)

Real native pickers (Open/Folder dialogs; only the fake picker path was run), the Yes/No/Cancel
batch-source box, tray right-click menu and notifications, close-button hide, the second-launch
"show existing window" wake, Restart / Force Restart / Reload, the busy and unsent-work
confirmations, window geometry persistence, download Save-As, new-window/navigation blocking,
inbox wake-to-show, GPU inference through the host, and AI Fill (`crop`).

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
| GPU/models | Optional `gpu` extra supplies CUDA 13/cuDNN libraries; `modules/skin/dnb_gpu.py` preloads libraries; `estimate_body_scale.py` explicitly requests CUDA for Wholebody | Verify Windows DLL discovery, driver/provider compatibility and real inference separately. CPU-only full analysis is not guaranteed. Provide external weights per MODELS; never commit weights or caches. |
| Experimental pose | `modules/skin/pose_backend.py` requires Linux bubblewrap/namespaces and `bin/python` for Google Full; `scripts/google-pose-requirements.txt` hashes Linux-specific wheels. `opencv_zoo_pose.py` uses existing OpenCV DNN with a pinned two-model pair | Keep rtmlib as baseline. Do not run the Google provisioning instructions unchanged or remove isolation to enable Windows. OpenCV Zoo is a separate candidate for Windows verification, not a verified port; Linux checks covered blank/synthetic input, not portrait accuracy or speed. |
| AI Fill/ComfyUI | External ComfyUI has its own PyTorch environment/models/nodes; `comfy.py` searches `venv/bin/python` or `.venv/bin/python` and uses `/proc` listener inspection. Klein uses core FLUX.2 nodes and a local safetensors shape check for optional LoRAs | Validate against an explicitly started external ComfyUI first. Later support `Scripts/python.exe` and Windows-safe ownership/lifecycle; do not attach to or terminate arbitrary listeners. Follow method-specific MODELS requirements; Klein is not FLUX.2 dev/GGUF and does not require the custom seam node. |
| Native desktop | `desktop/controller.py` runs system Python with GTK3, WebKit2 4.1 and AppIndicator3; GLib Unix signals, GTK file choosers and tray presentation | These are the Linux desktop implementation, not a verified Windows runtime. Choose a Windows host only after browser/service validation; preserve existing UI and lifecycle contracts. |
| Launcher/install/lifetime | `launcher.py` imports `fcntl`, uses file locks and `/usr/bin/python3`; `scripts/install_desktop.py` creates shell wrappers, XDG `.desktop` files and icons. Supervisor/process ownership uses `/proc`, Linux pidfds/signals and session semantics | Replace locks, platform paths, launch/install assets and verified process identity/termination. Standalone module commands avoid launcher import but do not provide equivalent owned-shell/tray management. |
| Output/file safety | `shared/local_output.py` uses directory descriptors, `dir_fd`, `O_DIRECTORY`, `O_NOFOLLOW`, exclusive allocation, directory fsync and atomic manifest replacement | Provide Windows equivalents with no-overwrite and replacement/symlink/reparse-point safety. Test case-insensitive collisions, reserved names, Unicode/spaces, long paths, locked files and cross-volume behavior. Do not remove safety checks just to make writes succeed. |
| Inbox imports | `shared/watch_folder.py` pins directories/files and validates PNG completion/CRC; uses `/proc/self/fd`, POSIX descriptor operations. Both server `WATCH_DIRECTORY` constants currently point to fixed Linux paths | Make inbox locations portable/configurable before enabling Windows imports. Use disposable configured directories, never a developer's real inbox. Retain stable-file detection, association checks, deduplication and cleanup only after unchanged successful import. Missing inboxes currently are not auto-created. |
| Settings/data | `SKIN_DATA`, `SHPC_DATA`, `SKIN_MODELS`, XDG global config and PhotoCraft config defaults are Unix-oriented | Define Windows config/data/state defaults and migration explicitly; preserve global-vs-preset separation and environment overrides. Use portable user-local placeholders, not copied machine paths. |
| PhotoCraft | `shared/photocraft/bridge.py` uses authenticated localhost JSON-lines TCP and exchange files. `SHP_PC_*` overrides are shared; `DNB_PC_*`/`SHPC_PC_*` take precedence. One spawn path already guards `start_new_session` on win32. Optional `flatpak_peer.py` uses Linux `/proc`, pidfds and Flatpak identity | Verify a Windows PhotoCraft build exposes the same control/automation APIs, path permissions and export capabilities. That single spawn guard is not Windows support. Do not copy Linux Flatpak guard configuration or disable authentication to make a connection succeed; define the Windows trust/process-identity mechanism separately. Configure token/exchange/read-root locally; never copy or commit credentials. |
| LightCraft handoff | `editor_handoff.py` validates input paths against the read root and sends one-shot acknowledged `app.open`; `scripts/install_editor_handoff.py` writes Linux shell wrappers/XDG desktop and native LightCraft settings | Windows needs its own executable/helper discovery, launcher and permission setup. An open acknowledgement is not proof of same-file save-back. Verify that an alongside-original edit can be reopened and saved in place without moving it or granting blanket drive access. |

PNG inboxes have no origin identity. Automatic association deliberately uses the active
PhotoCraft document at processing time, checks available identity/revision/dimensions
around load, and retains files on failure. It cannot prove a same-sized active document
is the one that exported the PNG. Preserve this limitation in Windows UX/docs.

## PhotoCraft/LightCraft integration boundary

### Windows PhotoCraft connection

The installed Windows PhotoCraft 0.23.1 (build `45066c482`) exposes the same
authenticated control and scoped filesystem APIs. With disposable files, both its
headless engine and a separate GUI instance fetched a merged image and accepted an
editable Soft Light layer with a mask. This did not test Skin Tools model analysis,
the complete WebUI flow, or the user's existing open document. The test did not use
the existing editor's settings or documents.

On Windows the client requires PhotoCraft's challenge authentication: it verifies the
server knows the token before sending a proof, never sends the token over the socket,
and does not retry potentially executed mutations. This authenticates possession of
the shared secret, not an executable identity; another process with the token can
impersonate PhotoCraft. Keep the token and exchange private. Windows defaults use
`%APPDATA%\shp-photocraft-tools\dnb` with the exchange as the only read root; shared
`SHP_PC_*` and module-specific `DNB_PC_*` overrides still apply. The Windows Launch
endpoint requires an existing token file; PhotoCraft itself does not enforce a private
Windows ACL. Do not use the Linux Flatpak verification settings on Windows.

When a `photocraft` connection is saved in `launcher.json` by Windows setup, the
normal Tools launcher passes its port, token-file path, exchange and read root to
both modules. Existing explicit `SHP_PC_*` and module-specific overrides take
precedence. Get Image fetches from the controlled PhotoCraft window when connected;
when PhotoCraft is unavailable it offers the desktop's local-file picker instead.
After updating launcher code, exit Tools completely and reopen it to replace the
running supervisor and modules; a window refresh alone does not update their settings.

For shortcut launches, the optional `photocraft` object in the existing
`%APPDATA%\shp-portrait-tools\launcher.json` holds paths only:

```json
{
  "photocraft": {
    "port": 7878,
    "token_file": "C:\\Users\\YOURNAME\\AppData\\Local\\shp-photocraft-control\\control.token",
    "exchange": "C:\\Users\\YOURNAME\\AppData\\Local\\shp-photocraft-control\\exchange",
    "read_root": "C:\\Users\\YOURNAME\\AppData\\Local\\shp-photocraft-control\\exchange",
    "executable": "C:\\Program Files\\PhotoCraft\\photocraft.exe",
    "editor_config": "C:\\Users\\YOURNAME\\AppData\\Local\\shp-photocraft-control\\editor-settings"
  }
}
```

Add that object alongside the existing settings, not as a replacement. All paths
must be absolute; keep both roots limited to the exchange. Explicit `SHP_PC_*`
and module-specific overrides take precedence, as do `PHOTOCRAFT_BIN` and
`PHOTOCRAFT_CONFIG_DIR`. The shortcut must run a checkout containing this
integration; merely updating configuration does not update an older checkout.
Quit Tools from its tray and reopen it once after changing configuration or code;
an already-running supervisor retains its old environment. PhotoCraft is not
started automatically: use the **PhotoCraft** button in Skin Tools. It authenticates
and reuses a controlled desktop listener, or starts a separate controlled window.
It refuses an untrusted listener instead of starting another on an occupied port.
Open the document in that controlled window; Get Image and Send Layers do not attach
to the ordinary already-running editor. If connection fails, the button reports
the error. A running editor is never stopped by this action.

For a first isolated setup, run the following in PowerShell from this checkout. It
creates a **new** private directory, refusing to alter an existing directory or token.
The ACL grants the current Windows user access and is inherited by the token and
exchange. Check local policy and protect any backups of this directory too.

```powershell
$private = Join-Path $env:LOCALAPPDATA 'shp-photocraft-control'
if (Test-Path -LiteralPath $private) { throw 'Control directory exists; inspect it rather than overwriting it.' }
New-Item -ItemType Directory -Path $private | Out-Null
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $private -AclObject $acl
New-Item -ItemType Directory -Path (Join-Path $private 'exchange') | Out-Null
New-Item -ItemType Directory -Path (Join-Path $private 'editor-settings') | Out-Null
$bytes = [byte[]]::new(32)
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$hex = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
[System.IO.File]::WriteAllText((Join-Path $private 'control.token'), $hex)
```

The environment-based alternative below is only for manual or standalone testing;
the configured shortcut does not require these commands:

```powershell
$private = Join-Path $env:LOCALAPPDATA 'shp-photocraft-control'
$env:SHP_PC_TOKEN_FILE = Join-Path $private 'control.token'
$env:SHP_PC_EXCHANGE = Join-Path $private 'exchange'
$env:SHP_PC_READ_ROOT = $env:SHP_PC_EXCHANGE
$env:PHOTOCRAFT_CONFIG_DIR = Join-Path $private 'editor-settings'
& "$env:ProgramFiles\PhotoCraft\photocraft.exe" --control 7878 --control-token-file $env:SHP_PC_TOKEN_FILE --automation-read-root $env:SHP_PC_READ_ROOT --automation-write-root $env:SHP_PC_EXCHANGE
python -m uv run --no-sync shp-portrait-tools
```

Use the actual executable path if installed elsewhere; set `PHOTOCRAFT_BIN` to it
for the Tools Launch endpoint. If port 7878 is in use, choose a free port and set
`SHP_PC_PORT` to the same value before starting both programs. Open the intended
document **in the controlled window**, then use Get Image and Send Layers. Control
flags deliberately start a separate instance; they do not attach to an ordinary
already-open PhotoCraft window. Save the edited document from that window yourself.
Automation can read and write only the exchange, not arbitrary image folders.
If the isolated editor's Preferences > Integrations disables Allow Agent Control,
enable it there and restart that isolated instance. Never copy a token into source
control. For an isolated repeatable integration test, set `PHOTOCRAFT_TEST_CLI` and
optionally `PHOTOCRAFT_TEST_GUI` to the installed executables and run
`python -m uv run --no-sync python -m unittest discover -s tests -p test_windows_photocraft_live.py -v`.

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
2. Set `SKIN_DATA`, `SHPC_DATA` and `SKIN_MODELS` to disposable/user-local absolute
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
