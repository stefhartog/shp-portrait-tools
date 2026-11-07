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
| `shared/local_output.py`, `owned_process.py` | Exclusive numbered local saves; verified session identity/PID-descriptor escalation for explicit Force Restart. |
| `desktop/controller.py` | Standalone system-Python GTK/WebKit window, tray, dialogs, geometry and guarded Quit/reload. |
| `comfy.py` | Dedicated process watchdog, actual listener ownership and queue-aware shutdown. |

Original PhotoCraft clients were compared before extraction; compatible transport/session/
fetch/launch differed only by prefixes, config paths and labels, now subclass attributes.
Do not combine the incompatible result-application semantics. Original best-effort cleanup,
reconnect/retry and undo behavior remain; this is not a transaction redesign.

### Shared colour themes

Shared themes: `shared/ui/common.css` retains the original Current palette and
adds `:root[data-theme="photocraft"]` variables (neutral panels #515151, workspace
#272727, recessed inputs #444444, blue #6a9fc5). Component CSS consumes these for
workspaces/controls/chart backdrops without changing canvas data or image pixels.
`shared/ui/theme.js` is packaged/served at `static/theme.js` by shell and both
standalone servers. A blocking head script applies the saved palette before UI
render (PhotoCraft if no valid saved choice; explicit Current is retained);
DOMContentLoaded adds one selector at shell-header right or before standalone
presets. Embedded tools have no duplicate selector; an origin/source-checked ready
handshake reapplies the shell theme after frame creation/reload. localStorage key
`shp-tools-theme` is per browser origin; storage events sync same-origin windows.
Separate standalone ports retain independent preferences. Storage failures warn in
console/selector tooltip. No processing/preset state changes. New static routes need
a normal Tools/backend restart before refreshing UI; do not restart automatically.
Selected shell/tool tabs use neutral borders with bottom borders matching their
background, not blue selection borders. Pane headers own the single baseline; tab
bottom borders overlap it (the same mechanism as shell tabs), and the pane below has
no second top border. Bottom alignment keeps tab edges connected even beside taller
header controls; wrapped controls sit above the tab row so tabs still touch the pane.
No shadow seam patch. Keyboard focus remains a separate blue outline.
Main workflow buttons (`xGet`, `xSend`, `pcGet`, `pcSend`, `loadBtn`, output `save`)
use `workflow`: shared Generate/primary colours only when enabled. Existing primary
Generate/Apply/Apply scale use the same subdued unavailable treatment; primary/workflow share enabled
hover/active/focus states. Preset and other utility buttons are unchanged.
Primary/workflow font-weight600 is constant across states; fixed shared Get/Send/Crop Generate
slots (`calc(11ch + 26px)`) prevent status/geometry shifts. Padding/borders stay constant,
disabled hover never changes colour, and Crop Cancel retains its red styling.
Shared button/tool-tab and shell-tab hover selectors exclude `:disabled`, so
unavailable controls keep the same background/text on mouseover in both themes.
Shell tabs have no status badges/slots: busy/starting/stopping subtly darken only the
title via aria-busy, without changing backgrounds, borders or dimensions. State/error text
stays in tab title/aria-label. Lost supervisor polling marks status unavailable.
Top-bar status uses a muted-blue indeterminate background during operations, an accessible
live region and a static reduced-motion treatment. Symmetric grid side columns keep it
screen-centred; narrower layouts use full-width rows. Standalone theme controls sit inside
the preset group. The completion checkmark retains --status-accent #6a9fc5.
FS + Skintone has no help/summary paragraph below its hue graph. Empty-sample,
unsupported-source and correction-effect diagnostics remain in Log; genuine errors
still use existing top-bar handling. Graph drawing/controls/tooltips are unchanged.
Right preview tabs: frequency Color/Detail, dnb Layer/Layer+mask, mask Mask/Analysis.
`syncRightTabs` validates remembered/current choice on initial sync and every mode
change, falling back to the first valid tab. Blur has no UI tab.
Skin Mask hides the left Result/Skin mask tab and falls back to Original on initial
sync/mode changes; other modes retain Original/Result. Right Mask/Analysis unchanged.
Preview math/encoding remain unchanged; cached revisits avoid recomputation as described below.
Shell DOM/keyboard order is mask/frequency/dnb/crop; initial selection is Crop unless its
startup preference is disabled, then the enabled skin service is preferred. Canonical mode IDs
are unchanged and manual selection is not overridden on subsequent polls.

### Preview reuse, Crop controls and local outputs

Skin preview cache retains at most one latest entry per real skin mode within a192MiB
budget. Keys include image generation, exact region/scale/center, mask identity and effective
settings; inactive parameters are normalized rather than spuriously invalidating results.
Cached region arrays are copied to avoid retaining full-source views. New Load/Get and
landmark edits invalidate entries. Cache hits restore images/state/stats, update versions and
re-render changed analysis flags without rerunning the processing pipeline. Full-res Send
does not reuse these reduced previews. Mode switches keep panes mounted and apply quietly;
a150ms feedback delay avoids flashing for fast work, with input interception during that
interval and normal busy state afterward. Generation/epoch/settings guards reject stale
responses. Cache restoration is explicitly logged, not timed as new processing.

All action labels are **Send Result**. Crop Generate now lives in the static CROP panel
beside evenly stretched1/2/3 ratio-style variation choices with aria-pressed state. No variation
slider remains; localStorage `shpx-var` and the existing1–4 API domain remain compatible,
including an explicit legacy-count4 note until selection changes. AI SETTINGS is an arrowless,
default-open details/summary; values remain mounted on collapse, dropdowns precede sliders.
Crop info between ratios and Generate shows a three-column resolution row and spatial signed
top/left/right/bottom shifts, accessible directional units but no visible labels/px suffixes.
Zero placeholders keep the empty readout mounted. Info inset is `clamp(16px,10%,40px)`,
shift-only inset60px per side,16px above shifts and20px before Generate.

`EXPAND_LIMIT = min(999, int(SHPC_LIMIT))` with default999 caps each positive requested
outpaint margin in ORIGINAL image pixels, not inference padding, final size or summed margins.
Backend frame validation rejects1000 per side for either graph; negative crop is unrestricted.
Ratio/free drag constraints now respect the same cap, including previous base-width/2px snap
loopholes; a ratio may require cropping/shrinking instead of larger outpaint.

Owned desktop injects a same-origin `__shpNativeFileDialog` capability and handles the explicit
`SHP_OPEN_IMAGE_V1` prompt with GTK's Open chooser only from the owned shell. Get checks
PhotoCraft connection and chooses fetch vs native local Open; cancel leaves current state alone.
Standalone browsers use existing native-path/list controls, not upload/download path fiction.
Successful sources carry `source_kind` local/photocraft; Send routes by that provenance even
after connection changes. PC disconnect is an explicit error, never a save beside exchange.
`/api/send-result` requires same-origin JSON; the verified shell proxy supplies child Origin.
Image generation/sequence/result-selection guards remain.

Local sends share `shared/local_output.write_local_result`: directory-fd-relative exclusive
`O_CREAT|O_EXCL|O_NOFOLLOW` numbering, load-time parent identity check, fsync and failed partial
cleanup. No source overwrite, layer bundle or sidecar. `<stem>_shpdnb_NNN.png` is full-res
D&B composite RGBA16 with processing ICC/sRGB; `_shpfs_` is full-res FS+Skintone composite
RGBA16 with source ICC/sRGB; alpha is opaque. `_shpskinmask_` is grayscale8.
Crop `_shpcrop_` exports only the selected final full-res RGB8 canvas, retaining existing sRGB
input conversion and no new output ICC embedding guarantee. D&B full-res calculation was
extracted into one helper shared by old Save/native Send/local Send, without changing math.

Crop AI SETTINGS has a utility **Free Comfy memory** button. Same-origin `/api/comfy/free`
checks local generation and configured Comfy `/queue` running AND pending lists (5s timeout),
then POSTs `/free` with `unload_models:true,free_memory:true` (10s). Invalid/unreachable queue
fails closed; no arbitrary URL, cancel or process kill. Installed Comfy sets asynchronous flags
and returns empty200; worker processes them between prompts. Success therefore says only
“Memory release requested”. Tooltip warns about next-generation reload and incomplete
RAM/VRAM release. User request authorizes implementation, not automatic live invocation.

### D&B numeric Soft Light transfer

Feedback layout: Image analysis (`g-file`, including S/landmark controls) is Skin Mask
only. `renderAnalysisSummary` uses textContent and current shared state: image name/size,
known colour handling, detected confidence/mask, S estimated vs overridden, Hue/light/
color midpoints and source/center warnings. It runs on load, settings dirtiness and mode
sync, never inference. Successful Apply/Send says "Finished processing in X seconds."
(one decimal), using performance.now elapsed time. Initial Apply uses Load/Get's
original start through preview image load events; later Apply uses its own start
through those events. Send measures its request through cleanup completion, not a
sum of backend stages. This does not measure paint. Correction/no-op/center details
go to the bounded in-memory log, genuine Hue Center failures remain errors.
`details#temporaryLog` is a collapsed Log subsection inside Image analysis, Skin Mask
only. `renderTemporaryLog` displays current log state with textContent; no download
button or Blob/file-opening code. Load/send live snapshots and archived notes
(last100 lines) stay in JS memory; reload clears them, switches retain them.

Load/Get stage timings use `perf_counter`: `timings.loaded` is decode/normalization,
alpha/ICC preparation and colour conversion; Get adds its measured bridge fetch/export.
`timings.analyzed` isolates `pick_target`; `timings.mask` isolates shared `skin_mask`
and initial skin statistics, or null when no usable detected S. `seconds` is now the
whole backend request work, not the former detection+mask-only partial timer.
UI temporary log memory retains these and browser-measured Hue Center/preview round trips,
visible-pane image load duration, and total elapsed Load/Get-to-ready. `showImages`
resolves after both pane load events (or false on error/replacement); ready is never
reported on failure or before those events. This does not measure final screen paint.
Long request handlers remain synchronous but publish lightweight progress snapshots.
`GET /api/progress?token=...` bypasses the image-processing LOCK on ThreadingHTTPServer,
so polls can read actual current/completed stages during computation. Request-scoped
ContextVar tokens isolate nested processing/bridge calls; no processing is repartitioned.
UI polls serially every250ms only while its operation is outstanding and rejects stale
token responses. Final snapshots travel in success/error responses; records are consumed
then, with bounded16-record/10-minute completed-record expiry for disconnected callers.
Load/Get distinguishes PhotoCraft fetch, decode/profile preparation, detection/S and mask.
Send instruments actual FS split/Blur calls, D&B/mask preparation, raster encoding/import,
Hue/mask attachments, assembly/final copy and cleanup. Command completion, not paint,
is measured; fast stages may only appear as completed. Failed stages retain durations.
No temporary GUI/native apps are needed for focused progress validation.
Missing mask/center/S and failures are explicit; failed new Load/Get does not auto-Apply
the previous image. Generation/settings guards remain. Summary survives later Apply.

Isolated `photocraft-cli 0.18.0 (8a87a457a)` tests established that
`layer.copyToDocument` colour-converts RGB layer samples between document profiles.
The old untagged RGBA16 correction opens as sRGB: its neutral code 32768/65535 becomes
approximately 0.49612 in Adobe-RGB-compatible, 0.42467 in ProPhoto-compatible, and 0.21405
in linear-sRGB/ACEScg destinations. sRGB and Display P3 preserve that gray. The problem
is numerical blend-data conversion, not the D&B calculation or the source file extension.
This reproduces a Send-only darkening mechanism; the original problematic RAW/profile
has not been examined, so it is not proof of that particular file's profile.

Normal Send reuses the actual ICC bytes already captured by Get, then inserts only an
`iCCP` chunk into the OpenCV RGBA16 scratch PNG.
This assigns the destination space without changing any correction samples; there is no
guessed gamma, sRGB substitution or builtin-profile name mapping. PNG metadata insertion
avoids Pillow's RGB16 writing limitation and the automation API's refusal of filesystem
ICC paths in `edit.assignProfile`. The mask PNG stays separate and untagged. Failure to
obtain an ICC profile refuses Send before copying a layer rather than falling back.
For D&B/Skin Mask callers without a captured ICC only, the previous temporary merged
destination export remains as a safe fallback; FS + Skintone fails explicitly without
captured ICC. No user's document is converted or saved.

User explicitly accepted assuming the destination profile stays unchanged between Get
and Send. Name/size lookup remains; Get must be repeated after changing/replacing/
converting the destination. Disposable probes of CLI0.18 and desktop0.20.2 found no
stable document-incarnation ID or exact ICC bytes/hash in metadata: `edit.profileInfo`
returns descriptive fields/byte count, not a fingerprint, and saved native layer IDs
survive reopening. Undetectable profile/replacement changes are an accepted limitation,
not an exact profile-safety guarantee. Old servers with Get-exported ICC use the same
fast path; their version alone does not require the redundant profile export.

Transfer memory: source documents are closed immediately after copying layers or
attaching masks; only assembly plus one temporary source are retained. Sources always
open above the assembly/target indices; `_release_source` closes the highest managed
index and removes it from final cleanup, so lower indices cannot shift from these
early closes. Cleanup still runs highest-first on failure; failed early close propagates
and final cleanup retries. One final target copy/undo and layer topology are unchanged.
`skintone.separate(native=True)` replaces float layers one at a time with the identical
RGB16 arrays, avoiding the export float round-trip and simultaneous three-float/three-u16
dictionaries. D&B releases unused composites/diagnostics and gray conversion buffers
before import. Load releases decoded alpha-check data before analysis, normalizes into
one float allocation, and releases raw data as soon as the analysis view is ready.
RGB16 conversion bounds clip/scale/round temporaries to64 rows with identical arithmetic.
RGBA PNG encoding allocates alpha in-place; iCCP assembly joins encoded views rather
than copying entire PNG byte slices. RGB16 samples and output bytes are unchanged.
Remaining costs: Get's merged analysis export, full-res algorithms/native source arrays,
PNG files/import, the required assembly, and the final cross-document group copy.

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

Module controls are `/api/modules/{crop|dnb}/{start|stop|restart|startup}`. The exact loopback Host,
same-origin JSON, non-simple `X-SHP-Control: 1` header, fixed allowlist, bounded bodies and
confirmation types are checked. Owner routes/capabilities are not proxied or exposed.
Stop/Restart cannot override active API work or Crop generation. Loaded work needs confirmation;
the original instance and server-provided revision must still match. Other windows share
backend work, so stale/changed-work confirmations are refused. Restart replaces only its frame.
Only a never-ready owned startup child is eligible for forced startup cleanup.
Intentional Stop is unloaded, not a supervisor failure. Late child exit settles `stopping`
to `stopped`; stopped snapshots are not busy/loaded. Leftover iframe lifecycle close/heartbeat
get an unmanaged acknowledgement, while other stopped/stopping routes return explicit409
`module_stopped`/`module_stopping`. Real failures retain error/unavailable states.
Shell removes stopping/stopped frames, keeps other frames and shows Start guidance; D&B's
three tabs share ONE service. Module controls also reject whole-supervisor draining admission.

`startup` maps real service names to strict booleans in launcher.json; absent entries default
true. Tray **Load on startup** changes only that preference, leaving current work intact.
Supervisor persists under a file lock with same-directory temporary/fsync/atomic replace,
then updates its config; failure does not masquerade as success. Manual Start does not enable
startup implicitly. All-disabled still serves shell/tray. Owned Comfy starts when Crop starts,
not in the unconditional supervisor startup path; Stop Crop alone does not stop Comfy.

The top-level shell owns browser leases: 20-second long polls, 90-second expiry,
15-second final-close grace and two-minute startup reservation. The desktop owns an independent
`desktop-<uuid>` lease from a Python thread, retained while hidden. The injected
`__shpOwnedDesktop` marker disables only that webview's redundant JS lease; it adds no native
privilege itself. The separate same-origin chooser capability is described above.
Other browser sessions keep independent leases. Direct backend pages do not
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
Last native Quit calls acknowledged `/api/ui/native-quit`, atomically sets draining before
lease release and skips browser15s grace. Heartbeat/admission serialization and launcher
reserve409/draining wait prevent rapid reopen from reviving old code. Quit with other leases
closes only this desktop; normal Restart refuses other active windows and waits old supervisor
and controller identities to exit before a fresh launcher handoff.
Explicit **Force Restart Tools** warns of ALL-window processing/unsent loss and interrupted-Send
scratch/exchange remnants. It pins only launch-verified module/owned-Comfy session members
through Linux PID descriptors, rechecks identities, then per target allows3s graceful,
3s after TERM and2s after KILL (plus control/lock overhead). No name-based/process-wide kill,
PhotoCraft/external Comfy target or result/inbox deletion. Partial cleanup/identity failures
leave visible draining/error state, not duplicate replacements; same initiating controller can
explicitly confirm a Force retry after the failed cleanup worker has ended. Late session
members also block replacement. Normal strict cleanup never force-kills active work;
late busy drain/managed-Comfy waits have30s failure bounds and controller wait is90s.
Timestamped admission/cleanup diagnostics go to shell.log; restart helper output goes to
webview/restart.log. These bounds are not promises of immediate process exit on an unhealthy OS.
If the supervisor is unavailable, closing only the desktop is explicitly confirmed without
force-stopping processes. Guarded Reload UI recovers renderer/load errors without service restart.
Native notifications use a non-expanding GTK row above the expanding WebView, not Gtk.Overlay:
the old overlay covered/captured the module tabs after Stop/Start. Ordinary info/error notices
auto-dismiss after1000ms, with timestamped desktop.log retention. Cancelled timers plus generation
guards prevent old callbacks from clearing new notices; repeated monitor text does not flash
again. Destroy/shutdown cancels timers. Whole-shell connection loss, pending/failed shutdown and
reload-required renderer errors keep actionable guidance/controls. No confirmation dialog,
destructive decision or chooser is auto-dismissed.

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
When Crop is disabled at startup, the watchdog is not launched just for skin tools.

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

Crop's empty instruction is replaced by the zero readout described above. Lowercase
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

The post-`be7e12d` changes above were checked incrementally with focused synthetic/local
tests: preview-cache keys/invalidation/memory bounds; local routing/chooser cancel,
exclusive numbering and decoded output depths/profiles; Crop999/1000 geometry and variation
state; mocked Comfy `/free`; fake-clock/process lifecycle/restart escalation and PID reuse;
module isolation/startup persistence; headless JavaScriptCore markup/state/polling; and
native notice fake timers/GTK layout structure. Python syntax and diff checks passed.
This publication does not rerun broad suites, launch GUI/PhotoCraft/Comfy, issue live `/free`,
restart services or mutate live settings/documents. Native overlay correction is supported
by the supplied screenshot and GTK packing structure, not a new live visual test.
Already-running Python/controller code needs a complete later user-directed Tools restart;
refresh alone can load new HTML against stale routes. No speed/quality/inference or
fresh-machine validation is implied by these focused checks.

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

Eyebrow exclusion/analysis polygons follow the lower mesh chain outer-to-inner, then the
upper chain in reverse. Both `skin_mask` and the comparison/demo path use this perimeter;
concatenating both chains in their original direction creates crossed closing diagonals.
Eyes, lips, landmark inference and feature dilation/feathering are unchanged.

Manual scale fallback: **Adjust scale landmarks** in Image analysis opens a full-image
Original overlay with explicit subject selection. Detected points remain immutable;
per-image/per-subject overrides support eye centres and body joints, missing points,
excluded body segments, drag/nudge, undo/reset and Apply/Cancel. The proposal replaces
edited eye cues, recomputes one body cue and retains unaffected automatic evidence.
Manual proposals cap high confidence at medium and retain at least 15% assumed uncertainty;
2D foreshortening and population-average anatomy remain limitations. Apply uses the normal
S-change path, including derived FS radii. New Get/load clears overrides; presets exclude
them, and stale image generations are rejected. This is scale-only: it does not regenerate
segmentation, change the mask target or move the subject crop. When no automatic scale/mask
was available, manual scale enables processing but does not create a missing skin mask.
The numerical S override remains available. Python/API changes require a normal backend/
Tools restart; a page reload alone is insufficient.

Tools navigation: Crop / Skin Mask / FS + Skintone / D&B, but only two service
keys (`crop`, `dnb`) and two mounted frames. Skin tabs route to the single D&B frame
using same-origin, frame-source-checked postMessage handshake and known mode/request
shapes. Commands queue until initial presets are ready; busy D&B defers to the latest
requested mode until idle. Recreated frames handshake again and honor the selected tab.
Preset/internal mode notifications synchronize shell ARIA/selected tab without stealing
the Crop view. Arrow keys cycle four tabs; skin tabs share one inert/ARIA-labeled panel.
All D&B badges reflect the same service status. Standalone selector stays visible until
the real parent handshake; unconnected embedding also retains it. Mode commands reuse
the same switch/dirty/recompute path, retaining mask, fetched identity and zoom.

Functionality-first mode selector: `dnb` (legacy/default), `mask`, `frequency` (FS + Skintone).
Legacy `skintone` API/preset/parent-message modes normalize to `frequency` in memory;
no separate visible tab or backend remains. Saturation correction fields are ignored by
processing but retained verbatim by preset load/Save, without automatic file rewriting.
UI/API/presets carry `mode` and `frequency` method, calibration, Blur percentage and derived
pixel radii. Legacy presets select D&B; existing correction
settings are not reinterpreted. Shared mask control sections (skin/feather, structure,
light/tone, color) are visible only in Skin Mask mode; hiding never disables or resets
their shared values/on-off flags, and D&B/FS/Skintone continue to use them. Active color/tone picking
is canceled on leaving Skin Mask mode without changing the picked values. Common analysis/S,
mask previews and whole-bundle presets remain available.
The preset star compares all saved settings except `mode` (navigation) and the S-derived
`frequency.radius`/`blur_radius`, so switching skin tools or a new S does not mark a
preset unsaved, while any edit (including hidden shared mask settings) stars it in every mode.
Presets still store and restore the mode. Option tooltips sit on first-column labels only.
bands/strength/edge/tau are hidden outside D&B and do not affect mask/FS/Skintone outputs.
`dodge_burn.combined_mask` is shared with the existing D&B pipeline without changing
its calculations. Standalone Send adds an RGB grayscale layer; it never replaces
the selected layer's mask. Local file loads clear the fetched-document identity.

D&B-only `layer_blur_at_s181` defaults0; **Layer blur** slider0–20, step0.1,
independent of FS. Full-image Gaussian sigma is value*S/181 without rounding/clamping
the derived pixels; preview multiplies by region scale. `blur_softlight_layer` applies
BORDER_REPLICATE Gaussian to unmasked cs, then Soft Light and the unchanged combined mask.
Zero returns original cs/after untouched, preserving legacy output byte-for-byte; positive
blur uses actual layer opacity rather than re-inverting a masked luminance change.
Preview Layer/Layer+mask/Result, Save and ICC-safe Send share this postprocess. Presets
store only calibration; missing legacy field defaults0 without writes, S changes update
tooltip pixels but not preset star. Other modes never run the postprocess.

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

The combined bridge stack described below adds Hue above Blur and below Detail.
Blur is active, not black-masked; the former folder-mask / black paint-in design is
superseded. The Result preview composites the same RGB16-quantized stack with separate
8-bit Blur/Hue masks; disabling both corrections reconstructs the full source.
The Blur preview pane displays Blur through its mask over Color (without Hue or Detail), not raw
unmasked Blur pixels. This display-only composition does not change Send/Save layer data.
It copies that single group to the destination in one history step. All RGB16
data gets the actual destination ICC without sample conversion, retaining the existing
D&B neutral fix. FS retains the pre-analysis native source values/profile, rejects
changed destination size, missing captured ICC and non-opaque sources, and cleans temporary documents/
files on failure. No PhotoCraft live-document edits or live service restarts are needed
for tests. Reloading the HTML alone is insufficient for this feature: a normal backend/
Tools restart is needed to load the new Python; do not restart with unsaved loaded work.

Validation covers separation reconstruction and soft Blur masks in eight destination
profiles × 8/16/32-bit × both filters, grayscale
mask Send, one-step group insertion, missing-profile/size rejection and failed-copy cleanup.
The three-test desktop suite also passed with actual WebKit mode/control/Send/preset flows
and existing Crop Fill/incremental-result checks, using isolated synthetic fixtures only.

### FS + Skintone: combined separation, Blur and one editable Hue adjustment

Current UI is three panels: Frequency Separation (method/radius only, no divider/toggle),
Blur (the former `frequency.on` toggle, Radius, Strength and independent Skin mask),
Skintone Correction (`skintone.on` and existing Hue controls).
`frequency.on=false` skips ONLY added Blur; Color/Detail always run. New settings are
strict-boolean `blur_mask=true`, finite `blur_strength=100` in0–100 and strict-boolean
`skintone.on=true`. Existing presets default enabled without rewriting.
Unmasked Blur uses whole-image sampling AND no layer mask, not just mask removal.
Correction off skips Hue Center estimation, LUT/HSV/distribution work and the Hue layer.
Preview/local results blend full-strength Blur and Hue through their masks and strengths.
Native Send uses full assets plus normalized `layer.setProps.opacity=strength/100`;
Save assets are full strength with an explicit recipe. Isolated CLI0.18.0 verifies raster/
colorLookup opacity and exported composites at0/partial/100. Default100 stays unchanged;
partial Hue Amount intentionally changes from inside-LUT interpolation.

`median_fast` retains the rounded full-image radius but uses uniform uint8 OpenCV
`medianBlur`, with at most eight radius-halo row stripes and row-bounded conversion
temporaries. Detail uses original float source minus actual Color; final assets stayRGB16.
Quantization/borders intentionally differ from original Median, which remains unchanged
alongside Gaussian. Preview, native/local Send, Save, validation and presets are wired.
Native-test UI/handler/route are removed; the isolated low-level benchmark helper remains.

`frequency_transfer` stores png/tiff plus strict booleans compressed/opaque_alpha/parallel;
defaults png/true/true/true. PNG level0 uses stored Deflate; TIFF classic IFD preserves
RGB16/ICC with none or ZIP/Deflate and optional unassociated alpha, subject to4GiB.
Mask PNG8/local encodings are unchanged. One Detail encoder overlaps request-thread work;
native control is serialized, futures joined before cleanup and final copy is non-replaying.
Real-engine tests cover eight codec variants and serial/parallel folder parity.
User live evidence found11–13s for all combinations and similar~1GB PSDs after two sends:
do not describe TIFF as a verified live speedup.

`send_skintone_parallel` overlaps final-mask preparation with Color/Detail and extra Blur
with raster preparation/import. A read-only `(total_mask,structure)` cache capped256MiB
uses source/content/profile/component signatures and effective mask/S settings, shared
across normal modes only when identical. `dodge_burn.process(mask_result=...)` avoids
duplicate final-mask work without using that final mask for base change-band analysis.
One separate precache worker waits600ms after settled preview, coalesces edits and rejects
stale publication. Its localStorage-only checkbox and cross/spinner/check status are only
in Skin Mask > Image analysis, alongside transfer controls labelled FS + Skintone only.
Status uses actual retained cache readiness, including entries filled by Send; polling
does not hash full images or retain arrays. Source load resets cache; over-budget arrays
are not retained. Forced overlap increased preview time20–24% and parent RAM1.53→2.83GiB;
precache remains optional. Synthetic32.7MP/r15 warm-mask Send averaged Median28.864s vs
Fast8.741s, not a live-image guarantee.

Commit boundary excludes the separate uncommitted Blemish runtime, ClearFrame evaluation,
dataset scripts/data and desktop changes. Unknown/stale UI modes normalize to D&B; no
Blemish tab is exposed in this committed snapshot. The working prototype is not removed.

User combined the two workflows and removed saturation correction. `frequency` is the
canonical mode; `skintone` remains an input alias only. The existing `frequency` and
`skintone` preset fields remain distinct. Hue settings default to center30°, half-width30°,
softness5°, amount100%; UI windows remain Center10–50° and Range1–8°. Legacy hue values
outside the UI windows keep their readout/value until edited. Backend accepts width0.
Legacy `sat_*` values are preserved as inert preset data, never applied or rewritten
on load; saving hue edits retains those fields. There is no saturation control/chart,
Saturation LUT generation, application or export in the combined workflow.

`frequency_separation.separate` retains full-image Gaussian/median Color, unmasked
Detail and skin-aware normalized Gaussian Blur. Shared Radius/S and Blur% retain their
existing scaling and legacy pixel semantics. `skintone.separate` quantizes these to the
actual tagged RGB16 raster layers. Preview computes Color + quantized-mask Normal Blur,
samples the Hue LUT on that base, feathers Hue with the same mask once, then restores
unmasked Linear Light Detail. Blur and Hue each have their own mask; neither is applied
twice. Hue does not act on original or Detail. Outside the mask the original reconstructs
within rounding. Hue Amount0 leaves enabled Blur active; disabling both effects produces
the Color/Detail reconstruction. Final detail recomposition can clip endpoints.
Reduced-view radii scale with resolution; full-resolution Send/Save uses native source.

Bridge assembles an unmasked Normal `SHP FS + Skintone` folder, top-down:
Detail (Linear Light, unmasked) / Hue (Normal native adjustment, combined skin mask) /
Blur (Normal, combined skin mask) / Color (Normal, unmasked). Hue is directly masked,
with no correction subgroup: one native Normal `colorLookup`, trilinear, dither off,
even at zero Amount. One mask file/document supplies both selection attachments;
the native Hue and Blur layers each own an editable mask. Empty masks use
native `layer.layerMask.hideAll`; `revealSelection` rejects empty selections.
Direct-vs-former-subgroup RGB16 comparisons cover empty, soft and full masks:
results can differ by up to two16-bit code values because
the native subgroup's intermediate rounding is removed. Preview math is unchanged;
native profile/depth comparisons remain within the existing tolerances.
Captured-ICC presence/size guards and scratch cleanup remain; one final group copy creates
one target undo step. `.pcraft` persists the native LUT and raster/mask hierarchy.
`layer.setAdjustment` uses flattened parameters (`data`, `fileName`, etc.).

Hue uses a circular identity core with C1 exponential shoulders. Neutrals/near-gray cube
cells are protected. The analytic hue transform preserves HSV S/value; subjective
saturation/brightness changes are not a promise of numeric S/value correction.
Cached33³/six-decimal LUTs fit the1MiB request limit and are sent inline because ambient
file-backed LUT automation is sandbox-disabled. Trilinear sampling, partial Amount,
mask feathering and near-neutral protection mean output hue is not strictly bounded.
LUTs come from settings, not learned pixels. Native RGB/ICC space is preserved numerically.

The single hue chart compares uncorrected Color with Color + masked Blur + masked Hue,
not the detailed original or recomposite. Shared-mask-weighted10/50/90% statistics use
16 original-Color HSV-value bins and up to~100k samples; hue axis10–50°, off-window
triangles, measured median-colour strips. S is still measured for those strips but
is neither a correction target nor a separate graph. Effect stats measure actual changed
base samples, including Blur. Empty/neutral-only masks and Hue Amount0 are explicit.
Color/Detail panes show unmasked data; Blur shows masked Blur over Color before Hue;
Result restores Detail after both corrections.

Manual Save writes `Hue.cube`, tagged RGB16 Color/Blur/Detail/result, skinmask and recipe.
Opaque/SDR guards remain; genuine extended range blocks the combined mode while D&B and
Skin Mask remain available. No clipping/tone mapping is introduced. Settings-revision
queuing prevents stale slider previews. Shell/standalone selection and old presets share
the same image/mask/preset state. Native matrix coverage includes eight profiles×8/16/32-bit,
soft/empty/outside masks, Blur-only/hue-only/combined/neutral cases, distinct textured
outliers, unmasked Detail, persistence/editing, one undo and failed final-copy cleanup.
A normal Tools/D&B backend restart then Get is needed; HTML reload alone is insufficient.
Activation is a separate user-directed step; this commit does not restart Tools or mutate
live PhotoCraft documents.

Hue Center load fix: the old `skin_colour` estimate initialized only Color Range/tone
controls, never Skintone Center; the FS merge did not remove prior Center initialization.
Load/Get now calls `/api/hue-center` once after applying new image metadata and shared
mask defaults. It computes a mask-weighted circular mean from native uncorrected Color,
not the corrected image/LUT/previous frame; omit HSV saturation<.05 and V<=1/255, reject circular
resultant<.1. Uses automatic S/current FS settings and a whole-image1400px working view.
Missing masks/scale or unsupported/empty/ambiguous samples produce explicit retained-
Center notes. No fallback whole-image hue without skin detection. New image generation
and UI load epoch/settings revision reject stale estimates or manual/preset edits during
initialization. Apply/mask/tab changes never auto-reset Center. Presets intentionally
restore Center; the next Load/Get re-estimates only Center, with no preset file writes.
Actual values outside10–50 remain visible, never silently clamped. Source/ICC untouched.
Validation:113 isolated repository tests pass (one opt-in desktop skip), plus3 actual
WebKit tests covering two different skin-colour Loads, Get re-estimation, manual/preset
overrides, delayed/stale results and out-of-window/empty estimates. Circular-wrap,
neutral/black exclusion, empty/ambiguous evidence and stale server generations are covered
without real inference or user document changes.

Hue Center runtime follow-up: confirmed an old running D&B process (started before the
Python endpoint addition) serving new HTML from disk. Its missing `/api/hue-center`
returns HTTP404 `text/plain` / `not found`; WebKit `Response.json()` reproduces the exact
`SyntaxError: The string did not match the expected pattern.` This is not hue/mask math
or a supervisor allowlist defect: the proxy forwards all non-private `/api/` paths.
The browser API helper now reports HTTP status/content type for non-JSON responses and
specifically explains that missing Hue Center endpoints require a Tools/D&B backend
restart then Get, not just page refresh. It retains the prior Center, never invents one.
Real proxy/direct-child route integration and WebKit old-response/current-route cases
cover the failure and recovery; all3 desktop tests pass. No running user service was
restarted or sent a processing request to diagnose this mismatch.

Combined-workflow validation: isolated repository wrapper passed111 tests (one opt-in
desktop skip), and the actual WebKit suite passed3 separately. Native matrix max RGB
errors were0.00611526 /0.00002163 /0.00000757 for8/16/32-bit, within three export LSB.
A new isolated actual PhotoCraft desktop passed RGB16 synthetic sRGB/Gaussian,
Display P3/median, linear-sRGB/empty mask, sRGB/Blur-only and P3/neutral cases,
max RGB error0.00002155. No user's desktop connection/documents were used.

#### Earlier investigations (before combining the workflows/removing Saturation)

Runtime range-check follow-up: reproduced ordinary SDR white at1.0000001 after OpenCV
float32 `INTER_AREA` resizing (e.g.2102×1401 to1400×933, also6000×4000 to1400×933).
The strict LUT guard incorrectly called this HDR. Validate/cache original source eligibility
at load, reject genuine out-of-range samples before Skintone preview/Save/Send, and repair
only the mathematically bounded SDR preview's area-average endpoints. Original/native
pixels and ICC are untouched; full-resolution processing still has the strict guard.
Skintone eligibility/error is returned with load metadata and shown in its chart/status UI.
Errors follow the normal JSON400/UI catch path; switching to another mode remains usable.
Also corrected float image decode (no erroneous `/255`) and preserved uint8/uint16
normalization plus existing integer analysis quantization. `IMREAD_ANYCOLOR|ANYDEPTH` then
explicit grayscale-to-BGR handles float gray TIFF without OpenCV's forced-three-channel
TIFF decoder failure. Non-finite source pixels fail load explicitly. No source HDR clipping
or inferred tone mapping is added. Regressions use real8/16-bit white PNG downscales and
float TIFF SDR/HDR sources, unchanged-source assertions, Send refusal and mask-mode recovery.
All38 focused processing/API/native-transfer tests pass; the real WebKit desktop suite
passes3 separately, including handled Skintone errors and other-mode recovery. A normal
Tools/backend restart then Get is needed; no live restart or document mutation performed.

Blank-Result/no-effect follow-up: read-only cached live `/img/result` was nonblank PNG
(1400×933, samples0–253), but incorrectly labelled `image/jpeg`; cached mask was all255.
The handler now derives PNG/JPEG MIME from the encoded bytes rather than the pane key.
This is a confirmed delivery defect, but permissive browsers previously decoded it, so it
does not prove the sole cause of the user's blank pane. Added browser image error status/
placeholder plus retry recovery; do not silently leave a black image area after decode
failure. Added sampled visible-effect feedback (any channel change>1/255), with explicit
empty-mask/zero-amount/in-range-or-neutral explanations. Defaults are allowed bounds and
can be identity on ordinary skin; no-op must show the original, not an empty pane.

Nonidentity native corrections also tested on the actual installed desktop0.20.2 in a NEW
disposable `--new-instance --safe-gpu` process, isolated token/config/read-write roots and
synthetic document only. Strong H50±4/S5–20 changed BGR[.25,.4,.7] to[.56,.667,.70],
matching sampled preview within2.25e-5. Native group, mask, LUT order/domain and inline
loader work on that version, not only CLI0.18.0. No user desktop/doc was contacted for
mutation. This does not diagnose an existing desktop's GPU display/cache or unknown
current control values; requested a screenshot of that view via the coordinator rather
than assuming both symptoms share one cause. No speculative algorithm redesign.

Added explicit native hue-only/saturation-only changed-colour assertions and default-core/
zero-amount no-op assertions. Real WebKit now waits for a decoded, visible Result image,
reads its rendered pixels, checks significant colour change for narrow limits, verifies
zero Amount displays the original, checks MIME and exercises image-failure recovery.
All24 focused Skintone/skin/native tests and all3 desktop tests pass. Activation still
requires normal Tools/backend restart then Get; no restart/commit/push performed.

Historical range adjustment (saturation was subsequently removed by the combined workflow):
Hue Center10–50° (linear midpoint30°), half-width
1–8° (subsequent user revision from0–15°), saturation min/max both25–75%. UI sliders may meet (lower<=upper); crossing moves
the counterpart to the same value. Backend now accepts hue_width0 and equal saturation:
the existing hard-limit path targets one hue/saturation without division by zero, retaining
neutral protection, Amount, masking and finite-LUT caveats. Wider API/legacy presets remain
supported; finite old/default values outside the UI window retain actual state/readouts
and endpoint thumbs until edited. No automatic rewriting/reinitializing or new factory
colour target was requested. Graphs now use absolute hue10–50° and saturation25–75%
vertically, original HSV-value brightness horizontally; edge triangles mark off-window
percentiles. Distribution remains unmodified weighted10/50/90% statistics, not a global
masked mean or an auto-derived saturation target. Focused tests cover exact zero/equal
limits, legacy readouts, graph windows and measurements outside the display range.

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
