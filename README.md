# SHP Portrait Tools

Two independently runnable modules in one Python monorepo:
**Skin Tools** (Skin Mask / FS & Tone / Dodge & Burn / Combine) and **AI Fill**
(Crop & Expand / Patch Fill) services. They share local packaged UI
resources and PhotoCraft transport, not incompatible canvas/layer application logic.
An optional shell and owned GTK/WebKit desktop provide one window with retained views.
The tab strip uses matching subtle neutral dividers between Combine and Crop & Expand,
and before Settings & Info. Module boundaries disappear when their
tabs are hidden; no module-specific tab hues are used.
The Tools WebUI starts on Skin Mask when Skin Tools is loaded, without applying
Dodge & Burn first. If Skin Tools is unloaded, it falls back to Crop & Expand or
Settings & Info. Later service status updates do not override the selected tab.

| Module | Command | Standalone default |
|---|---|---|
| AI Fill | `uv run shp-photocraft-crop` | http://127.0.0.1:8768/ |
| Skin Tools | `uv run --extra gpu shp-photocraft-dnb` | http://127.0.0.1:8765/ |

Private repository: `stefhartog/shp-photocraft-tools`, local checkout
`/home/stef/shp-photocraft-tools`. Original projects are provenance, not installation
dependencies. See [HANDOFF.md](HANDOFF.md) for the current implementation/limitations,
[PROVENANCE.md](PROVENANCE.md) for source revisions and [MODELS.md](MODELS.md) for
external weights/nodes. Frozen `docs/source/` notes are historical.
For a future Windows port, see [Windows handoff](docs/windows-handoff.md);
the current application remains Linux-only.

The public product name is **SHP Portrait Tools**. Internal IDs remain `dnb` (Skin
Tools) and `crop` (AI Fill). Existing `shp-photocraft-*` commands, routes, config/data
locations, watch folders, presets and desktop application identity are intentionally
unchanged; no migration or reinstall is required. PhotoCraft still names the actual
editor integration, and `SkinTools` remains the exported layer-folder name.
To refresh an existing shortcut's display name without touching its launcher,
configuration or custom fields, run:

```sh
uv run --no-sync python scripts/install_desktop.py --update-display-only
```

Only recognized old/new Tools display fields are updated; custom names are refused
for review. Restart Tools when ready to refresh the running window/tray titles.

## Setup

### Python, GPU and models

