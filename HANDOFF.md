# SHP PhotoCraft Tools handoff

This is the consolidated current-state handoff for the migration, launcher/supervisor,
native desktop, managed ComfyUI and subsequent UI refinements. It replaces the incremental
conversation chronology. See [README.md](README.md) for setup/commands, [PROVENANCE.md](PROVENANCE.md)
for exact sources and `docs/source/` for frozen original algorithm/graph notes.

## Scope and accumulated decisions

The new private repository is `/home/stef/shp-photocraft-tools`,
`stefhartog/shp-photocraft-tools`, on `main`. Migration was published as
`3adb22f98ee88550f21de55b23d9b4157b66fd17`; later work adds the Tools desktop experience.
The user requested consolidation, documentation updates, commit and push on 2026-11-05.

Original `/home/stef/shpcrop` and `/home/stef/dnb` sources were read-only during migration;
those local checkouts have since been removed separately. They are provenance, not runtime
dependencies. The migration did not import personal presets, settings, photos, credentials,
environments, models or generated files. The later approved model/ExifTool preservation is
described below. User-approved legacy Crop/D&B shortcut cleanup happened separately; PhotoCraft/
OpenFill shortcuts were retained, including the misleadingly named PhotoCraft launcher.
The distinct Tools installer does not remove or overwrite those launchers.

The launcher evolved from independent browser tabs to a thin combined shell, then to an
owned GTK/WebKit window and tray. Ungoogled Chromium remains an explicit fallback, not the
default. The web Services/details panel was removed in favor of native tray controls.
PhotoCraft remains external/informational; the user explicitly requested a dedicated
Tools-owned ComfyUI process, superseding the earlier reachability-only design.

Real service/window activations and the later termination of all then-current ComfyUI
instances were separately user-approved. Historical process IDs/status are not current-state
claims. Publication does not authorize reopening/reloading live views, restarting services,
running inference, changing PhotoCraft documents or reinstalling desktop assets.

## Architecture and preserved behavior

Each module retains an independent `ThreadingHTTPServer`, processing engine, UI and settings.
Standalone defaults remain Crop **8768**, D&B **8765**, with compatible `shpcrop`/`shpdnb`
aliases. No module registry, plugin framework or combined processing engine was introduced.

| Surface | Responsibility / invariant |
|---|---|
| `shared/ui/` | One `importlib.resources` CSS resource served identically by both modules; shared switches, controls and lifecycle script. No Git CSS dependency. |
| `shared/photocraft/bridge.py` | Common JSON-lines transport, auth/config/env precedence, reconnect/errors, session/headless negotiation, document lookup and merged-document fetch. |
| `modules/crop/photocraft_bridge.py` | Signed canvas crop/resize, generated RGBA layer and best-effort undo grouping. Crop alone calls `image.crop`. |
| `modules/dnb/photocraft_bridge.py` | Destination-ICC-tagged RGBA16 scratch layer, separate mask document, revealSelection retry, Soft Light mode and one layer copy. Never resizes the canvas. |
| `launcher.py` / `supervisor.py` | Verified startup/reuse, fixed proxy, module controls, external status and owned lifecycle. |
| `shared/health.py`, `supervision.py`, `lifecycle.py` | Identity, private work/stop gate, revision guards and desktop/browser leases. |
| `desktop/controller.py` | Standalone system-Python GTK/WebKit window, tray, dialogs, geometry and guarded Quit/reload. |
| `comfy.py` | Dedicated process watchdog, actual listener ownership and queue-aware shutdown. |

Original PhotoCraft clients were compared before extraction; compatible transport/session/
fetch/launch differed only by prefixes, config paths and labels, now subclass attributes.
Do not combine the incompatible result-application semantics. Original best-effort cleanup,
reconnect/retry and undo behavior remain; this is not a transaction redesign.

### D&B numeric Soft Light transfer

Isolated `photocraft-cli 0.18.0 (8a87a457a)` tests established that
`layer.copyToDocument` colour-converts RGB layer samples between document profiles.
The old untagged RGBA16 correction opens as sRGB: its neutral code 32768/65535 becomes
approximately 0.49612 in Adobe-RGB-compatible, 0.42467 in ProPhoto-compatible, and 0.21405
in linear-sRGB/ACEScg destinations. sRGB and Display P3 preserve that gray. The problem
is numerical blend-data conversion, not the D&B calculation or the source file extension.
This reproduces a Send-only darkening mechanism; the original problematic RAW/profile
has not been examined, so it is not proof of that particular file's profile.

