# SHP PhotoCraft Tools

Two independently runnable photo tools, with shared local CSS and PhotoCraft transport,
an optional combined shell, and an owned desktop window/tray controller.

| Tool | Standalone command | Default address |
|---|---|---|
| **SHP PhotoCraft Crop** | `uv run shp-photocraft-crop` | http://127.0.0.1:8768/ |
| **SHP PhotoCraft D&B** | `uv run --extra gpu shp-photocraft-dnb` | http://127.0.0.1:8765/ |

Crop generates new image area using ComfyUI. D&B creates a 16-bit Soft Light layer with a
combined skin/color/light/structure mask. Their processing engines, UIs, settings and result
application remain separate; there is no plugin framework or combined processing server.

This private repository lives at `/home/stef/shp-photocraft-tools`. It was created beside,
not in place of, `/home/stef/shpcrop` and `/home/stef/dnb`; those source projects remain
untouched. Old Crop/D&B shortcuts were later removed at the user's request, while
PhotoCraft/OpenFill shortcuts were retained. The distinct Tools launcher uses alternate ports.

## Setup

Use Python 3.13 and [uv](https://docs.astral.sh/uv/). The root `.python-version`,
`pyproject.toml` and `uv.lock` define both modules and all packaged resources.

```sh
cd /home/stef/shp-photocraft-tools
uv sync --extra gpu
```

The `gpu` extra provides D&B's CUDA 13/cuDNN libraries and needs a compatible NVIDIA driver.
Without it, `uv sync` supports Crop, HTTP startup and CPU-only synthetic checks, not necessarily
D&B GPU inference. The optional `research` extra supplies plotting/notebook dependencies.
Use uv's root project resolution: its `onnxruntime` override prevents rtmlib's CPU distribution
from overwriting `onnxruntime-gpu`. Plain wheel installation with pip does not inherit this override.

Model weights are **not shipped or downloaded by the installer**. Provide D&B's three ONNX
files listed in [MODELS.md](MODELS.md), through `DNB_MODELS` or `<D&B data>/models/`.
Standalone UI startup and `--help` work without them; analysis reports missing files.
rtmlib may download/cache pose weights on first real analysis.

Sony autofocus-point subject selection needs ExifTool with Perl on `PATH`, or
`DNB_EXIFTOOL=/absolute/path/to/exiftool`. Otherwise the existing no-AF-point selection applies.
Crop needs an already configured ComfyUI runtime and the models/custom nodes described in
[the Crop technical notes](docs/source/crop-handoff.md): ComfyUI-GGUF,
comfyui-inpaint-nodes and `SeamColorCorrect`.

## Desktop launcher

Install the distinct **SHP PhotoCraft Tools** menu entry, desktop shortcut and icon:

```sh
uv run --no-sync python scripts/install_desktop.py \
  --models /absolute/path/to/existing/models \
  --exiftool /absolute/path/to/existing/exiftool \
  --comfy-dir /absolute/path/to/ComfyUI
```

`--exiftool` and `--comfy-dir` are optional; `--desktop-dir` overrides the XDG desktop folder.
Resources are referenced, not copied or modified. The current installation references
`/home/stef/dnb/models`, `/home/stef/dnb/tools/Image-ExifTool-13.60/exiftool` and
`/home/stef/ComfyUI`. The installer is idempotent for identical content and refuses different
existing files; review a specifically named Tools file before replacing it.

The native helper uses `/usr/bin/python3` with system GI bindings for **GTK 3, WebKit2 4.1
and AppIndicator3**. Install those OS runtimes and a working tray host first on another machine;
GNOME normally needs an AppIndicator extension. The launcher checks bindings/display before
opening a window and reports missing components. The helper never imports the project's
ML packages; the supervisor and modules run in the uv environment.

| Service | Tools address |
|---|---|
| Shell | http://127.0.0.1:18760/ |
| Embedded Crop | http://127.0.0.1:18760/crop/ |
| Embedded D&B | http://127.0.0.1:18760/dnb/ |
| Owned module backends | 127.0.0.1:18768 / 127.0.0.1:18765 |
| Dedicated ComfyUI, when configured | http://127.0.0.1:18788/ |

Use the exact `127.0.0.1` shell URL; `localhost` and LAN addresses are rejected.
Backend ports preserve the standalone defaults for legacy apps. All Tools listeners bind
to loopback. An occupied or incompatible port produces an error, not adoption or termination
of somebody else's server. Repeated launches serialize startup and reuse verified owned services.
One failed module does not prevent opening the shell or using the other module.

### Window, tray and shutdown

The launcher presents **one owned GTK/WebKit window**. Launching again shows the same window;
**Close (X) hides to the tray**, preserving both module views. The GTK program class/Wayland
app ID, desktop basename and `StartupWMClass` all match `shp-photocraft-tools`, giving one
GNOME dash application. The separate tray icon is intentional.

The tray provides **Show Tools**, **Hide Tools**, **Quit Tools**, and **Crop / D&B** status
submenus with **Start / Stop / Restart**. Busy work blocks unsafe actions. Loaded images
require confirmation before discard, and changed instances/work invalidate stale confirmations.
Stopping or restarting a module affects every window using that backend, but leaves the
other module intact. Saved settings, presets and results remain on disk.

PhotoCraft has one **Reachable / Unreachable (TCP)** status line, not an authenticated
connection claim. Equal endpoints share one probe; differing Crop/D&B ports report a
configuration mismatch. Tools never starts/stops PhotoCraft or reads its documents for status.
The separate **ComfyUI (Tools)** line reports owned startup, running/busy, stopping and errors.
Without managed ComfyUI configuration, that line is a non-owning TCP reachability indicator.

Quit protects active work and confirms loss of the window's unsent previews/view state.
It releases only the desktop's lease: other browser shell sessions keep services alive.
After the last session closes, a **15-second grace** precedes shutdown of owned services,
once work drains. Hidden native windows retain their Python heartbeat lease; iframe
switching does not affect lifetime. Browser heartbeats use 20-second long polls and
90-second leases. Crashes, lost close beacons or OS/browser suspension can expire a lease;
reopen the launcher to restart. Startup reservations allow two minutes for the first session.
`--no-browser` does not keep an unused shell alive indefinitely.

If the tray host disappears, Tools keeps/reopens an accessible window. Without a tray host,
Close offers guarded Quit. Renderer/load failures are surfaced with guarded **Reload UI**;
that resets only the webview, never restarts servers. Notifications overlay the view rather
than resizing it. Native dialogs and download Save choosers retain overwrite confirmation;
external navigation, popups and device/notification permission requests are blocked.

Normal window size is persisted atomically in `webview/window.json`; maximized/fullscreen
sizes are skipped. Without saved geometry, the GTK size is **1399x949**, including its
titlebar, for a 1399x912 web UI. The WebView minimum is **363x626**.
Position is restored on X11 only; GNOME controls placement on Wayland.

### Tools-owned ComfyUI

Installer `--comfy-dir` adds optional configuration:

```json
{
  "models": "/absolute/path/to/models",
  "ports": {"crop": 18768, "dnb": 18765},
  "comfy": {"directory": "/absolute/path/to/ComfyUI", "port": 18788}
}
```

Tools starts a **dedicated process** using that checkout's existing `venv` or `.venv` Python,
models and custom nodes. It does not install dependencies/models or change the checkout.
Port 8188 is reserved for independent use and cannot be the managed port. Any occupied
dedicated port fails startup; existing ComfyUI processes are never adopted or stopped.
Multiple runtimes can consume additional memory/VRAM.

Input/output/user/temp directories are isolated as `comfy-{input,output,user,temp}` in
Tools' state directory. A watchdog verifies the actual spawned child's Linux listener
ownership and polls its queue. Supervised Crop receives the dedicated URL even if an
inherited `SHPC_COMFY` differs. Its retained startup API routes to the supervisor, preventing
a detached duplicate; there is no ComfyUI Start button or warning block in Crop's web UI.
Generate remains gated on readiness, and model/node errors appear in the main status bar.

Quit is blocked by active queue work. Last-session shutdown stops modules, then drains/stops
**only the owned ComfyUI child**. The watchdog also handles supervisor loss. If an established
queue becomes unreadable, cleanup waits and reports the error rather than risking active work.
Standalone Crop still defaults to `http://127.0.0.1:8188`, overridden by `--comfy` / `SHPC_COMFY`.

### Installed files and alternatives

```text
~/.local/bin/shp-photocraft-tools
~/.local/share/applications/shp-photocraft-tools.desktop
~/Desktop/shp-photocraft-tools.desktop        # actual XDG desktop folder
~/.local/share/icons/hicolor/scalable/apps/shp-photocraft-tools.svg
~/.config/shp-photocraft-tools/launcher.json
```

Application/icon/config destinations honor `XDG_DATA_HOME` / `XDG_CONFIG_HOME`.
The wrapper uses this checkout's `.venv` without syncing on every launch; rerun setup after
moving the checkout. If desktop trust cannot be set, choose **Allow Launching** manually.
Config holds resource paths and ports, not credentials. Optional `shell_port` defaults to
18760 and must differ from both backend ports and the managed ComfyUI port.

```sh
~/.local/bin/shp-photocraft-tools --browser     # explicit Ungoogled Chromium app fallback
~/.local/bin/shp-photocraft-tools --no-browser  # start/reuse without opening a window
```

The fallback detects native `ungoogled-chromium` / `ungoogled-chromium-browser`, then
`io.github.ungoogled_software.ungoogled_chromium` Flatpak. Optional config
`ungoogled_chromium` selects a native executable. No silent Google Chrome/Chromium fallback.
Browser mode may open additional windows; only native mode guarantees one window.
Previous browser profiles are retained, not migrated.

## Using Crop and D&B

**Crop:** **Get Image** from PhotoCraft, or choose a file supplied through standalone `--dir`.
Choose a ratio or Free, then drag the frame/corners/side handles. Each side can crop or expand,
but generation requires new area. **Generate** creates 1-4 variations and becomes **Cancel**
while working. Result tabs select the variation sent by **Send Result**. Wheel zoom, drag pan
and double-click fit remain. Send resizes the source canvas and copies the generated RGBA layer,
retaining best-effort single-undo grouping.

The empty-image instruction is removed; loaded dimensions/margins remain. Lowercase
**turbo lora** and **seam colour correction** occupy separate right-aligned rows above
model/upscaler selection, without a right inset.

**D&B:** **Get Image** analyzes the portrait and updates synchronized previews. Detail level,
Skin mask, Structure protection, Light range, Color range and Image analysis are independent
collapsible sections. Headers toggle them without arrows; mask switches have no static "on"
text. Switches/pickers remain visible and independent when collapsed. Wheel zoom/drag pan,
double-click reset, Shift-click crop changes and Ctrl-click tab cycling remain.

**Send Result** copies a normal 16-bit RGB Soft Light layer and mask into the fetched document
in one layer-copy operation; it never resizes the canvas. Manual load/apply/save endpoints
remain available. Saved output includes the full-resolution JPEG, combined mask PNG,
unmasked 16-bit RGB layer PNG and settings JSON. Correction math and ICC behavior are unchanged.

Both modules use the same **grey-off / blue-on horizontal switches**, dark dropdowns,
compact preset controls and **Get Image / Send Result** labels. Native checkbox state,
keyboard focus, labels, disabled behavior and settings handlers remain. Centered shell tabs
keep both iframes mounted at the same size, preserving image/preview/zoom/prompt state.
Refreshing resets browser-only views; stopping a backend discards its in-memory work.

## Data and compatibility

| Resource | Default |
|---|---|
| Crop settings, presets, inbox, results, standalone ComfyUI log | `~/.local/share/shp-photocraft-tools/crop/` |
| D&B presets, inbox, results | `~/.local/share/shp-photocraft-tools/dnb/` |
| D&B models | `<D&B data>/models/` |
| Fallback PhotoCraft config/log | `~/.config/shp-photocraft-tools/{crop,dnb}/` |
| Native WebKit data/cache and geometry | `<XDG_DATA_HOME>/shp-photocraft-tools/webview/` |
| Supervisor/module/desktop/managed ComfyUI logs and lock | `<XDG_STATE_HOME>/shp-photocraft-tools/` |

XDG defaults are `~/.local/share` and `~/.local/state`. Logs include `shell.log`, `crop.log`,
`dnb.log`, `desktop.log`, `comfy.log` and optional `chromium.log`. Module defaults use the
home-directory paths above; standalone `SHPC_DATA`, `DNB_DATA`, `DNB_MODELS` and `DNB_EXIFTOOL`
override them. Supervised modules use the launcher's own module data directories.

No old settings, presets, images, credentials or caches are imported automatically.
Crop retains `{presets,last}` plus `settings.json`; D&B retains `{presets,default}`.
An intentional JSON copy to the new locations needs no schema conversion.
Do not point side-by-side checks at original data, which would share writes.

Crop retains `SHPC_COMFY_DIR`, `SHPC_LIMIT`, `SHPC_CLIP`, `SHPC_VAE`, `SHPC_LORA` and
`SHPC_PC_*`; D&B retains `DNB_PC_*`. Shared connection variables are `SHP_PC_PORT`,
`SHP_PC_TOKEN_FILE`, `SHP_PC_EXCHANGE` and `SHP_PC_READ_ROOT`. A module-specific override wins.
Without overrides, port 7878 and existing OpenFill token/exchange are used when present.
Credentials are read at connection time, never embedded or copied. Get exports/closes a merged
duplicate; Send intentionally changes the target document.
Browser keys remain `shpx-*` / `dnb-*`, scoped to each origin and browser/WebKit profile.

## Standalone and isolated runs

Use alternate ports beside the originals; these commands do not automatically start/stop
PhotoCraft, ComfyUI or existing servers:

```sh
uv run --extra gpu shp-photocraft-crop --port 18768 --dir /path/to/pictures
uv run --extra gpu shp-photocraft-dnb --port 18765 --dir /path/to/pictures
```

Do not use those backend ports while the Tools supervisor owns them. `--port 0` chooses
and prints an ephemeral free port. Compatible aliases are `shpcrop` and `shpdnb`;
module entrypoints are `python -m shp_photocraft_tools.modules.crop.server` and
`python -m shp_photocraft_tools.modules.dnb.ui_server`. They stay running independently
unless explicitly passed `--close-with-ui`.

```sh
uv run --no-sync python tests/preview.py        # two standalone synthetic module UIs
uv run --no-sync python tests/shell_preview.py  # real supervisor with synthetic children
```

Fixtures use temporary data/settings/models/token/exchange paths, free ports, fake PhotoCraft,
stub ComfyUI and synthetic detection/masks with real CPU D&B correction. No real inference,
model downloads or PhotoCraft mutation. Stop with Ctrl+C; the shell fixture also exits after
its last session closes and removes temporary files.

## Layout and verification

```text
src/shp_photocraft_tools/
  launcher.py, supervisor.py   identity-verified startup, proxy and owned module lifecycle
  comfy.py                    dedicated ComfyUI watchdog/queue lifecycle
  desktop/                    standalone system-Python GTK/WebKit tray controller
  shell/                      packaged combined-shell HTML/CSS/JS
  shared/ui/                  packaged common.css and lifecycle.js
  shared/photocraft/           transport/auth/config/session/fetch/launch
  shared/                     health identity, browser leases and private work/stop gate
  modules/crop/               graph/client, canvas-send semantics and UI/server
  modules/dnb/                engine/inference helpers, mask-send semantics and UI/server
tests/                        isolated integration/regression fixtures
desktop/, scripts/            desktop template/icon and per-user installer
docs/source/                  frozen original technical notes
```

Both modules serve identical packaged CSS at `/static/shp-ui.css`; prefixed URLs work through
the shell without HTML rewriting. Common 74px labels, 42px readouts, pane headers and switches
live there; module-specific rules stay in each HTML file. No remote CSS dependency or CDN.

```sh
uv run --no-sync python tests/run.py
SHP_DESKTOP_SMOKE=1 uv run --no-sync python tests/run.py
uv build
```

The second command additionally opens a temporary real GTK/WebKit test window on an available
graphical display/tray host; it does not reload the live Tools window. Tests cover module math,
graph/cancel/variations, settings/presets, separate Get/Send semantics, transport auth/reconnect,
ICC/masks/save, proxy/ownership/confirmation/lifetime, desktop controls and ComfyUI queue safety.
Wheel/sdist include both module UIs, shared assets, shell and native helper.

[HANDOFF.md](HANDOFF.md) records the consolidated implementation decisions and validation
boundaries; [PROVENANCE.md](PROVENANCE.md) records source revisions. Historical source notes
are not instructions for this repository. Model/dependency notices remain in [MODELS.md](MODELS.md).