Use Linux, Git, Python **3.13** and [uv](https://docs.astral.sh/uv/).
Linux is required for process/listener ownership; native mode additionally needs a
graphical session and tray host.

```sh
git clone https://github.com/stefhartog/shp-photocraft-tools.git /home/stef/shp-photocraft-tools
cd /home/stef/shp-photocraft-tools
uv python install 3.13
uv sync --locked
# For NVIDIA analysis, after installing a compatible driver:
uv sync --locked --extra gpu
```

Use your own checkout path elsewhere. The root manifest/lockfile installs NumPy, Pillow,
OpenCV, ONNX Runtime GPU and rtmlib for both modules. `gpu` adds CUDA 13/cuDNN user-space
libraries, not a driver or ComfyUI's separate PyTorch environment. `research` adds optional
notebook/plotting dependencies. uv's ONNX override avoids the CPU package overwriting
the GPU distribution; plain pip installation does not inherit this resolution.
HTTP startup/synthetic tests need no inference; full CPU-only skin analysis is not
guaranteed because Wholebody explicitly requests CUDA.

Weights are **not bundled**. Provide the three ONNX files in [MODELS.md](MODELS.md)
under `DNB_MODELS` or `<D&B data>/models/`. rtmlib downloads separate pose weights on
first analysis; MODELS includes offline preparation/cache guidance. No startup/installer
model download is implied. ExifTool plus Perl is optional for Sony autofocus-point
selection: use `PATH` or `DNB_EXIFTOOL=/absolute/path/to/exiftool`, keeping its adjacent
`lib/` distribution intact.

Crop needs an external [ComfyUI](https://github.com/comfyanonymous/ComfyUI) checkout
with its own `venv/` or `.venv/`, supported GPU/PyTorch, models and nodes.
[MODELS.md](MODELS.md#crop-comfyui-models-and-nodes) lists FLUX.2/FLUX.1 Fill requirements.
The included `comfyui/custom_nodes/seam-color-correct/` source must be deliberately copied
to ComfyUI; it is in the source release, not the Tools wheel, and does not install itself.
Tools constructs its graphs in Python; no workflow JSON installation is required.

### PhotoCraft connection

Use a PhotoCraft build with authenticated control/automation APIs, an open document,
matching token and permitted read/exchange directories. Tools does not install PhotoCraft
or enable control in an already-running instance. Existing OpenFill token/exchange paths
are reused when present; OpenFill processing is not required.

For a separate configuration, replace `photocraft` with your executable:

The command below is a direct automation launch with per-process access roots. It
does not participate in normal PhotoCraft single-instance file forwarding and should
not be used as LightCraft's external-editor target; see the combined integration below.

```sh
mkdir -p "$HOME/.config/shp-photocraft-tools/photocraft/exchange"
export SHP_PC_TOKEN_FILE="$HOME/.config/shp-photocraft-tools/photocraft/control.token"
export SHP_PC_EXCHANGE="$HOME/.config/shp-photocraft-tools/photocraft/exchange"
export SHP_PC_READ_ROOT="$HOME"
export SHP_PC_PORT=7878
photocraft --control "$SHP_PC_PORT" --control-token-file "$SHP_PC_TOKEN_FILE" \
  --automation-read-root "$SHP_PC_READ_ROOT" --automation-write-root "$SHP_PC_EXCHANGE"
```

Use the token provisioned by PhotoCraft, protect permissions and never commit it.
Launch Tools from this environment; an app-menu launch does not inherit an unrelated
terminal's exports. Module `SHPC_PC_*` / `DNB_PC_*` overrides beat shared `SHP_PC_*`.
Get exports a merged duplicate; sending intentionally changes the fetched document.
Standalone file processing does not require PhotoCraft.

### Standalone or desktop

Standalone commands need no GTK/WebKit/AppIndicator:

```sh
uv run --no-sync shp-photocraft-crop --dir /path/to/pictures --comfy http://127.0.0.1:8188
uv run --no-sync shp-photocraft-dnb --dir /path/to/pictures
```

Aliases `shpcrop` and `shpdnb` remain compatible. These commands do not read launcher
configuration or manage ComfyUI. They run until stopped unless `--close-with-ui` is supplied.

Native mode uses `/usr/bin/python3` and system GI **GTK3 / WebKit2-4.1 / AppIndicator3**.
Install those bindings and a tray host (GNOME generally needs an AppIndicator extension).
Debian-family examples: `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-webkit2-4.1`,
`gir1.2-appindicator3-0.1`. An Ayatana-only namespace is not automatically interchangeable.

Install the distinct Tools launcher/icon/config only when desired:

```sh
uv run --no-sync python scripts/install_desktop.py \
  --models /absolute/path/to/existing/models \
  --exiftool /absolute/path/to/existing/exiftool \
  --comfy-dir /absolute/path/to/ComfyUI
```

ExifTool/Comfy arguments are optional; the installer requires all three D&B models.
It references resources without altering them, is idempotent for identical assets and
refuses differing existing Tools files. `--desktop-dir` overrides the XDG desktop folder.
No existing PhotoCraft/OpenFill shortcut is removed. The installed wrapper uses this
checkout's `.venv` without syncing every launch; rerun setup after moving the checkout.

```text
~/.local/bin/shp-photocraft-tools
~/.local/share/applications/shp-photocraft-tools.desktop
~/Desktop/shp-photocraft-tools.desktop             # actual XDG desktop folder
~/.local/share/icons/hicolor/scalable/apps/shp-photocraft-tools.svg
~/.local/share/icons/hicolor/scalable/apps/shp-photocraft-tools-tray.svg
~/.config/shp-photocraft-tools/launcher.json
```

App/icon/config paths honor XDG variables; desktop trust may require Allow Launching.
Bindings can be checked without opening a window:

```sh
/usr/bin/python3 src/shp_photocraft_tools/desktop/controller.py \
  --url http://127.0.0.1:18760/ \
  --data-dir "${XDG_DATA_HOME:-$HOME/.local/share}/shp-photocraft-tools/webview" --check
~/.local/bin/shp-photocraft-tools
```

Alternatively create a launcher config with absolute resources:

```json
{
  "models": "/absolute/path/to/models",
  "ports": {"crop": 18768, "dnb": 18765},
  "comfy": {"directory": "/absolute/path/to/ComfyUI", "port": 18788}
}
```

`comfy` is optional; `shell_port` defaults to 18760 and must be distinct.
`startup` maps `crop`/`dnb` to booleans (absent entries default true).

```sh
uv run --no-sync shp-photocraft-tools --config /absolute/path/to/launcher.json --no-browser
# Open http://127.0.0.1:18760/ promptly; no GI required for this mode.
~/.local/bin/shp-photocraft-tools --browser  # explicit Ungoogled Chromium app fallback
```

No silent Chrome fallback: an explicitly supported Ungoogled Chromium installation is required.
Optional `ungoogled_chromium` config selects a native executable. Browser mode can open
multiple windows; only native mode guarantees one. Use exact loopback `127.0.0.1`,
not localhost/LAN addresses.

| Supervised endpoint | Address |
|---|---|
| Shell | http://127.0.0.1:18760/ |
| Crop / shared skin service | `/crop/` / `/dnb/` at the shell |
| Owned backends | 18768 / 18765 |
| Optional dedicated Comfy | 18788 |

## Window, services and safe shutdown

PhotoCraft tray reachability is TCP-only, not proof of authenticated document/file
access. Get reports a rejected PhotoCraft token instead of silently opening the
local-image chooser; genuinely disconnected local-open behavior is unchanged.
PhotoCraft's GUI control port alone does not configure the shared token file or
automation read/write roots required by Tools. In PhotoCraft 0.22.0, both explicit
`--control` and automation-root flags opt out of normal single-instance file
forwarding. Removing only `--control` from an automation wrapper is therefore not
a complete LightCraft/Tools integration solution.

### Optional combined LightCraft / Portrait Tools integration

The repo-managed `shp_photocraft_tools.editor_handoff` provides a permissioned
PhotoCraft launcher and a separate acknowledged external-editor helper. Install
only after choosing this integration, using a new private backup directory:

```sh
uv run --no-sync python scripts/install_editor_handoff.py \
  --backup-dir /absolute/path/to/new/private-backup --defer-editor-setting
```

This changes only the recognized PhotoCraft desktop `Exec` and installs
`~/.local/bin/photocraft-dnb` / `~/.local/bin/photocraft-lightcraft`. Existing
different helpers, custom launch actions or concurrent target edits are refused.
The existing shared token/read/write configuration is reused; no broader grants
are added. The launcher starts PhotoCraft only if its port refuses connections;
an existing listener must authenticate, not trigger a duplicate instance.

Normal Tools can reuse an existing permissioned PhotoCraft launcher. Match its
`SHP_PC_PORT`, `SHP_PC_TOKEN_FILE`, `SHP_PC_EXCHANGE`, and `SHP_PC_READ_ROOT`
values rather than creating another token or broadening file access. Preserve
any existing process-identity checks; see the
[connection notes](docs/portrait-tools-fresh-linux-setup.md#7-optional-connect-photocraft).
GUI imports use absolute paths within the read root, avoiding a stale relative
Open-dialog location; headless imports remain read-root-relative.
A running supervisor retains its old environment until Quit and restart;
changing a launcher does not update an existing process.

In the **running LightCraft**, select `~/.local/bin/photocraft-lightcraft` in
Settings > External Editing (program path only, no quotes, flags or `%F`).
Use the full absolute path in that field. Editing its JSON while it runs would
be overwritten by its in-memory preferences on exit. Omit
`--defer-editor-setting` only with LightCraft closed to update that one saved field.
Then save your work and close PhotoCraft normally; reopen it from the updated
desktop shortcut, and restart Portrait Tools to load current integration code.
Do not start a second PhotoCraft while the old instance still owns its port.

The helper validates all file paths under the existing read root, opens each
with authenticated `app.open`, checks the acknowledgement and focuses PhotoCraft.
It supports multiple filenames and spaces without shell interpretation. Failed or
lost replies exit nonzero and produce a desktop error notification; file-open
requests are never replayed after a lost acknowledgement. It never silently
starts a GUI during external-editor handoff. This replaces native single-instance
forwarding for this workflow; LightCraft's built-in PhotoCraft entry is not the helper.
The upstream LightCraft catalog-add/duplicate-copy error happens **before** any
external editor call, so this helper cannot fix that separate bug.

Installer backups preserve the original desktop entry and LightCraft preferences
under their home-relative paths, with a `manifest.json` listing original modes
and whether each helper existed. For rollback, close the affected apps first,
restore only intended fields from those originals (avoid overwriting newer user
preferences), remove only helpers the manifest says were absent, and refresh the
desktop database. No backup contains the PhotoCraft control token.

Shell order is **Skin Mask / FS & Tone / Dodge & Burn / Combine / Crop & Expand / Patch Fill / Settings & Info**.
All four skin tabs share one mounted iframe/source/settings/service. Switching never
reloads the source or resets previews/zoom. Skin Mask is initial whenever Skin Tools is loaded.
Crop and Patch Fill share a second mounted iframe and one service/Comfy connection,
with acknowledged mode changes rather than reloading it. Processing, transfers and
settings changes in that service lock navigation as well as Skin Tools processing.
Tab titles/tooltips report service state; one unavailable module does not block the other.
Stopped/unloaded services have no navigation tabs: stopping Skin Tools hides all four
skin tabs, while stopping Crop hides both Crop and Patch Fill. Starting, running/busy, stopping and error
states remain visible; a connection outage retains the last known visibility.
If the selected service stops, the shell selects available Skin Mask, then Crop & Expand, otherwise
Settings & Info. Starting a service restores its tabs without stealing selection.
Tray Start/Stop controls remain available; the full busy-navigation lock is unchanged.

Native X hides to tray; relaunch/Show presents the same window. Tray supports Show/Hide,
Quit, Restart, explicit Force Restart, and Crop/D&B service/startup controls.
No tray host keeps/restores an accessible window. Stop/Restart/Quit recheck active work
and unsent state; normal restart refuses other active windows. Force Restart warns about
all-window loss and pins only verified owned Linux processes, never PhotoCraft/external
Comfy or process names. Cleanup failures block duplicate replacements.
Loaded browser-only state is not automatically recovered. Default size 1399x949 includes
titlebar; normal geometry persists, Wayland position remains compositor-controlled.

Optional managed Comfy starts with Crop from its existing environment, isolated Tools
user/input/output/temp folders and a dedicated non-8188 port. Existing servers are never
adopted/stopped; collisions fail visibly. Stop Crop alone leaves owned Comfy running;
whole-Tools shutdown drains its queue and stops only the owned child. Unreadable established
queues fail closed. Crop's **Free Comfy memory** refuses active/pending queue work:
success requests asynchronous unload, not a measured RAM/VRAM reduction.
Tools does not start/stop PhotoCraft for reachability status.

## Skin Tools and shared controls

Get Image analyzes once. Shared scale, skin/colour/light/structure mask and preset settings
stay linked across tabs. Edit mask controls and S/landmarks in **Skin Mask > Image analysis**.
That panel is hidden in FS, D&B and Combine; hidden values still apply.
Presets store the whole bundle and mode; switching tabs or S-derived pixel radii does not
star a preset, but editing hidden shared settings does. Legacy `skintone` opens FS & Tone.

| Mode | Controls / previews |
|---|---|
| Skin Mask | Shared mask/ranges/structure and analysis/scale. Left Original; right Mask/Analysis. |
| FS & Tone | Frequency Separation & Skin Tone, Blur, Directional Blur, Skintone Correction. Left Original/Result; right Color/Detail and Directional Region when enabled. |
| D&B | BAND 1, BAND 2, SHARED CONTROLS. Left Original/Result; right Band 1/Band 1 + mask and enabled Band 2 equivalents. |
| Combine | **FS mix and D&B mix**, plus local batch controls. Edit ingredient settings in their own tabs. Left Original/Combined; right FS result/D&B result/Mask. |

Original/Result starts on **Result** (Combined in Combine) at startup and for each
new source, regardless of the previous session's preview selection. Skin Mask
retains its Original-only left pane without changing the retouch preview preference.
Hold **Ctrl + primary mouse button** in either image pane to temporarily compare
Original and Result; releasing the mouse restores the exact prior selection, even
outside the pane or after releasing Ctrl first. Cancel, lost capture and window
blur also restore it. The gesture is inactive during landmark editing, color
picking, processing and Skin Mask mode; other modifiers retain their normal behavior.
Preview switches retain the valid displayed frame until the destination image is
loaded and decoded, and reuse ready images for the current result revision.

Slider readouts update while dragging; image processing waits for release/cancel/lost
capture/blur, keyboard changes debounce. Busy/off/missing geometry gating retains settings.
Themes Current/PhotoCraft affect UI only; PhotoCraft is default absent saved preference.
The rightmost **Settings & Info** page contains Theme Selection, PhotoCraft layer-transfer
options, Skin Mask precache, optional pose backend and the temporary calculation-timings log, plus About.
These controls are global, not module-preset settings. Layer-transfer options persist in
`$XDG_CONFIG_HOME/shp-photocraft-tools/settings.json` (normally
`~/.config/shp-photocraft-tools/settings.json`), migrating the old default preset's
choices once. Legacy presets are retained but no longer change these options; newly
saved presets omit them. Precache (`shp-mask-precache`) and theme (`shp-tools-theme`)
remain global browser-storage preferences, and calculation timings remain temporary.
**Exclude opaque alpha** is on for new settings (RGB rather than RGBA). Existing
include-alpha preferences migrate inversely without changing the resulting files.
Processing controls require the skin-tools service to be running;
Appearance/About remain accessible when it is stopped. Switching to this page does
not change the active skin-tool mode or clear its source/previews.

### Optional Google pose backend (experimental)

**RTMPose / rtmlib remains the default**, unchanged. In **Settings & Info > Pose
analysis backend**, you can explicitly select **google full (experimental, isolated)**,
provide a separate worker Python and official Full model path, then **Save pose
backend**. See [model/runtime provisioning](MODELS.md#optional-google-pose-landmarker-full-experimental).
Nothing is downloaded by selecting it. Missing runtime, wrong model/hash, unavailable
bubblewrap/namespaces or inference errors fail explicitly; there is no silent fallback
and no unrestricted MediaPipe execution. No system prerequisites are installed for you.

The saved choice applies to the **next Get/load** (including watch imports) and is
captured at **batch start**. The current source keeps its original pose, scale, mask
and previews; the settings hint identifies that source's backend. Get/load again to
reanalyze. A successful new load replaces the source generation and clears the existing
preview/mask caches through the normal loading path. A failed analysis leaves the prior
source intact. Backend settings are global, not retouching presets.

Google runs CPU IMAGE mode with at most **two poses** in a restricted Linux worker.
It maps the 33 observations to COCO's first 17 joints, using both visibility and
presence >= 0.5 and in-frame finite coordinates. These are not calibrated rtmlib scores.
Missing/low-confidence joints cannot drive Directional Blur. No equivalent whole-body
eye-corner or hand cues are fabricated; the independent face/iris scale cues remain.
This is not a claim of better scale, pose accuracy, multi-person recall or CPU-only
support for the entire application: only synthetic adapters and a blank-image real
worker smoke have been exercised, not positive portraits.

**Privacy:** MediaPipe 1.2.2's supplied NOTICE discloses performance/utilization metrics
transmission; no public opt-out is verified. The worker has no host network namespace,
routes, inherited sockets, normal home, desktop sockets or user-photo filesystem access.
It receives only the downscaled RGB image through stdin, sees read-only runtime/model
mounts, uses disposable temporary storage and returns a bounded result. Its network
boundary does **not** disable telemetry generation or prove absence of queues/retries.
The parent app retains PhotoCraft/Comfy networking. This is Linux-only and fails closed
if isolation cannot be created; it is not a general desktop privacy guarantee.

### Optional OpenCV Zoo pose backend (experimental)

**RTMPose / rtmlib remains the default.** Settings & Info > Pose analysis backend
also offers **OpenCV Zoo MediaPipe (experimental, CPU)**. It uses the two pinned
local OpenCV Zoo ONNX models through the existing OpenCV DNN dependency; no
separate runtime is installed and selection does not download the models. The
default pair path is `$DNB_MODELS/opencv-zoo/` or, without an override,
`~/.local/share/shp-photocraft-tools/dnb/models/opencv-zoo/`. See
[exact provenance, hashes and limits](MODELS.md#optional-opencv-zoo-mediapipe-onnx-pose-backend-experimental).

The backend runs CPU inference in the Tools process. It maps the 33-point model
to the first 17 COCO body joints, rejecting low-confidence or out-of-frame
geometry; it does not fabricate eye-corner or hand cues. The adapter makes no
model-service/network requests, but this is not a claim that the whole app has
no network activity. Both this and Google Full are experimental options; neither
is a silent fallback or known quality improvement. Only synthetic tests and a
blank-image model smoke have been run, not positive portrait evaluations.

Image-analysis details, the mask-cache status badge, non-timing Log notes and all existing
operation/status messages stay in their original locations. Standalone skin-tools views
offer the same general controls in a Settings & Info section.
Shared grey-off/blue-on switches, transparent disabled readouts, accessible focus and
responsive equal-width action slots remain. Quiet preview work delays visible busy feedback
150 ms but blocks conflicting input immediately. Status measures completion through image
load events or send cleanup, not final screen paint.

### D&B bands

Each independently enabled band has feature range/taper/Strength and its own gain graph.
Band 1 defaults on; band 2 off. Both analyze Original, then masked Soft Light applies
band 1 followed by band 2. Band 2 **invert** flips only its neutral-grey layer (`1-layer`)
to enhance instead of reduce tonal differences. Shared Layer blur/edge/tau/max EV remain.
Layer blur is 0-20 px at S181, scaled Gaussian sigma; 0 preserves legacy output.
Tau slider 0.01-0.4 retains positive legacy values outside its range until moved.

**Band diagnostic previews always exaggerate x8 about neutral 0.5**, including "+ mask"
and band 2; no toggle remains. This is visualization only. Result, Combine D&B result,
saved rasters and sent layers remain actual strength.
The **Band 2 / Band 2 + mask** preview tabs remain available even when Band 2 is
off; switching it off does not change the selected preview. Disabled-band image
content may be empty after recalculation; this does not enable its contribution.

### Frequency separation, Blur and Skintone

Gaussian, Median and Median Fast produce unmasked Color/Linear Light Detail from the
whole source. Radius calibration is 0.5-30 px at S181 (default 6), scaled/rounded to
1-100 full-image pixels. Blur radius is 100-500% (default 200) of that rounded radius.
Median Fast is faster but not numerically identical to Median.

Blur's switch controls only additional Gaussian Blur, not Color/Detail.
Strength is 0-100%. Its skin-mask switch uses normalized skin-weighted sampling and a
separate editable mask in the optional per-layer export mode; off samples the whole
image and leaves Blur unmasked in that mode. The default top-folder export instead
applies one shared skin mask to the complete SkinTools folder.
Turning Blur off skips its work but retains the Color/Detail foundation.
Skintone is one masked Hue LUT between low-frequency effects and Detail; off bypasses
Hue calculations. Legacy Saturation is ignored, not a separate correction.
FS/Combine require opaque finite supported SDR source samples and captured ICC.
Filters cannot undo colour mixing already present in the full-image Color split.

### Directional Blur

Off by default; select anatomical left/right upper arms, forearms, upper legs, lower
legs, plus torso. Camera-facing rows place anatomical right on viewer left.
Each pair/torso has independent **Distance / Width / Taper / Strength / Feather /
Start / End**. Old globals migrate into each group in memory; presets remain unchanged
until explicitly saved. Upper-leg selections default on, added limbs/torso off.

Finite in-image nondegenerate coordinates define availability; low confidence alone
does not block selection. Unavailable preset limbs/torso are **skipped**, not fatal:
grey disabled switches preserve selection, controls hide if no selected side is usable,
and future usable geometry restores them. All missing bypasses Directional while other
effects/outputs remain usable. Hover switch/label for reasons; informational paragraph
is hidden. Invalid settings/scaled limits/finite-mask data still fail explicitly.

Limb Width is thickness **perpendicular to the joint line**, not screen horizontal.
Defaults Distance 30 / Width 100 / Feather 3 px at S181, Strength 100%, Taper 0%,
Start 0%, End 100%. Taper narrows toward the adjusted distal end; endpoints stay at least
1% apart. Torso motion is shoulder midpoint->hip midpoint over its convex shoulder/hip
box: Width 100% covers the whole box, 1-200% narrows/expands around centreline; taper
narrows the hip end, Start/End crop along the axis. Torso is not a limb capsule.
Legacy Length above 100% is rejected rather than silently clipped.

Sampling uses normalized skin weights to exclude clothing without black edges.
All regions share one raster/mask: equal strengths retain full-strength raster/common
native opacity; differing strengths bake relative deltas with normalized feather-weighted
overlap, not additive/sequential blur. Equal feathers retain Gaussian union; different
feathers use max individual coverage. Disabled/unavailable groups skip work.

### Combine

FS mix blends Original->configured FS result, then original-derived D&B Soft Light
bands act on that base at D&B mix. Both sliders are 0-100%, default 100%.
Right **FS result / D&B result** show configured effects **before** mix even at mix 0;
Combined uses mixes. Shared settings remain linked without duplicate panels.
Final full-resolution work skips zero contributions; no layered standalone format
is introduced.

The separate collapsible **BATCH PROCESSING** panel in Combine accepts a folder or
multiple image files from one folder. One **Choose source** button offers Folder or
Images, then opens the appropriate native picker; there are no manual path fields.
Folder and file selections both display the discovered image count. Choose source and
Start Batch use the shared workflow-button styling; Cancel appears while processing.
Source selection requires the Tools desktop. Folder selection includes only direct children in the existing
JPEG/PNG/TIFF/WebP formats, not nested subfolders. Batch is Combine-only: it never runs
Crop or contacts PhotoCraft, even when the current editor source is a PhotoCraft document.

Start captures the current mix and ingredient settings. Each image is independently
decoded/analyzed for scale S, pose and skin mask, and its FS radii are recalculated
from the calibration/Blur percentage. Other settings, including Hue Center, stay fixed.
An image with no usable detected scale fails explicitly instead of borrowing the
editor's S. Symbolic-link inputs are refused. The loaded editor image/settings stay intact.

Each run exclusively creates `shpcombine_batch_001` (or the next unused number) inside
the input folder. Each successful item gets a RGB16 flattened PNG or TIFF named
`<source-stem>_001.png` (or `.tiff`), with the batch folder number after every stem and
source ICC/sRGB, using global format/compression/opaque-alpha options captured at batch start.
One `preset.json` stores the shared mix/ingredient settings at the top level and a `batch`
manifest recording that output-options snapshot and mapping each source/status/independently detected scale and FS radius to its
result filename. It is updated atomically after each item; no per-image JSON copies are
written. Existing folders, outputs and source files are never overwritten. An
output-name collision (including same-stem inputs with different original extensions)
is reported as a per-image failure, without adding item numbers. An external
edit to the batch preset stops metadata updates explicitly. Progress and per-image errors
remain visible in the panel, and later images continue after per-image failures.
Cancel is cooperative: it stops at the next safe processing boundary, preserves already
saved results, and marks unsaved/current and remaining items cancelled in `preset.json`.
An in-progress inference/filter/atomic save finishes before that boundary; Cancel does
not terminate worker processes or delete completed output. The top status updates after
each photo as `Batch processing... (3/8)`; completion includes total runtime in seconds
to one decimal, e.g. `Batch processing completed successfully (42.3s).`, with the existing checkmark treatment.
Output locations, counts, timings and failure details go to the log.

## Send Result, Send Layers and local output

Loaded provenance decides the destination. Get Image opens the owned desktop's native
local chooser if disconnected; standalone browsers use path/list controls, not uploads
masquerading as source-directory access. Cancel keeps previous work. Local sources save
locally even if PC later connects; disconnected PC sources error, never save to exchange.

**PhotoCraft FS/D&B/Combine have two actions:**

* **Send Result:** corrected flattened RGB16 Normal photograph, matching the intended
  calculation rather than exaggerated diagnostics.
* **Send Layers:** one editable **SkinTools** folder, enabled ingredients only:
  band 2 / band 1 (Soft Light), Detail (Linear Light), Hue, Directional
  Blur, ordinary Blur, Color.

Settings & Info > PhotoCraft transfer options > **Send Layers mask** defaults to
**Mask on top folder**: one shared Skin Mask on SkinTools, no ingredient masks.
**Mask on every layer** restores the previous ingredient-specific masks (bands,
Hue, Directional Blur and ordinary Blur when its mask switch is enabled); Color
and Detail remain unmasked. The global choice persists independently of presets;
existing settings without this choice adopt the folder default without losing other
preferences. Folder masking affects the whole editable stack, so its appearance can
differ from individually masked ingredients. Flattened Send Result, local exports,
Crop and standalone Skin Mask are unchanged.

FS-only omits bands; D&B-only contains bands and uses Pass Through so they affect the
destination below. FS/Combine use Normal folders with their Color foundation.
Combine inserts one parent, not separate FS/D&B folders. Empty editable contribution
is refused. Crop and Skin Mask keep only Send Result.

RGB16 scratch layers carry captured ICC **without numeric colour conversion**; masks
remain separate PNG8. Repeat Get after destination size/profile/replacement changes:
metadata cannot prove exact document incarnation/profile identity. No target profile
conversion is performed. Final one-copy insertion reports lost-reply uncertainty instead
of risking duplicate retries; temporary docs/files are cleaned and errors surfaced.

Combine editable partial FS mix interpolates ingredient rasters against Original and
Detail against neutral, retaining masks; Hue amount scales by mix. Nonlinear Hue,
clipping and quantization can differ from flattened output. **Send Result is authoritative**
when exact photograph appearance matters. Editable ingredients remain adjustable.

### PhotoCraft export watch folder

The skin-tools UI watches the direct PNG children of
`/home/stef/shp-photocraft-tools/watchfolder` while connected and idle. Export there
with the PhotoCraft action; Tools waits at least two seconds without a file change
and verifies the complete PNG before importing. Other formats, nested folders and
symlinks are not imported.
The tray controller also polls the service independently of the WebUI. A stable,
complete PNG presents the existing Tools window (including when hidden in the tray);
the resumed UI then imports it through the same Get Image flow. No second app is
launched. The desktop controller and skin-tools service must be running; a backend
without a tray/controller cannot open a desktop window.

Crop has its own independent PNG inbox at
`/home/stef/shp-photocraft-tools/watchfolder_crop`. Start **Crop** in the Tools tray
if it is unloaded. Stable exports use Crop's Get Image flow and automatically select
the **Crop** tab, associating the active PhotoCraft document with the same identity/
revision/dimension checks and cleanup-on-success rules. Skin Tools processing blocks
Crop imports until navigation unlocks. The tray detects either inbox without opening
a second app; each service only consumes its own inbox. `watchfolder_crop/` is Git-ignored.
Neither inbox is created by installation or service startup. Create the required
directory yourself, or have the PhotoCraft export action create it. Missing inboxes
are reported, not silently created. These fixed paths currently belong to the Linux
installation; configurable portable paths are future work.

Each incoming image automatically associates with the **active PhotoCraft document at
processing time**, without a dialog. Keep the exporting document active until import
finishes: a PNG cannot prove its origin, so matching dimensions cannot distinguish two
different same-size documents. Tools checks identity/revision and image dimensions around load/analysis,
then runs the normal Get Image UI flow (including Hue Center and preview calculation).
Successful watch imports automatically select **Skin Mask**, including after a tray wake;
failed/invalid imports do not change the selected tab.
Send Result / Send Layers use the captured PhotoCraft document, not a local destination.
Imports wait until current processing/batch work is finished.
Crop also waits for its own expansion/load work and displays a pending-import hint.
The backend locks are per service, not a global atomic lock across every module;
the normal shared-window/tray flow additionally coordinates busy status.

The inbox PNG is removed only after successful load and association. Partial,
invalid or failed imports remain in place, with errors in Log/status. Missing/disconnected
targets retry automatically; association failures retry when the active document identity,
revision or dimensions change. Invalid unchanged PNGs wait for a corrected export.
Cleanup failure
is reported without reimporting an already loaded file. Tools retains an app-owned source
snapshot in its data `dnb/inbox/watch-*/` directory, like other Get Image source captures.
There is no independent watcher process: polling ends with the desktop/UI/service, and shutdown
uses the existing module work gate. `watchfolder/` is ignored by Git.

### Local outputs

Local **Send Result** creates one exclusively numbered PNG or TIFF beside the source, no sidecar,
overwrite or layered format. Permission/identity/symlink/disk errors are explicit.
Global format/compression/opaque-alpha choices apply to flattened D&B/FS/Combine results,
including standalone Save. Parallel encoding applies to supported multi-layer PhotoCraft
transfers; batches process images sequentially and each flattened result has one encoder.
Masks and Crop remain unchanged.

| Mode | Suffix | Output |
|---|---|---|
| Crop | `_shpcrop_001.png` | Selected RGB8 final canvas; inherited sRGB conversion, no new ICC embedding guarantee |
| Skin Mask | `_shpskinmask_001.png` | Grayscale8 |
| D&B | `_shpdnb_001.png` or `.tiff` | RGB16, optional opaque alpha, processing ICC/sRGB |
| FS | `_shpfs_001.png` or `.tiff` | RGB16, optional opaque alpha, source ICC/sRGB |
| Combine | `_shpcombine_001.png` or `.tiff` | RGB16, optional opaque alpha, source ICC/sRGB |

Standalone Save remains separate: D&B enabled bands/mask/settings/full result;
FS Color/Detail/optional Blur/Directional/Hue cube/masks/result/settings;
Combine `result.png`/`result.tiff` and `settings.json`; Skin Mask mask/settings.
Standalone ingredient layers stay in their existing format; only flattened results
use global file options. Export metadata records the output settings separately from
module preset settings.

## Cache, diagnostics and performance

Mode previews retain at most one latest entry each within 192 MiB; generation,
region/scale/settings/pose changes invalidate appropriately. Full-resolution outputs
never upscale preview rasters. One immutable full-mask/optional-structure cache is capped
at 256 MiB and reused across modes when mask/source/S identity matches.
Mix/FS/Hue/band-only edits do not regenerate an identical mask.

Default-on **Precache skin mask**, under Settings & Info, starts one worker
after 600 ms settled preview; stale/over-budget work is discarded. Send waits for matching
work or computes its mask. Cross/spinner/check reflect the actual retained entry.
Checkbox is a local preference, not preset; disabling cancels pending preparation.
Cache retention caps do not limit temporary working memory.

Log notes remain bounded browser memory in Skin Mask Image analysis; calculation timings
are separate in Settings & Info. Genuine errors use
status/tooltips, not hidden success. Stage timings measure actual command/work completion,
not paint. [HANDOFF.md](HANDOFF.md#mask-cache-resource-measurements-and-validation-boundaries)
separates historical synthetic microbenchmarks from live observations:
headless transfer options differed, but live user sends were 11-13 s across choices,
so TIFF is not a demonstrated real-world speedup. Nine-region synthetic Directional
was about 1.64-1.77 s /710 MiB without inference/import; not a Combine/full-Send guarantee.

## AI Fill: Crop & Expand and Patch Fill

**Crop & Expand** and **Patch Fill** are adjacent top-level Tools tabs sharing one source,
ComfyUI connection and lifecycle. Switching modes retains the source, painted mask,
selected result preview and each source view's zoom/pan; no internal mode switch remains.
Loading a new source clears the mask. A successful Crop watch-folder import selects
**Crop & Expand**, not Patch Fill. Standalone browser access to Patch Fill uses `/?mode=patch`
on the Crop server.

For expansion, FLUX.2 remains default; FLUX.1 Fill has independent guidance 30, steps 28, 0.5 MP,
feather 64, Fill model and upscaler. Fill always colour-matches/seam-corrects/pastes
Original back, without LoRA; FLUX.2 keeps optional Turbo/seam choices.
Both offer model upscaling or Lanczos (no model); only selected neural models require
upscale nodes. Text encoding CPU/default is saved, not a guaranteed GPU-memory fit.
Old presets default to FLUX.2 without reinterpreting settings.

**FLUX.2 Klein** is the third expansion method, alongside the unchanged default
FLUX.2 dev and FLUX.1 Fill. It has separate `klein_*` settings: defaults are
4 steps, 2 MP, feather 32 px, Lanczos resizing and seam correction off.
Its optional Turbo LoRA is
off by default: select Klein-specific weights and a strength in Advanced, then enable
the toggle. Local safetensors projection widths are checked before generation;
incompatible or unrecognised weights block the run with an error, rather than being
silently skipped. FLUX.2 dev Turbo and FLUX.1 Turbo weights do not fit Klein 9B.
Put compatible weights under `ComfyUI/models/loras` (`SHPC_COMFY_DIR` overrides the
local ComfyUI directory). Width checks do not establish training family or effectiveness:
use weights explicitly published for the selected Klein model.

Klein prepares its overlap in source pixels (fixed 24 px growth, Gaussian generation-mask radius 8 px)
before resizing. Only missing pixels receive a blurred edge continuation; the
original alone remains the model reference. Its seam correction adds a smooth
colour-offset field measured on safe original pixels, without filtering generated
texture. Feather controls only the final Gaussian RGB transition into the original; zero keeps
a hard join. Klein sends this already blended result as an opaque layer, so its
placed seam matches the preview instead of applying the feather a second time.
Original pixels outside the transition remain unchanged. Other methods retain
their existing compositing. These independently implemented steps do not reproduce
GenFill's exact prefill/reference or guarantee identical generated results.
The user reports it is a useful third option; this is not a universal quality
ranking. Restart Tools after installing backend updates and generate a new
result; already generated results retain their previous compositing.

Ratio/free handles can crop/expand; positive margins cap at **999 original pixels per
side** (`SHPC_LIMIT` can lower), negative cropping unrestricted. CROP shows resolution
and spatial shifts with zero placeholders. Generate uses 1/2/3 choices, becomes Cancel
and publishes each finished variation immediately; later results do not steal selection.
Cancel/failure preserves finished results, Send waits for generation to stop.
Send resizes canvas/copies generated RGBA with inherited best-effort undo grouping.
AI SETTINGS is collapsible; zoom/pan/double-click fit remain.
Middle-mouse drag pans the viewport in Crop and Patch Fill (including result views)
without moving the crop frame or painting/erasing. Release ends the pan; Patch Fill
also retains Space+left-drag panning.

**Patch Fill** uses the existing FLUX.1 Fill processing setup and
variation/cancel workflow, with independently saved model, prompt and generation
settings, without changing expansion's saved method. Paint or erase
over the source; **Clear** resets the mask. Brush diameter is in original source pixels
(1-512), independent of zoom; use the wheel to zoom and Space+drag to pan. An empty
mask cannot Generate. Select a finished result to preview it; Send Result retains
the current local numbered-save or PhotoCraft-document association.
Paint and Erase show a contrasting circular cursor at the displayed brush diameter,
scaling with brush size and zoom; it hides outside the image, while panning or busy,
and in result/expansion views.

The **context area** slider chooses what the model sees: **0% Local context** uses
one bounding rectangle enclosing every painted stroke, padded on each side by 25%
of its painted width/height or at least 64 original pixels, clipped to image bounds.
Moving the slider toward **100% Whole image** progressively expands each edge of
that rectangle to the corresponding image edge. At 100% the previous full-image
processing path is preserved. Disconnected strokes share one enclosing rectangle;
even faint painted pixels are included. Near image edges the available context is
naturally smaller; a widely spread mask may already need the whole image at 0%.

The source and mask are cropped together. The megapixel setting applies to this
region, with existing 16-pixel model sizing, colour/seam correction and optional
upscaling. The generated region is mapped back to its exact source coordinates;
only the painted mask is composited, never the surrounding context. A local region
gives a small patch more model-resolution detail but less scene context. It does
not promise faster generation at unchanged megapixels/steps.
The panel reports `Processing <region width> x <region height> of <source width> x
<source height> px`, using the actual padded/clamped source-pixel region before model
resizing. It updates after painting/erasing and while adjusting Context; an empty mask
reports a zero-size processing region. Ratio, variation and Paint/Erase selection
use gray highlighting; the thin divider separates Patch Fill and AI Settings.

**Settings and named presets are separate for Crop and Patch Fill.** Crop keeps
`settings.json` / `presets.json`; Patch Fill uses `patch-settings.json` /
`patch-presets.json` in the same Crop data directory. On first use, existing saved
Fill settings and named presets are copied into the Patch files once, without
overwriting Crop's files. Later saves, preset loads/deletes and Factory settings
resets affect only the selected mode. A reset uses factory defaults, not the initial
copied values. Context area is Patch-only and saved in its settings and named presets;
missing/older values and Factory settings use 100% to preserve prior behavior.
Prompts are now saved independently with each mode's settings/presets.
Variation count is a separate browser preference per mode; the mask and paint/erase
brush selection remain session-only.

Connection URL, managed Comfy lifecycle, queue and memory-release action stay shared.
The CPU/default **text encoding** choice is also a shared resource preference, outside
both modes' presets and resets; old preset copies of that field no longer override it.
Diffusion/upscale model selections, guidance, steps, megapixels and feathering remain
independent processing settings.

Patch results retain the loaded image dimensions. **Feather px** fades inward only;
pixels outside the painted mask remain byte-for-byte identical to the loaded working
image. Crop's working source is RGB8 sRGB, so this is not a promise of retaining
original encoded high-depth/ICC samples. The saved flattened result and generated
RGBA patch layer share that effective mask; PhotoCraft receives the masked layer
without resizing its canvas. No real Comfy/GPU generation was exercised by the
isolated regression tests; check your existing Fill models/nodes before first use.

## Data, compatibility and isolated verification

| Resource | Default |
|---|---|
| Crop and Patch Fill settings/presets, shared inbox/results | `~/.local/share/shp-photocraft-tools/crop/` |
| Skin presets/inbox/results/models | `~/.local/share/shp-photocraft-tools/dnb/` |
| Native data/cache/geometry | `<XDG_DATA_HOME>/shp-photocraft-tools/webview/` |
| Supervisor/module/desktop/Comfy logs | `<XDG_STATE_HOME>/shp-photocraft-tools/` |

Standalone `SHPC_DATA`, `DNB_DATA`, `DNB_MODELS`, `DNB_EXIFTOOL` override resources.
Supervised modules use launcher-owned data directories. Compatible SHPC graph/PC and
DNB PC aliases remain; credentials are read at connection time, never copied into code.
Browser keys are per-origin/profile. No automatic import of settings/photos/credentials.

Use free ports and temporary resources beside production; do not share writable data or
token exchange. `--port 0` selects a free port; do not use 18765/18768 while owned.

```sh
uv run --no-sync python tests/preview.py        # synthetic standalone module UIs
uv run --no-sync python tests/shell_preview.py  # synthetic owned supervisor
uv run --no-sync python tests/run.py            # broad isolated regression suite
PYTHONPATH=tests uv run --no-sync python -m unittest \
  test_combine test_skin_tools_transfer test_directional_blur \
  test_second_band test_status_layout test_dnb_progress
uv build
```

Fixtures use temp data/ports/settings, fake detections/services and real CPU math.
When installed, `photocraft-cli` tests create isolated stdio engines, never live desktop
documents. Optional `SHP_DESKTOP_SMOKE=1` enables a separate real GTK/WebKit test window
on a graphical/tray host, not a reload of live Tools. No GPU inference/model download
is needed for these synthetic checks. Fresh-machine GPU/portrait quality are not implied.

```text
src/shp_photocraft_tools/
  launcher.py, supervisor.py, comfy.py   owned services/proxy/lifecycle
  desktop/, shell/                      native helper and shared-view shell
  shared/ui/, shared/photocraft/         packaged local assets/transport
  modules/crop/, modules/dnb/            independent engines/services
tests/                                  isolated math/native/UI/lifecycle fixtures
desktop/, scripts/                      installer/templates/icons
comfyui/custom_nodes/                    separately installed seam-node source
docs/source/                            frozen provenance-era technical notes
```

Wheel/sdist package both UIs/shared resources/shell/native helper; seam node is sdist-only.
No Blemish/ClearFrame prototype or private dataset is part of this release.
**Restart Tools only when explicitly authorized** to activate new backend code:
publication itself leaves live processes/documents/installed assets untouched.