Send now exports a temporary merged duplicate of the current destination to obtain its
actual ICC bytes, then inserts only an `iCCP` chunk into the OpenCV RGBA16 scratch PNG.
This assigns the destination space without changing any correction samples; there is no
guessed gamma, sRGB substitution or builtin-profile name mapping. PNG metadata insertion
avoids Pillow's RGB16 writing limitation and the automation API's refusal of filesystem
ICC paths in `edit.assignProfile`. The mask PNG stays separate and untagged. Failure to
obtain an ICC profile refuses Send before copying a layer rather than falling back.
The extra merged export adds temporary I/O; no user's document is converted or saved.

`tests/test_dnb_transfer.py` uses only a new stdio engine, with private temporary roots.
It checks sRGB, untagged/default, Display P3, Adobe-RGB-compatible, ProPhoto-compatible,
linear sRGB, Rec.2020 and ACEScg at 8/16/32-bit destination depths, plus a non-builtin
embedded ICC. Checks cover neutral and nonneutral numeric corrections, exact mask values,
W3C Soft Light composition, preserved dimensions, scratch cleanup and one target history
step. Neutral means the existing 16-bit representation `round(0.5 * 65535) = 32768`,
not a new rounding convention. CPU D&B and fake desktop/headless transport regressions
remain separate. The Send fix is integrated into the main checkout without changes to the
engine or preview. Activation in an already running D&B backend requires a later approved
restart; no live app/service restart, commit or push is part of this integration.

Crop's geometry, signed margins, Free height, WebSocket progress/polling fallback, variations
and cancellation are preserved. `expand.py` now also builds a native FLUX.1 Fill graph
alongside the unchanged FLUX.2 graph.
D&B's correction math, mask composition, ICC handling, output precision, scale cues, picker,
preview crop and full-resolution processing remain. Analysis scripts use package-relative
imports. Segmenter/model sessions are lazy; external missing files/inference failures
produce actionable errors rather than successful-looking results. ExifTool comes from
`PATH` / `DNB_EXIFTOOL`; the later cleanup preserved a verified copy of the complete tool tree.

## Supervisor, proxy and lifecycle

Tools shell **18760** proxies `/crop/` and `/dnb/` to owned backends **18768 / 18765**.
Both module documents use relative assets/API/image URLs, working standalone or prefixed
without response rewriting. D&B's inherited standalone resize request is suppressed inside
iframes. Same-sized mounted frames switch through visibility/inert/ARIA, not `display:none`,
navigation or recreation; image/preview/zoom/prompt state survives switching.

Startup locks serialize launcher clicks. Reuse checks package/interpreter/data/models/config,
PID and random instance, not simply an open port. The supervisor manages only children it
created with matching supervisor identity and private environment-only capabilities.
Standalone/unrelated servers are never adopted or stopped; port collisions fail visibly.
One unavailable module leaves the shell/other module accessible.

Module controls are `/api/modules/{crop|dnb}/{start|stop|restart}`. The exact loopback Host,
same-origin JSON, non-simple `X-SHP-Control: 1` header, fixed allowlist, bounded bodies and
confirmation types are checked. Owner routes/capabilities are not proxied or exposed.
Stop/Restart cannot override active API work or Crop generation. Loaded work needs confirmation;
the original instance and server-provided revision must still match. Other windows share
backend work, so stale/changed-work confirmations are refused. Restart replaces only its frame.
Only a never-ready owned startup child is eligible for forced startup cleanup.

The top-level shell owns browser leases: 20-second long polls, 90-second expiry,
15-second final-close grace and two-minute startup reservation. The desktop owns an independent
`desktop-<uuid>` lease from a Python thread, retained while hidden. The injected
`__shpOwnedDesktop` marker disables only that webview's redundant JS lease; it adds no native
privilege bridge. Other browser sessions keep independent leases. Direct backend pages do not
extend supervised lifetime. Manual module commands persist unless given `--close-with-ui`.

