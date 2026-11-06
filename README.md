# SHP PhotoCraft Tools

Two independently runnable photo tools, with shared local CSS and PhotoCraft transport,
an optional combined shell, and an owned desktop window/tray controller.

| Tool | Standalone command | Default address |
|---|---|---|
| **SHP PhotoCraft Crop** | `uv run shp-photocraft-crop` | http://127.0.0.1:8768/ |
| **SHP PhotoCraft D&B** | `uv run --extra gpu shp-photocraft-dnb` | http://127.0.0.1:8765/ |

Crop generates new image area using ComfyUI, with **FLUX.2** and **FLUX.1 Fill** methods.
D&B creates a 16-bit Soft Light layer with a
combined skin/color/light/structure mask. Their processing engines, UIs, settings and result
application remain separate; there is no plugin framework or combined processing server.

This private repository lives at `/home/stef/shp-photocraft-tools`. The migration originally
copied the separate Crop/D&B projects without changing them; those old local checkouts have
since been removed. They are not install prerequisites. PhotoCraft/OpenFill shortcuts were
retained. The distinct Tools launcher uses alternate ports.

### Colour themes

The **Theme** selector to the right of the shell's module tabs switches between
**Current** (the original palette) and **PhotoCraft** (the default when no explicit
choice is saved), an approximate
screenshot-inspired neutral grey palette with a darker image workspace and muted
blue selection/focus. It applies to the shell, Crop and all skin tools, including
reloaded embedded frames. Standalone tools show the same selector in their top bar,
immediately before presets. The preference is stored in this browser/profile per
origin (standalone ports have separate preferences); same-origin windows stay in
sync. Theme changes affect UI colours only, not chart data colours, mask overlays,
image pixels, processing settings or presets. There is no image/CSS filter.
Explicitly saved Current choices are retained. Selected tabs connect to their
background with a matching bottom border, not a blue selection line; keyboard focus
keeps its separate blue outline.
Enabled Get/Send actions and standalone skin-tools Load/Save use the same primary
accent styling as Generate/Apply. Primary/workflow buttons retain constant font weight, border and
padding across states; disabled controls use subdued base colours with no hover effect.
Get/Send and Crop Generate use fixed shared slots so status text does not jump when
availability changes. Preset/utility controls are not promoted; Crop Cancel stays red.
Module tab titles darken subtly while busy/starting/stopping, with no status icons
or changes to backgrounds or label width. Tooltips and accessible labels retain the full state and error
details. Top-bar completion checkmarks remain muted blue in both themes.

## Setup

### 1. Baseline environment (all modes)

