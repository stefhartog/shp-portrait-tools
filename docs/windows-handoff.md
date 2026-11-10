# Windows port handoff

## Baseline and scope

Start from `main` in `stefhartog/shp-photocraft-tools` after this handoff is merged.
The implemented Linux feature baseline is commit `96c495f`: Combine batch processing,
global settings, SkinTools/Crop watch inboxes, PhotoCraft mask placement and shell UI.
Read [README](../README.md), [HANDOFF](../HANDOFF.md) and [MODELS](../MODELS.md) first.

**Windows support is not implemented or verified.** Linux validation ran 118 tests
successfully with one optional live-desktop test skipped. That is not evidence that
Windows installation, inference, process management or PhotoCraft integration works.
Do not run the Linux desktop installer on Windows.

## Reusable code and concrete blockers

| Area | Current implementation | Windows work to investigate |
|---|---|---|
| Processing and WebUI | Python image processing plus packaged HTML/CSS/JS; separate Crop and shared SkinTools HTTP services | Start with standalone service/browser tests, not a native desktop rewrite. Imports and synthetic tests still need platform verification. |
| Python/dependencies | `pyproject.toml` requires Python 3.13; `uv.lock` pins NumPy, Pillow, OpenCV, ONNX Runtime GPU and rtmlib | Verify Windows wheels and lock resolution with `uv sync --locked`; report unavailable wheels rather than silently changing versions. Plain pip does not inherit uv's ONNX override. |
| GPU/models | Optional `gpu` extra supplies CUDA 13/cuDNN libraries; `modules/dnb/dnb_gpu.py` preloads libraries; `estimate_body_scale.py` explicitly requests CUDA for Wholebody | Verify Windows DLL discovery, driver/provider compatibility and real inference separately. CPU-only full analysis is not guaranteed. Provide external weights per MODELS; never commit weights or caches. |
| Crop/ComfyUI | External ComfyUI has its own PyTorch environment/models/nodes; `comfy.py` searches `venv/bin/python` or `.venv/bin/python` and uses `/proc` listener inspection | Validate against an explicitly started external ComfyUI first. Later support `Scripts/python.exe` and Windows-safe ownership/lifecycle; do not attach to or terminate arbitrary listeners. |
| Native desktop | `desktop/controller.py` runs system Python with GTK3, WebKit2 4.1 and AppIndicator3; GLib Unix signals, GTK file choosers and tray presentation | These are the Linux desktop implementation, not a verified Windows runtime. Choose a Windows host only after browser/service validation; preserve existing UI and lifecycle contracts. |
| Launcher/install/lifetime | `launcher.py` imports `fcntl`, uses file locks and `/usr/bin/python3`; `scripts/install_desktop.py` creates shell wrappers, XDG `.desktop` files and icons. Supervisor/process ownership uses `/proc`, Linux pidfds/signals and session semantics | Replace locks, platform paths, launch/install assets and verified process identity/termination. Standalone module commands avoid launcher import but do not provide equivalent owned-shell/tray management. |
| Output/file safety | `shared/local_output.py` uses directory descriptors, `dir_fd`, `O_DIRECTORY`, `O_NOFOLLOW`, exclusive allocation, directory fsync and atomic manifest replacement | Provide Windows equivalents with no-overwrite and replacement/symlink/reparse-point safety. Test case-insensitive collisions, reserved names, Unicode/spaces, long paths, locked files and cross-volume behavior. Do not remove safety checks just to make writes succeed. |
| Inbox imports | `shared/watch_folder.py` pins directories/files and validates PNG completion/CRC; uses `/proc/self/fd`, POSIX descriptor operations. Both server `WATCH_DIRECTORY` constants currently point to fixed Linux paths | Make inbox locations portable/configurable before enabling Windows imports. Use disposable configured directories, never a developer's real inbox. Retain stable-file detection, association checks, deduplication and cleanup only after unchanged successful import. Missing inboxes currently are not auto-created. |
| Settings/data | `DNB_DATA`, `SHPC_DATA`, `DNB_MODELS`, XDG global config and PhotoCraft config defaults are Unix-oriented | Define Windows config/data/state defaults and migration explicitly; preserve global-vs-preset separation and environment overrides. Use portable user-local placeholders, not copied machine paths. |
| PhotoCraft | `shared/photocraft/bridge.py` uses authenticated localhost JSON-lines TCP and exchange files. `SHP_PC_*` overrides are shared; `DNB_PC_*`/`SHPC_PC_*` take precedence. One spawn path already guards `start_new_session` on win32 | Verify a Windows PhotoCraft build exposes the same control/automation APIs, path permissions and export capabilities. That single guard is not Windows support. Configure token/exchange/read-root locally; never copy or commit credentials. |

PNG inboxes have no origin identity. Automatic association deliberately uses the active
PhotoCraft document at processing time, checks available identity/revision/dimensions
around load, and retains files on failure. It cannot prove a same-sized active document
is the one that exported the PNG. Preserve this limitation in Windows UX/docs.

## Browser-first validation sequence

1. Clone the same repository on Windows, check out merged `main`, and record `git rev-parse HEAD`.
   Install Git, uv and Python 3.13. Attempt `uv sync --locked`; add `--extra gpu` only
   after checking Windows library/driver availability. Capture actual failures.
2. Set `DNB_DATA`, `SHPC_DATA` and `DNB_MODELS` to disposable/user-local absolute
   Windows directories as appropriate. Prepare external models using MODELS. Import
   each standalone module and open its loopback WebUI before attempting launcher/desktop.
   The documented standalone ports are SkinTools 8765 and Crop 8768.
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
5. Validate real NVIDIA analysis and an external ComfyUI, then an explicitly configured
   Windows PhotoCraft instance. Preserve serialized control calls and non-retried final
   insertion when a reply is lost. No live-document test without deliberate user setup.
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
> Use disposable fixtures/inboxes; do not import/delete real photos, restart installed
> apps, change an existing launcher or commit secrets. Report verified Windows results
> separately from remaining assumptions. Do not claim support from Linux tests alone.