Last-session shutdown drains API/generation work before closing owned modules and ComfyUI.
Module children detect parent loss and finish work before exit. Suspension/crash/lease expiry
can shut down otherwise unused services; reopen the launcher. Only saved JSON/results persist.
Refresh/reload clears browser-only state; module stop/restart clears in-memory work for every
window using it. No automatic snapshot/recovery was introduced.

## Native desktop and tray

The native helper uses `/usr/bin/python3` and GI GTK3/WebKit2-4.1/AppIndicator3;
project uv Python stays separate. Bindings/display preflight opens no window or data.
It needs a graphical display and tray host; GNOME uses AppIndicator support.
The deterministic per-origin GApplication D-Bus ID guarantees single-instance activation.
GLib program name, GDK program class, icon name and desktop `StartupWMClass` are all
`shp-photocraft-tools`, matching the pinned desktop basename.

GTK3's actual Wayland `app_id` followed its program class, not the D-Bus ID: merely changing
`StartupWMClass` to the D-Bus name was insufficient. Actual protocol assertions now cover
the correct identity; the user confirmed one GNOME dash icon without repinning/favorites edits.
The intentional AppIndicator tray icon is separate. Its installed `shp-photocraft-tools-tray.svg`
uses the same square artwork in greyscale with a white outer border; the app/window SVG
remains coloured. The installer installs both and the launcher passes distinct `--icon` /
`--tray-icon` paths. Icon/source updates do not restart an existing desktop process.

X hides to tray; relaunch/Show presents the same mounted webview. No tray host/lost tray host
keeps or restores an accessible window. Show/Hide/Quit and Crop/D&B Start/Stop/Restart use
the existing supervisor API. HTTP/control workers run off GTK's main thread; UI/dialog updates
use the main loop. Epoch/pending-action guards prevent stale polls from overwriting controls.
Disconnected/transitional/busy actions are disabled; failures surface rather than force-retry.

Quit warns about unsent/view-state loss and rechecks busy state and work signature after
confirmation. Cancel, stale or busy work keeps the window. Other browser leases retain services.
If the supervisor is unavailable, closing only the desktop is explicitly confirmed without
force-stopping processes. Guarded Reload UI recovers renderer/load errors without service restart.
Native notifications overlay rather than resize the viewport.

Same-origin navigation/downloads only; popups, external navigation and browser device/
notification permissions are refused. Native prompt/confirm dialogs and Save chooser preserve
cancel/overwrite handling. Failed/canceled downloads are never reported as completed.

WebKit uses `<XDG_DATA_HOME>/shp-photocraft-tools/webview/{data,cache}` without moving old
browser profiles. Debounced atomic `window.json` stores normal sizes, not maximized/fullscreen.
Default GTK size **1399x949** includes the titlebar; WebView minimum is **363x626**.
X11 coordinates restore; Wayland placement remains compositor-controlled.

## External services and managed ComfyUI

One PhotoCraft tray line reports **Reachable / Unreachable (TCP)** without auth/document access.
Equal module endpoints share one probe, including failure results. Different endpoints show
an actionable mismatch; module auth/env compatibility remains independent.
Tools never starts/stops PhotoCraft for status.

Optional `comfy` launcher config and installer `--comfy-dir` select an existing runtime and
dedicated port **18788**; 8188 is disallowed. The watchdog launches that checkout's
`venv`/`.venv` Python, existing models/custom nodes, with isolated
`comfy-{user,input,output,temp}` state directories. No installation/download/adoption.
Occupied dedicated ports fail. Supervised Crop receives the managed URL; its retained startup
API is intercepted by the supervisor, and direct supervised calls cannot spawn a duplicate.

Linux `/proc/net/tcp` listener inode plus child `/proc/<pid>/fd` verifies actual ownership;
the real `/queue` supplies busy/readiness. Atomic instance-scoped status reports phases/errors
through the tray. Native Quit/lifetime protection includes owned queue work. Modules close first;
the watchdog drains the queue before stopping only its child, including after parent loss.
If an established queue becomes unreadable, shutdown conservatively waits and reports it.
Startup failures are visible and do not trigger a hidden retry loop.

