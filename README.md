# SHP PhotoCraft Tools

Two independently runnable modules in one Python monorepo: **Crop** and the shared
**Skin Mask / FS + Skintone / D&B / Combine** service. They share local packaged UI
resources and PhotoCraft transport, not incompatible canvas/layer application logic.
An optional shell and owned GTK/WebKit desktop provide one window with retained views.

| Module | Command | Standalone default |
|---|---|---|
| Crop | `uv run shp-photocraft-crop` | http://127.0.0.1:8768/ |
| Skin tools | `uv run --extra gpu shp-photocraft-dnb` | http://127.0.0.1:8765/ |

Private repository: `stefhartog/shp-photocraft-tools`, local checkout
`/home/stef/shp-photocraft-tools`. Original projects are provenance, not installation
dependencies. See [HANDOFF.md](HANDOFF.md) for the current implementation/limitations,
[PROVENANCE.md](PROVENANCE.md) for source revisions and [MODELS.md](MODELS.md) for
external weights/nodes. Frozen `docs/source/` notes are historical.

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

No silent Chrome fallback: native Ungoogled Chromium or its named Flatpak is required.
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

Shell order is **Skin Mask / FS + Skintone / D&B / Combine / Crop**.
All four skin tabs share one mounted iframe/source/settings/service. Switching never
reloads the source or resets previews/zoom. Crop is initial unless disabled on startup.
Tab titles/tooltips report service state; one unavailable module does not block the other.

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

## Skin tools and shared controls

Get Image analyzes once. Shared scale, skin/colour/light/structure mask and preset settings
stay linked across tabs. Edit mask controls and S/landmarks in **Skin Mask > Image analysis**.
That panel is hidden in FS, D&B and Combine; hidden values still apply.
Presets store the whole bundle and mode; switching tabs or S-derived pixel radii does not
star a preset, but editing hidden shared settings does. Legacy `skintone` opens FS + Skintone.

| Mode | Controls / previews |
|---|---|
| Skin Mask | Shared mask/ranges/structure and analysis/scale. Left Original; right Mask/Analysis. |
| FS + Skintone | Frequency separation, Blur, Directional Blur, Skintone Correction. Left Original/Result; right Color/Detail and Directional Region when enabled. |
| D&B | BAND 1, BAND 2, SHARED CONTROLS. Left Original/Result; right Band 1/Band 1 + mask and enabled Band 2 equivalents. |
| Combine | **FS mix and D&B mix only**. Edit ingredient settings in their own tabs. Left Original/Combined; right FS result/D&B result/Mask. |

Slider readouts update while dragging; image processing waits for release/cancel/lost
capture/blur, keyboard changes debounce. Busy/off/missing geometry gating retains settings.
Themes Current/PhotoCraft affect UI only; PhotoCraft is default absent saved preference.
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

### Frequency separation, Blur and Skintone

Gaussian, Median and Median Fast produce unmasked Color/Linear Light Detail from the
whole source. Radius calibration is 0.5-30 px at S181 (default 6), scaled/rounded to
1-100 full-image pixels. Blur radius is 100-500% (default 200) of that rounded radius.
Median Fast is faster but not numerically identical to Median.

Blur's switch controls only additional Gaussian Blur, not Color/Detail.
Strength is 0-100%. Its skin-mask switch uses normalized skin-weighted sampling and a
separate editable mask; off samples the whole image and sends Blur unmasked.
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
Final full-resolution work skips zero contributions; no batch/layered standalone
format is introduced.

## Send Result, Send Layers and local output

Loaded provenance decides the destination. Get Image opens the owned desktop's native
local chooser if disconnected; standalone browsers use path/list controls, not uploads
masquerading as source-directory access. Cancel keeps previous work. Local sources save
locally even if PC later connects; disconnected PC sources error, never save to exchange.

**PhotoCraft FS/D&B/Combine have two actions:**