Use Linux, Git, Python 3.13 and [uv](https://docs.astral.sh/uv/). Linux is required for the
supervisor's process/listener ownership checks; the native desktop also needs a graphical
session. The root `.python-version`, `pyproject.toml` and `uv.lock` define both modules
and all packaged resources. Clone the private repo using an account with access:

```sh
git clone https://github.com/stefhartog/shp-photocraft-tools.git /home/stef/shp-photocraft-tools
cd /home/stef/shp-photocraft-tools
uv python install 3.13
uv sync --locked
```

Use your own checkout path on another account/machine. Baseline dependencies include NumPy,
Pillow, OpenCV, `onnxruntime-gpu` and rtmlib even if you only use Crop; there are no per-module
dependency extras. OpenCV may need OS GL/runtime libraries (for example `libGL.so.1`).
`uv sync --locked` supports HTTP startup and synthetic CPU checks without CUDA, but is not
a promise of full CPU-only D&B analysis: its pose helper explicitly requests CUDA.

For **D&B analysis on NVIDIA**, install a driver compatible with CUDA 13 and add the
project's user-space libraries (uv does not install/update the driver):

```sh
uv sync --locked --extra gpu
```

The `gpu` extra supplies CUDA 13/cuDNN libraries for ONNX Runtime. It does not configure
ComfyUI's separate PyTorch environment or provide weights. Driver/GPU support and sufficient
VRAM must be checked on the target machine. The optional `research` extra supplies plotting/notebook dependencies.
Use uv's root project resolution: its `onnxruntime` override prevents rtmlib's CPU distribution
from overwriting `onnxruntime-gpu`. Plain wheel installation with pip does not inherit this override.

Model weights are **not shipped or downloaded by the installer**. Provide D&B's three ONNX
files listed in [MODELS.md](MODELS.md), through `DNB_MODELS` or `<D&B data>/models/`.
Standalone UI startup and `--help` work without them; analysis reports missing files.
rtmlib downloads separate YOLOX/RTMW pose weights on first analysis; see that document for
cache location, explicit prefetch and offline preparation.

Sony autofocus-point subject selection needs ExifTool with Perl on `PATH`, or
`DNB_EXIFTOOL=/absolute/path/to/exiftool`. Otherwise the existing no-AF-point selection applies.
Use an OS ExifTool package or the full distribution from [exiftool.org](https://exiftool.org/);
when using the Perl script, keep its adjacent `lib/` tree. It is not a D&B model or a required
dependency for ordinary analysis.

### 2. Processing services and PhotoCraft

**Crop generation:** prepare a separate [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
checkout with a supported PyTorch/GPU installation, its own `venv/` or `.venv/`, the custom
nodes and exact model files in [MODELS.md](MODELS.md#crop-comfyui-models-and-nodes).
The Tools environment does not install that runtime. In particular, the local
`SeamColorCorrect` node is supplied unchanged in `comfyui/custom_nodes/seam-color-correct/`;
copy it into ComfyUI as described in MODELS. It is included in source checkouts and source
releases, not the Tools wheel, and does not install itself automatically.
Do not treat frozen `docs/source/` instructions as current installation instructions.

**PhotoCraft Get/Send:** use a PhotoCraft build supporting its authenticated JSON-lines control
and automation APIs. Enable control on loopback port **7878** and use the same token file
and permitted exchange/read directories on both sides. Tools neither installs PhotoCraft
nor enables control in an already running instance. Existing
`~/.config/openfill-photocraft/control.token` and `exchange/` are reused when present;
OpenFill processing itself is not required.

For a new independent configuration, choose absolute paths and start PhotoCraft explicitly
(replace `photocraft` with your executable):

```sh
mkdir -p "$HOME/.config/shp-photocraft-tools/photocraft/exchange"
export SHP_PC_TOKEN_FILE="$HOME/.config/shp-photocraft-tools/photocraft/control.token"
export SHP_PC_EXCHANGE="$HOME/.config/shp-photocraft-tools/photocraft/exchange"
export SHP_PC_READ_ROOT="$HOME"
export SHP_PC_PORT=7878
photocraft --control "$SHP_PC_PORT" --control-token-file "$SHP_PC_TOKEN_FILE" \
  --automation-read-root "$SHP_PC_READ_ROOT" --automation-write-root "$SHP_PC_EXCHANGE"
```

Use the token produced/provisioned by your PhotoCraft build; do not invent a separate token
for Tools or commit it. Protect its file permissions. Launch Tools from an environment carrying
these variables, or configure the same module-specific defaults described below. An app-menu
launch does not inherit exports from an unrelated terminal. Get/Send requires an open document;
Get exports a merged duplicate and Send intentionally changes that document. File-based use
with standalone `--dir` does not need PhotoCraft.

### 3. Choose native desktop or browser/CLI

The standalone HTTP commands need **no GTK, WebKit or AppIndicator**. After environment/model
setup, run either and open its printed URL in a normal browser:

```sh
uv run --no-sync shp-photocraft-crop --comfy http://127.0.0.1:8188 --dir /path/to/pictures
DNB_EXIFTOOL=/absolute/path/to/exiftool uv run --no-sync shp-photocraft-dnb --dir /path/to/pictures
```

Omit `DNB_EXIFTOOL` if using ExifTool from `PATH` or not using Sony AF. Standalone commands
do not read `launcher.json`, start managed ComfyUI, or stop when a browser tab closes by default.
Start your independent ComfyUI separately for Crop, or use the supervised setup below.

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
`~/.local/share/shp-photocraft-tools/dnb/models`,
`~/.local/share/shp-photocraft-tools/dnb/tools/Image-ExifTool-13.60/exiftool` and
`/home/stef/ComfyUI`. Models and the complete local ExifTool distribution were copied and
hash-verified from the old D&B folder before its later removal. No old-folder symlinks are
needed. The installer is idempotent for identical content and refuses different
existing files; review a specifically named Tools file before replacing it.

The native helper uses `/usr/bin/python3` with system GI bindings for **GTK 3, WebKit2 4.1
and AppIndicator3** (`python3-gi`/PyGObject, including Gdk/Gio/GLib). Install those OS runtimes
and a working tray host first on another machine;
GNOME normally needs an AppIndicator extension. The launcher checks bindings/display before
opening a window and reports missing components. The helper never imports the project's
ML packages; the supervisor and modules run in the uv environment.
Package names vary by distribution: Debian-family examples are `python3-gi`,
`gir1.2-gtk-3.0`, `gir1.2-webkit2-4.1`, and `gir1.2-appindicator3-0.1` where available.
This helper imports the **AppIndicator3** namespace specifically; an Ayatana-only package
is not automatically interchangeable. Check without starting a window:

```sh
/usr/bin/python3 src/shp_photocraft_tools/desktop/controller.py \
  --url http://127.0.0.1:18760/ \
  --data-dir "${XDG_DATA_HOME:-$HOME/.local/share}/shp-photocraft-tools/webview" --check
~/.local/bin/shp-photocraft-tools
```

The installer requires all three D&B model files even if only Crop will be used. To use the
shell without installing desktop files, create `launcher.json` with the absolute paths and
ports shown below, then run `uv run --no-sync shp-photocraft-tools --config /absolute/path/to/launcher.json --no-browser`
and open http://127.0.0.1:18760/ promptly. This mode needs neither GI nor Ungoogled Chromium.

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
GNOME dash application. The separate tray icon is intentional: it uses a transparent
greyscale version of the same artwork with a white outer border, while the window and launcher keep the coloured icon.

The tray provides **Show Tools**, **Hide Tools**, **Quit Tools…**, **Restart Tools…**,
**Force Restart Tools…**, and **Crop / D&B** status submenus with
**Start / Stop / Restart** and **Load on startup**. Busy work blocks unsafe actions. Loaded images
require confirmation before discard, and changed instances/work invalidate stale confirmations.
Stopping or restarting a module affects every window using that backend, but leaves the
other module intact. Saved settings, presets and results remain on disk.
Stopped modules show an unloaded panel with Start guidance; tabs and other modules remain
usable. D&B is one service shared by Skin Mask, FS + Skintone and D&B, so its controls
affect all three tabs. Genuine child failures remain visible separately from intentional Stop.

**Load on startup** persists in `launcher.json` as
`"startup": {"crop": true, "dnb": true}`; absent entries default to enabled.
Changing it does not stop/start current work. Manual Start remains available and does not
change the preference. If Crop is disabled, the shell initially selects the skin service;
with both disabled, the shell/tray remain usable for manual Start.

PhotoCraft has one **Reachable / Unreachable (TCP)** status line, not an authenticated
connection claim. Equal endpoints share one probe; differing Crop/D&B ports report a
configuration mismatch. Tools never starts/stops PhotoCraft or reads its documents for status.
The separate **ComfyUI (Tools)** line reports owned startup, running/busy, stopping and errors.
Without managed ComfyUI configuration, that line is a non-owning TCP reachability indicator.

Native Quit protects active work, confirms loss of unsent previews/view state and waits
for an acknowledged shutdown. With other browser shell sessions, it closes only this desktop;
otherwise it immediately marks services draining, without the browser grace. Rapid relaunch
waits for the draining supervisor instead of reviving old code. Normal **Restart Tools**
requires other windows to close, shuts down orderly, then launches fresh services/controller
only after the old processes exit. It never force-kills active work.

**Force Restart Tools** requires a separate destructive confirmation: processing and unsaved
previews in all Tools windows can be lost; interrupted Send can leave exchange/scratch
remnants. It targets only verified Tools-owned module/managed-Comfy session processes using
Linux PID descriptors: per target, up to 3 seconds graceful, 3 seconds after TERM and
2 seconds after KILL, plus control/lock overhead. PhotoCraft and external Comfy are never
targets. Unverifiable ownership or incomplete cleanup blocks replacement and reports an error.
The controller's shutdown wait is bounded at 90 seconds; no duplicate is launched on timeout.

Browser final-close/refresh still has a **15-second grace** before shutdown once work drains.
Hidden native windows retain their Python heartbeat lease; iframe
switching does not affect lifetime. Browser heartbeats use 20-second long polls and
90-second leases. Crashes, lost close beacons or OS/browser suspension can expire a lease;
reopen the launcher to restart. Startup reservations allow two minutes for the first session.
`--no-browser` does not keep an unused shell alive indefinitely.

If the tray host disappears, Tools keeps/reopens an accessible window. Without a tray host,
Close offers guarded Quit. Renderer/load failures are surfaced with guarded **Reload UI**;
that resets only the webview, never restarts servers. Native notifications occupy a separate
row above the WebView, never cover the tabs, and ordinary info/errors disappear after one
second. Timestamped diagnostics remain in `desktop.log`. Reload/retry recovery and pending
shutdown notices retain their necessary guidance/actions. Confirmation dialogs and file
choosers do not auto-dismiss. Native dialogs and download Save choosers retain overwrite confirmation;
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

When Crop starts, Tools starts a **dedicated process** using that checkout's existing `venv` or `.venv` Python,
models and custom nodes. It does not install dependencies/models or change the checkout.
Port 8188 is reserved for independent use and cannot be the managed port. Any occupied
dedicated port fails startup; existing ComfyUI processes are never adopted or stopped.
Multiple runtimes can consume additional memory/VRAM.
Disabling Crop's startup preference avoids starting owned Comfy solely for the skin service.
Stopping Crop alone leaves an already-started owned Comfy running; whole-Tools shutdown
owns its cleanup.

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

Crop's **AI SETTINGS → Free Comfy memory** requests model unload and memory cleanup through
the configured backend URL, not a browser cross-origin call. It refuses during Crop generation
or any running/pending Comfy queue work. Success means **Memory release requested**:
Comfy's `/free` sets asynchronous flags, not a synchronous reclaimed-memory measurement.
The next generation reloads models; not all RAM/VRAM can be freed while the process lives.
No automatic unload, cancellation or process termination is performed.

### Installed files and alternatives

```text
~/.local/bin/shp-photocraft-tools
~/.local/share/applications/shp-photocraft-tools.desktop
~/Desktop/shp-photocraft-tools.desktop        # actual XDG desktop folder
~/.local/share/icons/hicolor/scalable/apps/shp-photocraft-tools.svg
~/.local/share/icons/hicolor/scalable/apps/shp-photocraft-tools-tray.svg
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

All send actions are labelled **Send Result**; their target follows the loaded source.
See [Local-file Open and Send Result](#local-file-open-and-send-result) below.

**Crop:** **Get Image** from PhotoCraft or a local file.
Select **method** in the settings pane:

| Method | Generation and post-processing |
|---|---|
| **FLUX.2** (existing default) | GGUF reference-latent generation, optional Turbo LoRA, neural upscaler or Lanczos-only resizing, and optional seam correction. Existing saved settings are unchanged. |
| **FLUX.1 Fill** | Native Fill conditioning; full Fill weights loaded FP8, CLIP-L + scaled FP8 T5 on CPU, 28 steps, guidance 30, 0.5 MP and feather 64 by default. Always colour matches and seam corrects, then pastes the original back at final resolution. Lanczos-only resizing by default, or an optional neural upscale model followed by exact-size Lanczos resizing. No LoRA. |

Guidance, steps, megapixels, feather and model are saved separately for Fill; switching methods
does not reinterpret FLUX.2 values. Fill's upscaler choice is also separate: **Lanczos (no model)**
or any installed model offered by ComfyUI. FLUX.2 offers the same choices, retaining its
existing neural model default. Neural upscaling
runs after colour matching and before final-size seam correction/paste-back; it adds processing
time/VRAM and can change generated detail. Only selecting a model requires upscale nodes/weights.
The LoRA and seam toggle appear only for FLUX.2. Fill always uses the fully corrected pathway.
Presets save both sets and the
selected text-encoding device: **CPU** (default, lower VRAM) or **Default (GPU / auto)**,
which lets ComfyUI manage GPU placement/offloading. Fitting depends on available memory;
this is not a guarantee that both encoders and diffusion weights stay on the GPU.
Presets also save the
selected method; older settings/presets without a method load as FLUX.2. Factory settings
reset both sets and select FLUX.2. Only the selected method's nodes/models gate Generate;
see [MODELS.md](MODELS.md#crop-comfyui-models-and-nodes) for method-specific setup.

Crop constructs its API graph in Python; no saved workflow JSON must be installed or opened
in ComfyUI. Fill is based on the successful fully corrected comparison-workflow branch,
not its raw/A/B outputs, and does not use Nunchaku or Turbo/Hyper LoRAs.

Choose a ratio or Free, then drag the frame/corners/side handles. Each side can crop or expand,
but generation requires new area. Each positive margin is capped at **999 original-image
pixels per side**, independently, for both methods; negative crop amounts are not capped.
Ratio constraints may shrink/crop the frame to stay within the cap. Invalid direct API geometry
is rejected, not merely clamped in the displayed counters; `SHPC_LIMIT` can lower this cap.
**Generate**, in the CROP panel, uses adjacent **1 / 2 / 3** variation buttons and becomes **Cancel**
while working. Each completed variation appears immediately in a result tab while the rest
continue generating; you can inspect completed results without interrupting the batch.
New results do not replace the result you are viewing. Send stays disabled until generation
stops. Cancel or a later variation's failure preserves completed results for selection/Send.
Result tabs select the variation sent by **Send Result**. Wheel zoom, drag pan
and double-click fit remain. Send resizes the source canvas and copies the generated RGBA layer,
retaining best-effort single-undo grouping.

The variation slider is removed. A saved legacy count of 4 is retained and explicitly shown
until another count is chosen, not silently rewritten or mislabelled as 1/2/3.
CROP stays open; **AI SETTINGS** is default-open and collapses by its full keyboard-accessible
header without an arrow. AI dropdowns precede sliders. The live crop readout sits between
ratios and Generate: original resolution / arrow / new resolution, then spatially arranged
top/left/right/bottom signed shifts without visible direction labels or `px` suffixes.
Accessible direction/unit descriptions remain. An unloaded image shows zero placeholders.
Lowercase
**turbo lora** and **seam colour correction** occupy separate right-aligned rows above
model/upscaler selection, without a right inset.

**D&B:** **Get Image** analyzes the portrait and updates synchronized previews.
**tau** sits alongside **edge**, in normal numeric direction (0.01–0.4, step 0.01; default 1).
Lower tau more strongly suppresses corrections of large local log-luminance deviations;
higher tau permits more correction. Edge instead controls guided-filter smoothing: lower
values preserve weaker boundaries. Tau is included in existing presets; loading a positive
legacy value outside the slider range preserves its actual value in the readout and requests;
the handle stays at the nearest endpoint until moved. Slider bounds remain 0.01–0.4.

Detail level, Skin mask, Structure protection, Light range, Color range and Image analysis are independent
collapsible sections. Headers toggle them without arrows; mask switches have no static "on"
text. Switches/pickers remain visible and independent when collapsed. Wheel zoom/drag pan,
double-click reset, Shift-click crop changes and Ctrl-click tab cycling remain.

**Send Result** copies a normal 16-bit RGB Soft Light layer and mask into the fetched document
in one layer-copy operation; it never resizes the canvas. Send tags its scratch RGB layer
with the exact ICC profile captured by **Get**, without
converting the correction samples. This prevents cross-document colour conversion from moving
neutral gray or changing correction strength in wide-gamut/linear documents. The mask remains
separate and untagged; the destination's profile is never changed. Send fails visibly if the
profile cannot be obtained. **Get again after changing, replacing or converting the
destination document.** Send checks available document name/size information but assumes
the profile is unchanged; PhotoCraft's current metadata cannot verify exact ICC identity.
This avoids another full merged duplicate/export merely to read ICC on normal Send.
Calls without a captured ICC retain the existing destination-export fallback for D&B and
Skin Mask; FS + Skintone requires a captured profile rather than inventing one.
Manual load/apply/save endpoints
remain available. Saved output includes the full-resolution JPEG, combined mask PNG,
unmasked 16-bit RGB layer PNG and settings JSON. Calculation, preview and manual-save colour
handling are unchanged.

The collapsible **Log** subsection inside Image analysis on Skin Mask shows current
Load/Get/Send progress, timings and diagnostics. There is no save/download button.
The log is in-memory only, cleared on page reload; older entries are bounded. Successful
processing finishes with **Finished processing in X seconds.** in the top bar;
errors remain explicit. This measures elapsed wall-clock time, not summed stages:
Load/Get through initial preview image loading, recalculation through preview image
loading, and Send through scratch cleanup. It does not measure screen paint.
**Image analysis** is visible only on **Skin Mask** and summarizes the known file,
dimensions/colour handling, detection confidence, mask availability, S, Hue midpoint,
and light/color midpoints. It updates from shared state when settings change, without
triggering analysis or resetting settings on tab switches.

Skin-mode previews reuse a bounded in-memory cache (up to 192 MiB) keyed by loaded image,
region/scale, shared mask and effective per-mode settings. New loads and landmark changes
invalidate it; mismatched settings recompute. Fast mode switches keep the previous preview
mounted and suppress busy flashing for 150 ms, while still blocking conflicting edits;
slower work shows normal progress and failures remain explicit. Cache restoration is labelled
as such, not reported as fresh processing. Full-resolution Send still runs its own pipeline.

### Local-file Open and Send Result

When PhotoCraft is disconnected, **Get Image** in the owned Tools desktop opens a native
file chooser and runs the normal load/analysis path. Cancel preserves the previous image,
crop and results. A standalone browser cannot expose a trustworthy source-folder path through
an upload: use the existing path/list controls instead (Crop `--dir`, skin-tools Image analysis).
No download to another folder is disguised as a next-to-source save.

Provenance is retained: a local source always saves locally even if PhotoCraft connects later;
a PhotoCraft source requires reconnection rather than saving beside its internal exchange file.
Local **Send Result** writes one full-resolution flattened output beside the source, with the
next unused number, never overwriting an existing file or emitting layers/settings sidecars:

| Tool | Example output | Format |
|---|---|---|
| Crop | `portrait_shpcrop_001.png` | Selected generated variation only; RGB8 PNG, existing sRGB conversion, no new ICC embedding guarantee. |
| D&B | `portrait_shpdnb_001.png` | 16-bit/channel RGB + opaque alpha PNG, processing ICC or sRGB. |
| Skin Mask | `portrait_shpskinmask_001.png` | 8-bit grayscale PNG. |
| FS + Skintone | `portrait_shpfs_001.png` | 16-bit/channel RGB + opaque alpha PNG, source ICC or sRGB. |

Exclusive creation, symlink-safe names and load-time parent-directory identity checks protect
allocation; permission/disk errors are explicit and failed partial writes are cleaned up.
Existing standalone Save and PhotoCraft layer/mask application remain separate.

### Progress and diagnostics

Load/Get's temporary log records:
**Image loaded** includes decoding/profile preparation and, for Get, PhotoCraft's
merged export/transfer; **Image analyzed** measures detection/S estimation;
**Mask generated** measures shared mask generation plus initial skin-colour statistics.
Without a usable detection/scale it explicitly reports that no mask was generated.
Hue Center initialization and initial preview computation are timed separately
(request round trips); **Preview images loaded** waits for both visible pane image
load events. **Ready** is elapsed browser time from Load/Get through those events,
not a claim that the screen has been painted. The summary survives automatic/manual
Apply until the next Load/Get. During Load/Get, lightweight polling shows the actual
active stage and each completed stage's measured duration. Send likewise reports mask
preparation, Color/Detail separation, Blur, encoding/import, native Hue/mask assembly,
final insertion and scratch cleanup (or D&B calculation/layer blur where applicable).
No percentage is estimated. Steps shorter than the polling interval may appear already
completed. Durations measure backend work/command completion, not PhotoCraft repaint.
Send's timing summary remains visible after completion or failure.

### Skin tools inside D&B

The **Skin tool** selector switches between **D&B layers** (the existing default),
**Skin mask**, and **FS + Skintone**, without fetching the picture again.
All three use the same combined skin × picked color × light range × structure-protection
mask. D&B strength, edge, tau and detail bands are correction controls only: they do not
change the standalone mask or frequency split. Mask controls and the subject scale remain
shared; edit Skin mask/feather, Structure protection, Light range and Color range in
**Skin Mask mode**. Those sections are hidden in the other skin tools to reduce
clutter, but their values and on/off switches still apply in all three. Switching modes does
not reset the mask; presets still save the entire tool bundle. Image analysis/S and mask
preview tabs remain available across modes. The combined tool keeps `frequency` settings
for separation/Blur and `skintone` settings for hue in the shared preset bundle.
Old `frequency` and `skintone` modes both open **FS + Skintone**; presets without a mode
still select D&B. Legacy saturation values remain stored but are ignored, including on
explicit preset Save; loading never rewrites preset files.

**Adjust scale landmarks** in Image analysis offers a manual fallback for uncertain S.
Select a subject, place or adjust eye centres/body joints on Original, and exclude
occluded or foreshortened segments. Review the proposed scale and uncertainty, then Apply;
Cancel leaves S unchanged. Edits belong to the loaded image, not presets, and clear on
the next Get/load. This adjusts scale only: it does not rebuild segmentation or retarget
the skin mask/crop. If automatic analysis produced no mask, manual landmarks do not
create one. See [landmark controls](docs/source/dnb-readme.md#adjusting-scale-landmarks).

The preset star indicates edits to shared settings, including settings hidden in the current
mode. Switching skin tabs or recalculating pixel radii from S does not mark a preset unsaved;
Save clears the star across all three skin tabs. Presets still store and restore the chosen mode.
Option tooltips appear only over their first-column labels, not sliders or value readouts.

**D&B → Layer blur** smooths the generated gray Soft Light layer before Result, Layer,
Layer+mask, Save and Send. It leaves the separate combined mask unchanged.
The independent slider is **0–20 px at S181**, step0.1, default0 (off):
full-image Gaussian sigma is `Layer blur × S / 181`, without rounding the fractional S
or sigma. Preview sigma scales with its reduced resolution; Save/Send use full resolution.
The label tooltip shows current pixels. It does not use or change FS Radius/Blur.
Legacy presets default to0 without rewriting files; zero preserves the existing D&B
output exactly. Slider edits dirty the preset, while S-derived pixel changes do not.

**Skin mask → Send Result** to PhotoCraft adds a reusable grayscale RGB layer, not a mask on the selected
PhotoCraft layer. Its numeric gray values match the displayed combined mask.
With every mask component disabled the output is white (the whole image).

**FS + Skintone** combines frequency separation, skin-aware Blur and soft hue limiting
in one tab. It separates the unmasked image into **Color** and **Detail**, adds skin-masked
Blur over Color, applies skin-masked Hue correction to that low-frequency base, then restores
unmasked Detail with Linear Light. The Hue LUT comes from your range settings, not learned
from the image. **There is no saturation correction control, graph or Saturation LUT.**
Hue changes can change perceived saturation/brightness, but the analytic hue transform
preserves HSV saturation/value; interpolation and final detail clipping can deviate slightly.
**Hue Center** slides from **10–50°**, with30° at its midpoint,
and **Range ±** slides from **1–8°**, defining a circular allowed hue arc.
Each successful **Load/Get** initializes Center once from uncorrected low-frequency Color
under the current combined skin mask: a mask-weighted circular hue mean, excluding gray/
black samples. It uses the new image's estimated S and current separation method/Radius,
on a whole-image preview capped at1400px. Red wrap is handled circularly; estimates outside
10–50° keep their actual readout/value, with the thumb at an endpoint, not silently clamped.
Manual Center edits, Apply, mask edits and tab switches do not re-estimate. Loading a preset
intentionally restores its saved Center; the next image Load/Get estimates anew without
changing saved files or Hue Range/Softness/Amount. Missing mask/scale, no coloured masked
samples or ambiguous circular hues leave the prior Center intact with an explicit note
to set it manually. HDR/transparent sources retain their normal combined-mode error.
If the page reports that `/api/hue-center` returned HTTP404, it has newer UI code than
the running backend: restart Tools/D&B when ready and Get again; page refresh alone
cannot activate a new Python endpoint.
Legacy/API zero Hue Range targets Center (Amount, neutral protection and LUT approximation still apply).
The hue chart uses an **absolute10–50° vertical axis**; brightness
still runs horizontally from dark to light. Triangles mark off-window measured percentiles,
without clamping the distribution data or changing the correction.
Legacy/default values outside these narrower slider windows retain their actual readout
and processing value until edited, with the thumb at the nearest endpoint. Presets and API
keep their wider compatible numeric domain; files are not rewritten until explicit Save.
Defaults remain hue30° ±30°, **Softness**5° and **Amount**100% for preset compatibility.
These are allowed limits, not a tint: skin already inside the inner core may look unchanged
at defaults. The status/analysis text reports visible correction (over one8-bit RGB level)
among sampled masked preview pixels, or explains zero Amount/empty mask/no visible change.
Narrowing the limits requests stronger hue correction. **Hue Amount0 leaves Blur active**;
zero Blur radius plus neutral Hue settings (or an empty combined mask) reconstructs the
original within raster rounding, never an intentionally empty Result pane. Image delivery uses the encoded
PNG/JPEG's actual MIME type, and browser image-load failures appear as explicit errors.
Softness controls exponential shoulders *inside* the boundaries: colours in the inner core
stay unchanged, while boundary/outlier colours compress toward the bounds. Zero softness
gives hard analytic limits; partial Amount intentionally permits weaker correction.
Neutral/near-neutral RGB is protected, including LUT cells touching the gray diagonal,
so shadows and low-chroma pixels are not forced into orange. HSV value is retained by the
analytic transforms; finite RGB LUT interpolation can introduce small deviations.
Hue is a circular quantity: extreme hues opposite Center sit at its wrap seam, not on a
linear spectrum. This is not an automatic skin-colour target or a perceptual colour model.

The hue chart shows **uncorrected Color versus Color + masked Blur + masked Hue**, not
the original detailed image or final recomposite: shared-mask-weighted 10/50/90th percentiles
across 16 original-Color brightness bins, with median colour strips from dark to light. Hue is shown as
absolute hue angle. Brightness means **HSV value of native
document RGB**, not physical luminance, and differs across ICC profiles. The chart describes
the current reduced preview/view, samples at most approximately 100,000 pixels, omits neutral
samples, and explicitly reports when none remain. Median colour strips use measured
HSV statistics, not an automatically calculated saturation target.
The chart does not rerun detection or use
different correction rules for shadows/midtones/highlights. Shared masking still uses
the existing analysis colour space. Adjust that mask in **Skin Mask**.

**Send Result** to PhotoCraft creates one unmasked **Normal SHP FS + Skintone** folder, top to bottom:
**Detail** (Linear Light, unmasked), **Hue** (Normal native adjustment, combined skin mask),
**Blur** (Normal, combined skin mask), **Color** (Normal, unmasked).
Hue is **one editable native Color Lookup adjustment**, with its mask attached directly
and no intermediate subgroup, even at
zero Hue Amount: Normal, trilinear interpolation, dither off. It is not a
raster layer or PhotoCraft Hue/Saturation sector slider. Edit the exact
range controls in SHP Tools and resend; PhotoCraft retains the embedded Hue LUT as an editable
adjustment (including replacement, visibility and opacity), even after saving/reopening
`.pcraft`, with no external LUT file dependency. Hue affects Color + masked Blur
inside the outer Normal folder, before Detail.
Blur's mask feathers Blur once; the Hue adjustment's own mask feathers Hue once.
Send transfers one mask file/source document and reuses it for both mask attachments;
PhotoCraft stores a separate editable mask on each layer.
The folder, Detail and Color are never masked. Send stages the group off-document and copies it in one
target undo step; stale size, missing captured ICC or unsupported native adjustments fail explicitly.

Both preview and Send sample the same **33³ RGB Hue LUT** (six-decimal samples, sized to fit
PhotoCraft's 1 MiB control request). This approximates the continuous limiter, **not an
exact guarantee that every output hue is inside its bounds**, especially at
wraps, narrow ranges, near-neutral protection, partial Amount or feathered mask boundaries.
Color/Blur/Detail use the same tagged RGB16 samples in preview and Send. Empty-mask
output reconstructs the original within raster rounding; genuine correction leaves
high-frequency Detail unchanged, although the final composite can clip at black/white.
Preview uses a reduced view and proportionally scaled Radius, so fine-detail filtering
can differ from full-resolution Send. Eight-profile × 8/16/32-bit native comparisons
cover the nested stack, partial masks and numeric-space ICC handling; 8-bit layer
quantization can differ from the RGB16 preview by about three8-bit levels.
FS + Skintone requires an opaque, normalized source. Manual **Save** writes
`Hue.cube`, tagged RGB16 Color/Blur/Detail/result, `skinmask.png` and settings with the layer recipe.
Older presets receive Skintone defaults in memory without rewriting files until Save.
The loader checks the original numeric range: 8/16-bit integers normalize by their own
maximum; floating-point samples are already numeric RGB and are not divided by255.
Genuine extended-range sources receive a Skintone-specific explanation at load/entry and
cannot be processed, saved or sent by Skintone; other modes remain available. No automatic
HDR clipping or tone mapping is applied to source pixels. On validated SDR sources only,
the reduced Skintone preview repairs float32 area-resize endpoint roundoff (for example
white becoming1.0000001); this is not interpreted as HDR and leaves original samples intact.

The **Frequency separation** section has Gaussian or Median separation, controlled by
**Radius** (a calibration in px at S = 181 px / 63 mm, default **6**). There are no direct pixel sliders.
On each new image/Get, or an edit to S or calibration, the radius becomes
`calibration × S / 181`, rounded to whole pixels and limited to 1–100 px (current values are shown in
the Radius/Blur label tooltips). Thus S181 → 6 px, S362 → 12 px and S90.5 → 3 px.
**Blur** is 100–500% (step 1%, default 200%): actual Blur Gaussian sigma is
the **rounded/clamped separation radius × percentage / 100**, not a multiple of calibration.
S181/calibration6 gives radius6/Blur12; S362 gives radius12/Blur24 at 200%.
At radius100 and 500%, Blur is 500 px; there is no hidden 100 px Blur cap.
Calibration (0.5–30 px in 0.1 steps) and percentage are saved independently. On preset
load both radii are recalculated using the current S, never overridden by stored pixels.
Legacy presets without a percentage derive it from `100 × blur_radius / radius`, limited
to 100–500%; missing calibration defaults to 6. This migration is in memory only until
saved. Legacy API requests without `blur_percent` retain their explicit pixel radii.
No new detection runs: the existing full-resolution S is reused even when loading in D&B.
If no valid S was estimated, enter it manually in Image analysis; no scale is guessed.
For Gaussian, radius means sigma; for Median it is the square neighborhood's radius,
rounded to the nearest pixel after preview scaling. These are not D&B's millimeter bands.
Preview radii scale with the preview resolution; Send and Save calculate at full resolution.
Color uses ordinary full-image Gaussian or Median filtering, completely independent of
the skin mask. Median can be slower on large pictures/radii and OpenCV internally quantizes
floating-point median values; Detail is derived from the actual resulting Color.

Color + Detail alone reconstructs the full fetched original within RGB16 rounding tolerance.
Blur's extra Gaussian filter divides skin-weighted Color by
the blurred mask, excluding non-skin samples before filtering without introducing black
edges. Outside support it keeps Color unchanged. The same combined mask then feathers
Blur as its actual layer mask, applying opacity once, not baking it into the pixels.
This cannot undo color mixing already present in the ordinary full-image Color split.
Blur is active immediately, smoothing skin tones under the unmasked Detail.
The right pane offers **Color / Detail** in FS + Skintone, **Layer / Layer + mask**
in D&B, and **Mask / Analysis** in Skin Mask. Hidden or obsolete remembered choices
fall back to the first available preview. This is a UI change only: existing backend
preview generation/encoding remains unchanged, so it does not speed up processing.
Masks, separation and Blur still contribute to Result and full-resolution output.
Skin Mask's left pane offers **Original** only; its mask remains on the right.
**Result** shows the actual sent stack,
including masked Blur, masked Hue and restored Detail. There is no initial black paint-in mask.

Separation stays in the fetched image's native numeric color space, even when analysis
uses an sRGB conversion. All RGB16 scratch layers carry Get's exact captured destination ICC,
without conversion of their values. A changed destination size/profile requires another
Get. The combined workflow requires an **opaque** merged source; transparent
images fail explicitly rather than silently losing transparency. No new models or
dependencies are required. Manual Save writes `skinmask.png` and settings, plus RGB16
`color.png`, `detail.png`, `blur.png`, `result.png` and `Hue.cube`; settings include the layer recipe.

Skin-tools Send releases temporary Blur, mask and Detail source documents as soon as
their data is copied into the assembly. FS + Skintone keeps at most the assembly plus
one temporary source open, then inserts the completed folder once. Native RGB16 layers
are prepared directly, without retaining three additional float RGB export copies;
PNG tagging also avoids redundant full-buffer copies. Precision, RGB channels, masks,
blend modes and the final one-step target insertion are unchanged. Full-resolution
processing, PNG encoding/import and the Color/Blur/Detail assembly itself still cost memory;
Get still exports the merged image needed for analysis. No reduced-precision path is used.
Get/analysis still has the same model/GPU requirements as D&B.

Both modules use the same **grey-off / blue-on horizontal switches**, dark dropdowns,
compact preset controls and **Get Image / Send Result** actions. The Send label is constant
across skin-tool modes; tooltips/status describe the target. Native checkbox state,
keyboard focus, labels, disabled behavior and settings handlers remain. The Tools top tabs
are **Skin Mask · FS + Skintone · D&B · Crop** (Crop is initial unless disabled on startup).
All three skin tabs reuse one
D&B iframe/backend and the same fetched image, mask, settings and PhotoCraft identity.
No view is reloaded when switching; presets synchronize the selected skin tab.
Left/right arrow keys cycle all four tabs in that order. The internal Skin tool selector is hidden only
after a Tools handshake; standalone D&B keeps its selector and needs no shell.
The two backend iframes stay mounted at the same size, preserving image/zoom/prompt state.
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
Restart-helper output is `webview/restart.log` in the native data directory.

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

Use alternate ports beside other instances; these commands do not automatically start/stop
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
comfyui/custom_nodes/         separately installed SeamColorCorrect node source
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
When `photocraft-cli` is installed, `tests/test_dnb_transfer.py` also launches an isolated stdio
engine (never the desktop session). It reproduces the old neutral-gray shift and checks
neutral/nonneutral corrections, exact masks and one-step copy across RGB profiles and bit depths.
Run it alone with `uv run --no-sync python -m unittest discover -s tests -p test_dnb_transfer.py -v`.
Wheel/sdist include both module UIs, shared assets, shell and native helper.

[HANDOFF.md](HANDOFF.md) records the consolidated implementation decisions and validation
boundaries; [PROVENANCE.md](PROVENANCE.md) records source revisions. Historical source notes
are not instructions for this repository. Model/dependency notices remain in [MODELS.md](MODELS.md).