Standalone Crop retains its original ComfyUI URL/start API behavior. Crop's web AI panel no
longer has a Start button/warning block; readiness retries remain, Generate is gated and genuine
model/node errors go to the main status bar. Independent ComfyUI processes remain unowned.

## Current UI and runtime files

Crop's method dropdown selects `flux2` (default/legacy fallback) or `flux1_fill`. Existing
FLUX.2 setting fields keep their meanings and saved values, including custom feather values.
Fill uses separate `fill_guidance`, `fill_steps`, `fill_megapixels`, `fill_feathering` and
`fill_unet`, defaulting to 30 / 28 / 0.5 / 64 / `flux1-fill-dev.safetensors`.
Presets include the selected method and both sets; loading an old preset selects FLUX.2
and fills newly introduced keys from defaults rather than inheriting the last selected method.

Fill follows the fully corrected C branch of the user-saved
`flux1_fill_postprocessing_comparison.json` (read-only source, not a runtime dependency).
Full Fill weights load via native UNETLoader with FP8 casting, CLIP-L/scaled FP8 T5 on CPU,
and `ae.safetensors`. Mask resizing aligns conditioning/statistics to generation resolution;
InpaintModelConditioning uses `noise_mask=false`; KSampler uses Euler/simple, CFG 1, denoise 1.
Always colour match, Lanczos resize to final dimensions, SeamColorCorrect and feathered original
paste-back. No raw/A/B comparison outputs, Turbo/Hyper LoRA or Nunchaku nodes.
Fill now optionally inserts a neural upscaler after colour matching, before exact-size Lanczos
resize/seam/paste-back. Its independent `fill_upscale` defaults to `lanczos` (no model);
the dropdown also lists ComfyUI's installed upscale models. Only a selected neural model
requires upscale nodes/weights. The LoRA/seam-toggle controls are hidden for Fill.
FLUX.2 likewise offers `upscale=lanczos` to bypass neural upscale nodes/weights while keeping
its existing neural model default; both methods still resize exactly before seam/paste-back.
Model/node readiness is method-specific and checks FP8/CPU loader capabilities. New env overrides are
`SHPC_FILL_CLIP_L`, `SHPC_FILL_T5`, `SHPC_FILL_VAE`. No live services are restarted by this change.
Fill also exposes `fill_clip_device` as CPU (default) or Default (GPU / auto), persisted
with settings/presets and used in DualCLIPLoader plus readiness validation. Default delegates
placement/offloading to ComfyUI; it does not promise VRAM fit. FLUX.2 is unaffected.

Fill validation passed eight focused unittest checks, eight existing Crop checks and the
three-test native desktop suite with its isolated WebKit smoke enabled. Coverage includes
legacy settings/presets, separate method values, graph connections/defaults, method-specific
missing-model/node errors, crop-before-upload, variation seeds/alpha, cancellation/failures,
real UI method switching/preset stars and retained loaded images. A static comparison also
matched the saved workflow's corrected C-branch links and parameter values. Installed Fill
loader schemas/model filenames were read without queueing work. No real generation or
PhotoCraft document operation was performed, so no integrated speed/quality claim is made.

Both modules show **Get Image / Send Result**, with original semantics/tooltips. Shared CSS
provides dark select/option styling, 74px labels, 42px readouts, 11px toolbar vertical padding
and preset buttons 1px shorter than their dropdown. Global real-checkbox switches are
**32x18**, grey off/blue on, with disabled dimming, keyboard focus and reduced-motion support.
IDs, label associations, checked state and settings/preset handlers remain.

Crop's empty instruction is removed; loaded dimension/margin text remains. Lowercase
`turbo lora` / `seam colour correction` rows precede model/upscaler selection, aligned right
with **zero inset**, matching D&B. Toggle rows have 2px vertical padding and compensated 6px
inter-row margin; model rows have 4px vertical padding.
D&B's four mask switches have no static "on" text; all six collapsible headers have no arrows.
Native header click/keyboard collapse and independent switch/picker behavior remain.
Shell tabs are centered, up to 260px each, shrink equally, and have no blue top stripe or
repeated web title. The native window retains its title.