* **Send Result:** corrected flattened RGB16 Normal photograph, matching the intended
  calculation rather than exaggerated diagnostics.
* **Send Layers:** one editable **SkinTools** folder, enabled ingredients only:
  band 2 / band 1 (masked Soft Light), Detail (Linear Light), Hue (masked), Directional
  Blur (masked), ordinary Blur (mask optional), Color.

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

Local **Send Result** creates one exclusively numbered PNG beside the source, no sidecar,
overwrite or layered format. Permission/identity/symlink/disk errors are explicit.

| Mode | Suffix | Output |
|---|---|---|
| Crop | `_shpcrop_001.png` | Selected RGB8 final canvas; inherited sRGB conversion, no new ICC embedding guarantee |
| Skin Mask | `_shpskinmask_001.png` | Grayscale8 |
| D&B | `_shpdnb_001.png` | RGB16 opaque-alpha, processing ICC/sRGB |
| FS | `_shpfs_001.png` | RGB16 opaque-alpha, source ICC/sRGB |
| Combine | `_shpcombine_001.png` | RGB16 opaque-alpha, source ICC/sRGB |

Standalone Save remains separate: D&B enabled bands/mask/settings/full result;
FS Color/Detail/optional Blur/Directional/Hue cube/masks/result/settings;
Combine `result.png` and `settings.json`; Skin Mask mask/settings.

## Cache, diagnostics and performance

Mode previews retain at most one latest entry each within 192 MiB; generation,
region/scale/settings/pose changes invalidate appropriately. Full-resolution outputs
never upscale preview rasters. One immutable full-mask/optional-structure cache is capped
at 256 MiB and reused across modes when mask/source/S identity matches.
Mix/FS/Hue/band-only edits do not regenerate an identical mask.

Default-on **Precache skin mask**, under Skin Mask > Image analysis, starts one worker
after 600 ms settled preview; stale/over-budget work is discarded. Send waits for matching
work or computes its mask. Cross/spinner/check reflect the actual retained entry.
Checkbox is a local preference, not preset; disabling cancels pending preparation.
Cache retention caps do not limit temporary working memory.

Log is bounded browser memory, visible in Skin Mask Image analysis; genuine errors use
status/tooltips, not hidden success. Stage timings measure actual command/work completion,
not paint. [HANDOFF.md](HANDOFF.md#mask-cache-resource-measurements-and-validation-boundaries)
separates historical synthetic microbenchmarks from live observations:
headless transfer options differed, but live user sends were 11-13 s across choices,
so TIFF is not a demonstrated real-world speedup. Nine-region synthetic Directional
was about 1.64-1.77 s /710 MiB without inference/import; not a Combine/full-Send guarantee.

## Crop

FLUX.2 remains default; FLUX.1 Fill has independent guidance 30, steps 28, 0.5 MP,
feather 64, Fill model and upscaler. Fill always colour-matches/seam-corrects/pastes
Original back, without LoRA; FLUX.2 keeps optional Turbo/seam choices.
Both offer model upscaling or Lanczos (no model); only selected neural models require
upscale nodes. Text encoding CPU/default is saved, not a guaranteed GPU-memory fit.
Old presets default to FLUX.2 without reinterpreting settings.

Ratio/free handles can crop/expand; positive margins cap at **999 original pixels per
side** (`SHPC_LIMIT` can lower), negative cropping unrestricted. CROP shows resolution
and spatial shifts with zero placeholders. Generate uses 1/2/3 choices, becomes Cancel
and publishes each finished variation immediately; later results do not steal selection.
Cancel/failure preserves finished results, Send waits for generation to stop.
Send resizes canvas/copies generated RGBA with inherited best-effort undo grouping.
AI SETTINGS is collapsible; zoom/pan/double-click fit remain.

## Data, compatibility and isolated verification

| Resource | Default |
|---|---|
| Crop settings/presets/inbox/results | `~/.local/share/shp-photocraft-tools/crop/` |
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