Installer sources are `scripts/install_desktop.py` and `desktop/`; identical reinstalls are
safe, differing Tools files are refused. Desktop trust failures explain manual Allow Launching.
The installed wrapper uses this repo's `.venv` without network sync.
Current resource references are `~/.local/share/shp-photocraft-tools/dnb/models`,
`~/.local/share/shp-photocraft-tools/dnb/tools/Image-ExifTool-13.60/exiftool` and
external `/home/stef/ComfyUI`.
Config is `~/.config/shp-photocraft-tools/launcher.json`, with no credentials.
Module data remains `~/.local/share/shp-photocraft-tools/{crop,dnb}`.
Logs/lock and managed ComfyUI directories are under
`<XDG_STATE_HOME>/shp-photocraft-tools/`.

`SHPC_DATA` / `DNB_DATA` isolate standalone writes; `DNB_MODELS` / `DNB_EXIFTOOL` select
external resources. Supervised modules use launcher-owned data directories. Original Crop
`{presets,last}` / settings and D&B `{presets,default}` JSON schemas remain.
Module `SHPC_PC_*` / `DNB_PC_*` overrides take precedence over `SHP_PC_*`; existing OpenFill
token/exchange are read at connection time when configured. Browser keys stay module-prefixed
but origin/profile scoped. Do not import secrets or reuse original writable data for checks.
Disconnected control now reports `PhotoCraft not running or contollable (port <configured port>)`;
the spelling is user-requested. Authentication/token failures retain their separate diagnostics.

## Fresh-install prerequisites

[README.md](README.md#setup) is the current ordered setup path; [MODELS.md](MODELS.md)
records download locations, names, verification and offline preparation. Frozen source notes
are historical and may describe removed checkouts, earlier CSS dependencies or obsolete UI.

- Baseline `uv sync --locked` installs both modules' Python dependencies, including the GPU
  ONNX Runtime distribution. Optional `--extra gpu` adds user-space CUDA 13/cuDNN libraries,
  not an NVIDIA driver. Full CPU-only D&B is not guaranteed: Wholebody explicitly requests CUDA.
- Three manual D&B ONNX conversions have revision-pinned sources and full SHA-256 values
  verified against preserved binaries and upstream LFS metadata. No weights are bundled.
- rtmlib lazily downloads YOLOX-m HumanArt and balanced RTMW-x 256x192 on first analysis,
  independently of `DNB_MODELS`. Cache is normally `~/.cache/rtmlib/hub/checkpoints`,
  controlled by `TORCH_HOME` / `XDG_CACHE_HOME`. MODELS includes download-only prefetch and
  offline cache transfer instructions; downloader hashing is not enabled by default.
- Crop requires a separate ComfyUI PyTorch environment and inpaint/seam custom nodes.
  FLUX.2 additionally needs GGUF nodes and its encoder/VAE/GGUF/upscaler (plus Turbo LoRA
  when enabled); Fill instead uses native Fill conditioning and its own encoder/VAE/weights.
  Public model
  filenames/listings were checked without downloading weights. `SeamColorCorrect` is now
  copied unchanged into `comfyui/custom_nodes/seam-color-correct/__init__.py`, included in
  source releases explicitly through Hatch's sdist force-include. It is not in the Tools
  wheel; copy the package into ComfyUI's `custom_nodes`, then restart ComfyUI safely and
  verify `/object_info/SeamColorCorrect`. Readiness requires it even with seam correction off.
  Its docstring credits OpenFill's `quality.seam_field`; no separate license accompanied
  the local source and no new license grant is claimed. Original ComfyUI files are unchanged.
- ExifTool plus Perl is optional for Sony AF selection. Keep the full Perl distribution;
  copying its executable alone is insufficient.
- Native mode additionally requires system Python GI GTK3/WebKit2-4.1/AppIndicator3 and a
  tray host. Normal browser/standalone HTTP and `--no-browser` shell mode do not require GI.
  PhotoCraft integration separately needs its control/automation-capable build, token and
  permitted exchange paths; it is not installed/configured by Tools.

The documentation refresh checked metadata, local hashes, code contracts and document paths,
not fresh-machine execution, GPU compatibility or inference. No dependencies or weights were
installed and no services/windows were started or restarted for it.

The subsequent node-copy validation compared SHA-256/source bytes against the original,
checked node registration without torch in the Tools test suite, and built the source
release offline to verify exact inclusion without caches. A separate synthetic CPU tensor
check using the already installed ComfyUI torch verified constant-offset correction,
strength-zero bypass and unchanged inputs. It loaded no weights, started no ComfyUI server
and made no changes to the existing node/runtime.

## Validation and limitations

`tests/run.py` sets temporary data/model/token/exchange paths before imports, then runs eight
migrated Crop checks and the complete unittest suite. `tests/preview.py` and
`tests/shell_preview.py` use synthetic images, fake PhotoCraft, stub ComfyUI and fake model
results with real CPU D&B correction, on free ports. Tests neither infer on GPU nor mutate
PhotoCraft or start real ComfyUI.

Opt-in `SHP_DESKTOP_SMOKE=1` exercises actual GTK/WebKit against synthetic children: real canvas
readback/pointer capture/wheel zoom, generation/previews, retained iframe state, native dialogs/
downloads, geometry/localStorage across restart, hide/relaunch, tray controls, stale/busy/Cancel
guards, independent leases and final shutdown. It deliberately terminates only its own test
renderer to verify guarded recovery. Computed styles cover dark controls, all ten switches,
Crop zero-inset alignment and D&B arrowless header toggling/switch isolation.

Migration checks additionally verified wheel installation outside the checkout, both command
aliases, identical packaged CSS, usual/narrow layouts and unchanged source hashes/dirty D&B diff.
Real GPU/PhotoCraft integration was not repeated. Separately approved managed ComfyUI activation
verified runtime/custom-node startup and ownership with an idle queue, without inference.
Source notes retain algorithmic limitations: scale heuristics, tattoos/nostrils and nonidentical
Photoshop Soft Light. The inherited D&B preset toolbar can overflow around 344px width;
no unrelated responsive redesign was included.

Publication verification on **2026-11-05** passed **54 unittest checks with native smoke
enabled, plus eight migrated Crop checks**. Wheel/sdist builds contain all module/shared/
shell/native resources and the installer/docs. A local wheel installation outside the checkout
passed all five command help checks, four standalone command/alias HTTP and identical-asset
checks, and a full regression rerun (native smoke skipped only in that installed-wheel rerun).
The installed native helper's runtime preflight created no UI data.
Validation used only temporary services; no live app/window was reloaded or restarted for publication.

## D&B tau control

D&B exposes the existing `tau` value next to `edge`: linear 0.01–0.4, step 0.01,
default 1.0, with normal numeric direction. Positive legacy preset values outside that
range remain unchanged in state, requests, presets and the numeric readout; the handle sits
at the nearest endpoint until edited. Bounds never expand. Input uses existing
request/preset/dirty-star plumbing. Lower tau strengthens the existing
`exp(-(band/tau)^2)` deviation suppression; edge is a distinct guided-filter epsilon
(`edge^2`), where smaller values generally preserve weaker boundaries. No processing
or defaults changed.

## Skin tools in D&B

Tools navigation: Crop / Skin Mask / Frequency Separation / D&B, but only two service
keys (`crop`, `dnb`) and two mounted frames. Skin tabs route to the single D&B frame
using same-origin, frame-source-checked postMessage handshake and known mode/request
shapes. Commands queue until initial presets are ready; busy D&B defers to the latest
requested mode until idle. Recreated frames handshake again and honor the selected tab.
Preset/internal mode notifications synchronize shell ARIA/selected tab without stealing
the Crop view. Arrow keys cycle four tabs; skin tabs share one inert/ARIA-labeled panel.
All D&B badges reflect the same service status. Standalone selector stays visible until
the real parent handshake; unconnected embedding also retains it. Mode commands reuse
the same switch/dirty/recompute path, retaining mask, fetched identity and zoom.

Functionality-first mode selector: `dnb` (legacy/default), `mask`, `frequency`.
UI/API/presets carry `mode` and `frequency` method, calibration, Blur percentage and derived
pixel radii. Legacy presets select D&B; existing correction
settings are not reinterpreted. Shared mask control sections (skin/feather, structure,
light/tone, color) are visible only in Skin Mask mode; hiding never disables or resets
their shared values/on-off flags, and D&B/FS continue to use them. Active color/tone picking
is canceled on leaving Skin Mask mode without changing the picked values. Common analysis/S,
mask previews and whole-bundle presets remain available.
The preset star compares all saved settings except `mode` (navigation) and the S-derived
`frequency.radius`/`blur_radius`, so switching Skin Mask/FS/D&B or a new S does not mark a
preset unsaved, while any edit (including hidden shared mask settings) stars it in every mode.
Presets still store and restore the mode. Option tooltips sit on first-column labels only.
bands/strength/edge/tau are hidden outside D&B and do not affect mask/FS outputs.
`dodge_burn.combined_mask` is shared with the existing D&B pipeline without changing
its calculations. Standalone Send adds an RGB grayscale layer; it never replaces
the selected layer's mask. Local file loads clear the fetched-document identity.

FS calibration `frequency.radius_at_s181` defaults to 6, exposed as **Radius**
(0.5–30, step 0.1). Direct radius/Blur pixel sliders are removed.
On new load/Get, preset load or manual S/calibration/percentage change,
radius becomes `round(calibration * S / 181)`, clamped 1–100; current values appear in the Radius/Blur label tooltips.
Reuse full-precision S (including fractional estimates), no additional detection. Invalid/
missing S leaves the radius intact and requests manual scale input; processing already
rejects invalid S. **Blur** (`blur_percent`, 100–500, step1, default200) derives
`blur_radius = actual rounded/clamped radius * blur_percent / 100` (up to500px).
New API requests with percentage enforce both derived radii server-side from S.
Legacy requests without percentage retain explicit pixel values. UI legacy presets infer
percentage from saved Blur/radius, clamped to100–500; calibration defaults6 if absent.
Migration never writes preset files until Save. UI presets never restore stale pixel
radii over calibration. Gaussian remains OpenCV separable filtering, no 2D kernel allocation.

User revision: Color is ordinary full-image Gaussian / Median filtering, independent of
skin mask; Detail and Color are unmasked everywhere. OpenCV median uses WMF_OFF with
uniform full-image support (float medians internally quantized). Only extra Blur uses
normalized soft-mask Gaussian filtering of Color, excluding non-skin samples before
filtering and keeping Color outside support. Its combined skin layer mask feathers
opacity once; normalized support is not an opacity premultiplication.
Radius means full-image Gaussian sigma / median square radius (1–100 px);
extra Blur Gaussian sigma derives from it (up to500 px). Preview scales radii; full Send/Save do not.
Color is not opacity-premultiplied; Detail is `(source-Color)/2+0.5`, derived against the
actual Color. Native PhotoCraft Linear Light reconstruction was checked, not assumed.

Bridge assembles an unmasked Normal folder with top-down Detail (Linear Light, unmasked),
Blur (Normal, combined skin mask), Color (Normal, unmasked). Blur is active, not black-masked;
the former folder-mask / black paint-in design is superseded. The Result preview composites
the same RGB16-quantized stack and 8-bit Blur mask; hiding Blur reconstructs the full source.
The Blur preview pane displays Blur through its mask over Color (without Detail), not raw
unmasked Blur pixels. This display-only composition does not change Send/Save layer data.
It copies that single group to the destination in one history step. All RGB16
data gets the actual destination ICC without sample conversion, retaining the existing
D&B neutral fix. FS retains the pre-analysis native source values/profile, rejects
changed destination profile/size and non-opaque sources, and cleans temporary documents/
files on failure. No PhotoCraft live-document edits or live service restarts are needed
for tests. Reloading the HTML alone is insufficient for this feature: a normal backend/
Tools restart is needed to load the new Python; do not restart with unsaved loaded work.

Validation covers separation reconstruction and soft Blur masks in eight destination
profiles × 8/16/32-bit × both filters, grayscale
mask Send, one-step group insertion, stale-profile/size rejection and failed-copy cleanup.
The three-test desktop suite also passed with actual WebKit mode/control/Send/preset flows
and existing Crop Fill/incremental-result checks, using isolated synthetic fixtures only.

## Incremental Crop batch previews

Crop batches now publish each completed variation and preview before starting the next.
Polling appends tabs without resetting selection/zoom; the first available result opens
automatically, subsequent ones do not steal the view. Send remains disabled during generation.
Cancellation/failure retains completed results; only the unfinished variation is discarded.
Server selection survives later result publication and batch completion. This applies to
both generation methods without changing graph, seed sequence or PhotoCraft send semantics.

## Possible future feature: frequency-separated feathering

Brainstorm only; deferred by the user, not implemented or scheduled. Explore independent
feather widths for low-frequency tone/colour and high-frequency texture/detail during Crop's
final original paste-back. A wider tonal transition and narrower detail transition might
reduce visible colour joins without softening or doubling texture. A separation-radius
parameter would define the frequency split; resolution dependence, halos, ghosting and
misaligned generated structures need evaluation. Quality gains and processing cost are
unverified.

Suggested implementation: a separate ComfyUI blending node in the repo-managed custom-node
package, after colour/seam correction, shared by FLUX.2 and Fill. Crop would expose controls
and construct the graph. Keep existing single-feather blending as the default and initially
leave generation/Fill conditioning masks unchanged, isolating the final-composite experiment.
Do not modify the existing SeamColorCorrect implementation merely to add this feature.

PhotoCraft export needs explicit design: the current generated RGB plus one alpha mask may
not reproduce independent frequency blends. Verify that Send Result reproduces the preview
and preserves intended layer/editing semantics before enabling such a mode. Compare against
the current blend on identical generations rather than adding another AI pass.

## Possible future work: skin-tools performance on large images

Deferred by the user; not implemented or scheduled. Investigate responsiveness of Skin Mask,
Frequency Separation and D&B on ~5000×7000 (35 MP) images. Previews already work on a reduced
region (`get_region` caps the whole view at a 1400 px long side and crops at 1600 px); Send
and Save run at full resolution.

Profile first to find the actual bottleneck: first load/analysis and mask-component setup,
interactive slider previews, and Send/Save separately. Do not assume every filter runs at full
resolution, and do not promise a speed-up before measuring.

Candidate approach: cache a reduced working source and the mask components (skin, structure,
tone/colour inputs) once per image/region, and resize the FS source region once, rather than
preparing them repeatedly from the full-resolution image. Scale S, mm and pixel radii
consistently with the preview factor, as today. Final Send/Save must stay full-resolution,
using the original pixels with native ICC profile and 16-bit precision. Accept and document
that previews may differ in fine detail from the full-resolution result.

GPU processing is another possible future investigation, not an implemented feature.
Image filtering currently uses CPU NumPy/OpenCV; ONNX detection can use CUDA when available,
which is a separate acceleration path. A resident GPU pipeline could cover Gaussian and
normalized skin blur, mask calculations, D&B guided filtering and structure protection;
median separation is less straightforward. Benchmark end-to-end transfer overhead, device
memory use on 35 MP images, numerical/preview parity and contention with ComfyUI before
choosing a backend or promising gains. Keep native ICC/sample precision and full-resolution
Send/Save semantics unchanged; avoid repeatedly transferring intermediate arrays between
CPU and GPU. No GPU implementation or dependency change is authorized by this backlog note.

## Legacy-folder cleanup follow-up

The user subsequently requested preserving D&B models and local ExifTool before any old-folder
cleanup. The three ONNX files (21,608,081 bytes) and complete ExifTool 13.60 distribution were
copied to the D&B data paths above, retaining source files. Every copied file was SHA-256
verified against its source. The old `models/models` self-referencing symlink was intentionally
not copied. The reported 3.6GB was the whole D&B checkout, predominantly its 3.0GB environment,
not the model directory. No environment/results/private data were copied for this operation.
Launcher configuration now uses these copies; no live service restart or inference is required.
Other intentional dependencies are the existing ComfyUI runtime/models/custom nodes, system
GTK/WebKit/AppIndicator/Perl, this repo's uv environment and compatible OpenFill connection
configuration when present. Source repositories are provenance references, not runtime imports.
GitHub legacy-repository deletion was blocked by the reported missing `delete_repo` scope;
no successful remote deletion is established here. Do not retry without permission changes.
The coordinator subsequently reported removal of the old local source folders. Preserved
models/ExifTool and this repository no longer rely on those folders.
