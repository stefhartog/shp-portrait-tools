# SHP PhotoCraft Tools handoff

Current reproducible implementation, updated **2026-11-08**. This file describes the
completed Tools code in this revision, not incidental experiments or planned features.
Use [README.md](README.md) for operation/setup, [MODELS.md](MODELS.md) for external weights
and nodes, and [PROVENANCE.md](PROVENANCE.md) for the migration sources.
`docs/source/` is deliberately frozen historical context, not current installation guidance.

## Scope, ownership and activation

The private monorepo is `stefhartog/shp-photocraft-tools`, local checkout
`/home/stef/shp-photocraft-tools`, branch `main`. The migration used the original Crop/D&B
sources read-only; those checkouts are not runtime dependencies. No personal photos,
presets, credentials, environments or model binaries are shipped.

Publication does **not** restart Tools, reload a live view, reinstall desktop assets,
start inference, or change PhotoCraft documents. Already-running backends/controller
retain their loaded Python: activate changes only through a later explicitly authorized
Tools restart after dealing with loaded/unsent work. HTML refresh alone is insufficient.
Existing installed launchers/resources are not modified by a commit.

Unrelated Blemish/ClearFrame prototypes, local research/data and desktop-tooltip edits
are excluded from this publication. They may remain dirty in the development checkout;
the committed application neither imports nor requires an excluded prototype module.
There is no Blemish tab/API or Windows support in this state.

## Architecture and entrypoints

| Surface | Responsibility |
|---|---|
| `modules/crop/` | Independent HTTP UI/server, signed canvas crop/outpaint, Comfy graphs, generated RGBA insertion. Crop alone resizes/crops the target canvas. |
| `modules/dnb/` | One shared skin-tool source/analysis/UI/server; D&B, Skin Mask, FS + Skintone and Combine. Engines are reused, not copied into Combine. |
| `shared/ui/` | Packaged local CSS, theme and lifecycle scripts, served identically by both modules. No Git CSS dependency/CDN. |
| `shared/photocraft/` | Auth/config/env precedence, JSON-lines transport, reconnect/errors, document/session negotiation and merged-document fetch. Module-specific application remains separate. |
| `launcher.py`, `supervisor.py`, `shell/` | Identity-verified owned startup, fixed proxy, mounted views, mode handshake, controls and lifecycle. |
| `shared/health.py`, `supervision.py`, `lifecycle.py` | Private ownership capabilities, work admission/draining, revision guards and leases. |
| `shared/local_output.py`, `owned_process.py` | Exclusive numbered outputs and identity-pinned explicit Force Restart cleanup. |
| `desktop/controller.py` | System-Python GTK3/WebKit2-4.1/AppIndicator3 window, tray, chooser, geometry and guarded shutdown. |
| `comfy.py` | Dedicated existing ComfyUI runtime watchdog, listener ownership and queue-aware shutdown. |

Standalone commands are `shp-photocraft-crop` / `shpcrop` (port **8768**) and
`shp-photocraft-dnb` / `shpdnb` (port **8765**). The supervised shell uses **18760**
and owned backends **18768 / 18765**, plus optional owned Comfy **18788**.
Skin Mask, FS + Skintone, D&B, Combine, Crop, Settings / Info is the shell DOM/keyboard order.
The four skin tabs share one D&B iframe/service. Crop is initially selected unless
its startup preference is disabled. Later polling never overrides manual selection.
Exception: a selected service reaching `stopped` falls back to available Crop, D&B,
then Settings so no hidden tab owns an inaccessible view. Only `stopped` hides module
tabs (all four for SkinTools); starting/stopping/error/busy remain visible.
Unavailable status preserves last known visibility. Keyboard navigation skips hidden
tabs, hidden-tab selection is rejected, and restart restores tabs without stealing
the current view. The existing full processing navigation lock still applies.

## Shared source, settings and current UI

Get/load captures one source, analysis image, selected-subject pose, segmentation,
scale S and provenance. FS/Combine retain the original native numeric samples and ICC;
D&B retains its existing processing colour-space contract. A local load clears the
fetched PhotoCraft identity. Mode changes do not refetch, rerun inference or reset settings.

Presets save the entire bundle, including hidden mask/FS/D&B/Directional/Combine settings.
The preset star excludes navigation mode and S-derived FS pixel radii. Legacy `skintone`
normalizes to `frequency`; absent new controls receive defaults in memory, not file rewrites.
Saturation correction is removed; legacy fields are ignored rather than reinterpreted.
Landmark edits belong to the source, clear on new load, and adjust scale only: they do
not rebuild segmentation or change the selected skin region.

| Mode | Processing controls | Left previews | Right previews |
|---|---|---|---|
| Skin Mask | Shared skin/feather, structure, light and colour range; Image analysis/S/landmarks | Original | Mask, Analysis |
| FS + Skintone | Frequency separation, Blur, Directional Blur, Skintone Correction | Original, Result | Color, Detail, Region when Directional enabled |
| D&B | BAND 1, BAND 2, SHARED CONTROLS | Original, Result | Band 1, Band 1 + mask; Band 2 and Band 2 + mask when enabled |
| Combine | **FS mix and D&B mix**, plus local batch controls; edit ingredients in their individual tabs. Image analysis is hidden. | Original, Combined | FS result, D&B result, Mask |

Shared mask values remain active while panels are hidden. S/source access is through
Skin Mask > Image analysis and common Get Image. Combine does not duplicate controls.
Remembered invalid preview tabs fall back to the first available tab; both pane images
remain mounted through changes. Image analysis/Log are visible only in Skin Mask.
The rightmost general Settings / Info page holds Theme Selection, PhotoCraft layer-transfer
options, precache preference and calculation timings, plus About. Existing image-analysis
details, the cache-status badge, non-timing notes and operation/status messages stay put.
The shell adopts the existing controls into its own panel after the origin/source-checked
skin-ready handshake; scoped ID lookup keeps handlers bound to the same source state.
Layer-transfer options now persist globally in the app config `settings.json`, migrating
the old default once; module presets no longer save or restore them. Existing legacy
preset records remain intact. Theme/precache remain global browser preferences and the
calculation log is temporary.
Exclude opaque alpha defaults on for new globals; persisted `exclude_opaque_alpha`
inverts legacy `opaque_alpha` without changing saved preferences. Encoders still use
the include-alpha contract internally.
Native disabled setters are restored on adoption so busy gating still works. Repeated ready
messages do not clear mounted controls; source reload replaces them, and stopping the service
hides them. Theme/About remain usable without a running skin service. Standalone skin views
retain the controls in their own general section. Settings navigation never requests a skin
mode or overrides it when a delayed mode acknowledgement arrives.
SkinTools busy signals disable all shell navigation, including Crop/Settings, immediately;
supervisor busy status provides a fallback. Disabled tabs have no hover/press affordance.
Busy mode requests are discarded rather than queued, and obsolete acknowledgements ignored.
Directional informational text is hidden, but its handler target and switch/parent
geometry-reason tooltips remain intact.

Themes are Current and PhotoCraft (default absent a valid saved choice), per-origin
`shp-tools-theme`, reapplied by an origin/source-checked iframe handshake. Shared 74px
labels, 42px transparent readouts, grey-off/blue-on switches and equal-width workflow
buttons preserve keyboard/focus behavior. Disabled range/readout backgrounds match
ordinary Blur: grey switch backgrounds target checkboxes only. Header switches remain
clickable while their panel controls are off; operation busy gating is independent.
Responsive centred status/header rows avoid action/preset overlap. Busy progress is a
muted-blue indeterminate background, static with reduced motion, never a click interceptor.

Slider values/readouts update during pointer movement, but image computation waits for
release, cancel, lost capture or blur; keyboard changes retain debouncing. Gain graphs
can update independently. Quiet recalculation blocks conflicting input immediately,
with visible busy feedback delayed 150 ms. Stale generation/settings/epoch responses
are discarded. Completion waits for visible image load events, not screen paint.
The bounded in-memory log records stages/timings/errors; reload clears browser-only state.

## D&B bands and diagnostic exaggeration

`dodge_burn.band_stack` generates enabled bands independently from the original, then
applies band 1 followed by band 2 through the shared final mask. Band 1 defaults on;
band 2 off, preserving legacy band-1-only output. Each has range, taper, Strength and
its own gain-by-feature-size graph. Band 2 invert changes only its neutral-grey raster
to `1 - layer`, not band 1 or the corrected photograph. Layer blur, edge, tau and
maximum EV remain shared. Disabled bands skip analysis.

Layer blur is Gaussian sigma **0-20 px at S181**, step 0.1, default 0; full-resolution
sigma is value * S / 181 and preview sigma also uses view scale. It never blurs the mask.
Tau's slider is 0.01-0.4, step 0.01; positive legacy values such as default 1 retain
their actual readout/request value until moved. Lower tau suppresses large deviations;
edge instead changes guided-filter smoothing.

Diagnostic band panes are **always exaggerated x8 about neutral 0.5**:
`clip(0.5 + (layer - 0.5) * 8)`, also after masking for "+ mask".
Band 1 routes to `layerx` / `layermx`, band 2 to `layer2x` / `layer2mx`.
No toggle remains; obsolete browser storage cannot turn it off. Result, Combine's
D&B result, processing, Save/export and sent layers remain actual strength.
Two-band compositing quantizes RGB16 layers/PNG8 masks for native agreement; the
original single-band path keeps its legacy calculation.

## Frequency separation, Blur and Hue

`frequency_separation` provides Gaussian, Median and Median Fast Color/Detail.
Color is ordinary full-image filtering; Detail is `(source - Color) / 2 + 0.5`.
Unmasked Color plus Linear Light Detail reconstructs the source within RGB16 rounding.
Core separation has no off switch. `frequency.on` controls only additional Blur.

Radius calibration defaults 6 px at S181, slider 0.5-30, step 0.1.
Actual full-image radius is rounded calibration * S / 181, clamped 1-100.
Blur is 100-500% of that rounded radius (default 200), up to 500 px.
Gaussian uses sigma; Median uses square radius with float medians internally quantized.
Median Fast uses an optimized uniformly quantized median, not identical output to Median.
Preview scales radii; final outputs stay full-resolution.

Optional Blur filters Color using normalized skin-weighted Gaussian support, not
black premultiplication; no support retains Color. Strength 0-100 controls layer opacity.
Skin mask off uses whole-image support and sends Blur unmasked for later painting.
Turning Blur off skips its calculation/encoding/import but retains Color/Detail.

Skintone is one masked Hue Color Lookup between low-frequency effects and Detail.
Hue centre/width/softness and amount persist; off skips estimation/LUT/correction.
The 33-cube LUT is inline, trilinear, dither off; neutral pixels are protected.
Source must be opaque with supported finite SDR samples; missing ICC/invalid samples
fail explicitly. Skin-weighted additional filters cannot undo colour mixing already
introduced by the ordinary Color split. No native-FS test button/HTTP route remains.

## Directional Blur: geometry, effective selections and overlap

`directional_blur.py` owns eight limb selections plus torso. Upper legs are hip->knee
(COCO 11->13 / 12->14); lower legs knee->ankle (13->15 / 14->16); upper arms
shoulder->elbow (5->7 / 6->8); forearms elbow->wrist (7->9 / 8->10).
Four full-width symmetric rows put anatomical right on viewer left and anatomical left
on viewer right for camera-facing correspondence. IDs/meaning stay anatomical.
Directionality is off by default; legacy `left/right` upper legs default selected,
the six additional limbs and torso default off. No extra pose inference on edits.

Coordinates must be finite, in-image and nondegenerate. Low/absent confidence scores
do not prohibit valid geometry. Requested preset selections remain unchanged.
`effective_settings` makes a per-image copy and catches only typed `GeometryUnavailable`,
skipping unavailable selected limbs/torso. A wholly missing selection bypasses Directional,
omits its raster/mask/native ingredient and leaves FS/Combine/Save/Send usable.
Grey disabled checked selectors retain requested values; groups with no usable selected
side hide their controls. Later usable geometry restores the selection automatically.
Tooltips explain missing geometry. Genuine settings, safe extent, malformed data and
finite-mask errors still propagate; no broad catch or guessed geometry is used.
Native shared opacity depends only on usable groups, not unavailable preset strengths.

Each pair (`upper_arm`, `forearm`, `upper_leg`, `lower_leg`) and torso owns Distance,
Width, Taper, Strength, Feather, Start and End. Missing nested fields migrate old
global values into every group, preserving legacy settings/output. Deep preset/request
copies avoid accidental mutation. Pair controls hide when off/unavailable, not just busy;
selected usable groups remain visible but disabled during work.

Limb defaults: Distance 30, Width 100, Feather 3 px at S181; Strength 100%, Taper 0%,
Start 0%, End 100%. Bounds: Distance 0-300, Width 1-1000, Feather 0-50,
Strength 0-100, Taper 0-90, Start 0-99, End 1-100 with at least 1% separation.
Width is full thickness **perpendicular to the joint-to-joint line**, with rounded caps.
Taper narrows toward the adjusted distal end. Start/End move along the anatomical axis,
independently of motion distance. Legacy Length <=100 becomes Start 0/End Length;
extensions above 100 remain an explicit error, not silently clipped.

Torso uses the convex shoulder/hip quadrilateral and motion shoulder midpoint->hip
midpoint. Width **1-200%**, default 100%, covers the whole detected box at default;
smaller widths narrow around its centreline. Taper defaults 0 and narrows toward hips.
Start/End crop that box along its axis. It is not a narrow capsule and has no rounded caps.
Crossed/degenerate/nonfinite/outside geometry is unavailable.

Distance is centred, rounded to a symmetric odd kernel. Scaled safe limits remain
Distance 2000, limb Width 8000, Feather 500, region axis 16000 pixels.
Filtering uses bounded union ROIs, globally anchored rotation and chunked masks.
Sampling is motion(base * skin) / motion(skin), identical transforms/kernels for both;
zero-weight clothing contributes nothing, <=1e-6 support keeps the base.
Final region coverage intersects skin once.

All usable regions produce **one RGB16 raster and one editable mask**. Equal Strength
keeps full-strength pixels plus common native opacity. Mixed strengths bake each group's
relative delta against the same base into opacity-100 pixels; overlaps use a normalized
feather-weighted average, not sequential/additive blur. Zero-strength regions skip motion
but contribute unchanged base to that average. Equal Feather retains Gaussian(union)
legacy coverage; different Feather uses max(individually feathered regions).

## Combine and output contracts

Combine reuses current FS/D&B/shared source/settings/mask, with no duplicate engine.
FS mix and D&B mix are 0-100%, default 100 each. First blend Original->FS result,
then apply original-derived masked Soft Light bands to that base with D&B mix.
Right FS result and D&B result are configured **before** mix, including when mix is zero;
preview computes each ingredient once. Final Save/Send skip zero-mix work.
Mix/settings/source/pose changes invalidate the bounded mode preview.

PhotoCraft FS/D&B/Combine expose **Send Result** (flattened corrected RGB16 Normal layer)
and **Send Layers** (editable ingredients). Skin Mask/Crop retain only Send Result.
Every layered skin send creates one **SkinTools** folder, top-down:

1. Enabled D&B band 2, then band 1, Soft Light.
2. Detail, unmasked Linear Light.
3. Enabled Hue/Skintone adjustment.
4. Enabled Directional Blur, Normal.
5. Enabled ordinary Blur.
6. Color, unmasked Normal.

Global `mask_placement` in Settings / Info chooses `folder` (default, including
existing config migration) or `layers`. Folder mode attaches one shared skin mask
to SkinTools, never its ingredients; an empty mask hides the folder. Layers mode
restores the previous per-ingredient skin/Directional/D&B masks and optional Blur
mask, leaving Color/Detail unmasked. Names/order/blends/opacity are unchanged.
Folder masking intentionally affects the complete stack rather than individual
ingredient regions. Flattened results, standalone Skin Mask and Crop are unaffected.

FS-only omits D&B; D&B-only contains only enabled bands and is **Pass Through**, so
Soft Light acts on the destination below it. FS/Combine folders are Normal because
Color supplies their opaque foundation. Each folder is assembled off-document and
copied once, not separate FS/D&B folders. A zero-contribution editable send is refused.
Final insertion uses non-retried command admission; a lost reply reports uncertainty
and asks to inspect the destination before retrying. Scratch/docs/files are cleaned,
including TIFF band files; cleanup errors remain visible.

Combine editable FS mix interpolates Color/Blur/Directional rasters against Original,
Detail against neutral and Hue amount by mix, retaining masks and relative opacities.
This is an editable approximation: nonlinear Hue, clipping and quantization can differ
from the flattened photograph. Exact cross-output parity was explicitly deprioritized;
**Send Result** is the authoritative flattened calculation. No standalone layered format
is introduced.

Combine's separate collapsible BATCH PROCESSING panel accepts one input folder's direct
image children or selected files from that folder (existing JPEG/PNG/TIFF/WebP support).
One Choose source button opens a native Folder/Images mode choice followed by the
appropriate chooser; no manual path boxes remain. Source selection requires the desktop.
Selection lists direct images immediately and displays a count for both folder/files.
The shared workflow-actions row places Start Batch next to Choose source; Cancel reuses
Crop's red cancel treatment from global CSS. Top batch statuses are concise and use the
existing success checkmark; while running they show `Batch processing... (n/total)`
after each photo. Details go to the log and calculation timings page.
Completion/cancellation statuses include total backend runtime in seconds, one decimal.
Native prompts reuse the owned-origin chooser bridge. Batch does
not run Crop or contact PhotoCraft. It snapshots all ingredient/mix settings, independently
loads/analyzes each image for S/masks/pose, and scales calibrated FS radii per image.
Other settings, including Hue Center, stay fixed. Missing detected S or invalid images
are per-item failures; later items continue. Editor source, previews and document provenance
are not replaced. Scoped D&B colour mode leaves editor/background-mask colour unchanged.

`/api/batch/images` validates/list-selects direct sources; same-origin JSON `/api/batch`
runs under the existing processing lock/lifecycle work admission. The owned proxy permits
long batches without the ordinary 900-second request timeout. Progress snapshots include
per-item pending/running/success/failed states. A pinned, exclusively numbered
`shpcombine_batch_NNN` input subfolder holds RGB16 flattened PNG/TIFF results named
`<source-stem>_NNN.png` (or `.tiff`), with the same batch-folder ordinal for every image, and one
`preset.json`. Shared settings appear once at the top level; `batch.items` maps source,
status/errors, independent S/analysis/FS radii and output filename. The owned preset is
atomically updated after each item and refuses external changes. Exclusive image writes,
directory identity checks and failed-write cleanup prevent source/output overwrites.
Same-stem output collisions fail the later item explicitly; no per-item renumbering.
No symlink inputs or nested image folders are processed. Single-image Save/Send are unchanged.
Same-origin `/api/batch/cancel` bypasses the processing lock and cancels only its active
progress token. Cancellation checks safe boundaries between loading, analysis and
compositing/saving: saved outputs remain success; unsaved/current and pending items become
cancelled, persisted to the single manifest. Existing inference/filter work is not killed.

All native RGB16 scratch data carries captured ICC without altering numeric samples;
masks remain untagged PNG8. Repeat Get after destination size/profile/replacement changes.
PhotoCraft metadata does not prove document incarnation or exact ICC identity, an accepted
limitation. FS/Combine/flattened sender require captured ICC; legacy low-level D&B/mask
paths retain their safe profile-export fallback. Tools never changes the target profile.

Local Send Result follows load-time provenance, exclusively creates a numbered file
beside the source and never overwrites or silently saves a disconnected PC exchange file.
Suffixes: `_shpdnb_`, `_shpfs_`, `_shpcombine_` are RGB16 PNG/TIFF with global
compression/optional opaque-alpha settings and appropriate
processing/source ICC; `_shpskinmask_` grayscale8; `_shpcrop_` selected RGB8 final canvas
with inherited sRGB conversion and no new ICC guarantee. Standalone Save is separate:
D&B band/mask/settings bundle; FS ingredients/Hue cube/masks/result/settings; Combine
flattened `result.png`/`result.tiff` plus `settings.json`; Skin Mask mask/settings.
The globals also govern standalone Save's flattened results (D&B now RGB16 rather than
JPEG), not ingredient layers, masks or Crop. Batch captures globals once and records
them under `batch.output_settings`, separate from module preset fields. Parallel encoding
still applies to supported multi-layer transfers; batch remains sequential.

## PhotoCraft export inbox

Crop independently watches `/home/stef/shp-photocraft-tools/watchfolder_crop` while its
service runs. Validated inbox/capture-target helpers now live in shared/watch_folder.py
(no SkinTools/model dependency in Crop). Crop prepares RGB/preview, checks target/size
before publishing, then captures PhotoCraft provenance and cleans the unchanged input.
UI uses xLoaded and source/origin-validated shp-crop-watch-loaded to select Crop;
failed imports never switch. Crop watch work announces busy to the shell, blocks
competing imports, and waits for SkinTools navigation locks. Desktop monitor polls both
running services, with independent wake-token deduplication. No stopped module is
automatically started; enable Crop in tray first.

The D&B service owns a `WatchFolder` for the explicit
`/home/stef/shp-photocraft-tools/watchfolder` path. Its connected UI polls every three
seconds when ready/idle; direct regular PNGs require two unchanged seconds plus complete
PNG chunk/CRC/IEND validation. Directory descriptors and file-version checks pin the
input, and decoding uses an exclusive app-owned `ROOT/inbox/watch-*/<filename>` snapshot.
No arbitrary source path is accepted by watch API requests.
The desktop controller's existing monitor also polls this backend status route while
D&B is running/idle, independently of any WebUI polling. Stable PNG validation occurs
in service status before ready is reported. A new ready token presents the existing
window once on the GTK main loop; stale epochs and shutdown/control operations cannot
reveal it. Showing the window resumes the normal no-dialog UI import. Tray-active
requires the controller; a supervisor-only process does not launch a new GUI.

Same-origin POST load automatically captures the active PhotoCraft document index/name plus
`document.inspect` id/revision/width/height at processing time; there is no confirmation.
Keep the exporting document active: same-size unrelated exports cannot be distinguished
without PNG provenance metadata. Older PhotoCraft APIs without an inspect `id` use session index/name/path
as the identity, with inspect name/path consistency and before/after revision checks.
Load rechecks the captured target before/after independent analysis and size
checks before publishing editor state/PhotoCraft provenance. It then uses applyLoaded,
Hue Center and normal preview apply. Only successful load/association permits identity-
checked inbox unlink. Successful watch loads switch the iframe mode and shell tab to Skin Mask
via a source/origin-checked internal message that bypasses user navigation locking;
failed/invalid loads never send it. Failure retains the original, version changes restart
stability waiting, and cleanup errors surface without reloading the same version.
Invalid unchanged files are suppressed until changed/re-exported (or service restart).
Association failures retry only on a changed target identity/revision/dimensions;
missing/temporarily uninspectable targets retry automatically without prompting.

There are no watcher threads/subprocesses. Actual imports run inside the normal owner/
lifetime admission and image-processing lock; `main` closes the watcher on exit.
No runtime/launcher changes are needed except the user's Tools restart to load new code.
The watch folder is Git-ignored; never add exported inbox PNGs to a commit.

## Mask cache, resource measurements and validation boundaries

Preview cache retains one latest entry per mode within **192 MiB**, keyed by source
generation, region/scale, shared mask and mode settings; requested Directional settings
and normalized pose coordinates remain in keys. New loads/landmarks invalidate entries.
Cache restoration is explicitly logged, not timed as fresh processing.

Full mask cache has one immutable final mask/optional structure entry within **256 MiB**.
Source/component hashes and effective mask/S/profile identity prevent stale reuse;
FS/Hue/D&B band/mix-only edits reuse it. No full-resolution result/disk cache exists.
Default-on precache (Settings / Info) uses one worker after **600 ms** settled
preview; stale/over-budget jobs are discarded. Checkbox is a local preference, not preset.
Send waits for matching work or computes its correct mask. Status cross/spinner/check
reflects the actual retained cache, not just job completion. Temporary working memory
can greatly exceed either retention cap.

Historical measurements below are host/fixture-specific, not guarantees for portraits,
fresh machines, live UI or the new combined stack:

| Measurement | Result / boundaries |
|---|---|
| Synthetic 32.7 MP FS transfer, CLI 0.18 | Encoding/import/assembly only: default compressed RGBA PNG parallel 9.65 s vs serial 14.16 s; uncompressed RGB TIFF parallel 5.47 s. Excludes separation/load/analysis. |
| Subsequent **live user** FS sends | 11-13 s across tested transfer choices, no meaningful TIFF speedup; two sends about 1 GB PSD. Live observation takes precedence over transfer microbenchmarks. |
| Synthetic 32.7 MP full warm-mask FS Send | Gaussian 22.04 s serial ->14.25 s warm optimized; Median 54.35->46.39 s. CLI, not desktop; excludes load/analysis/export verification. |
| Synthetic radius-15 full warm Send | Median 28.86 s vs Median Fast 8.74 s, two runs each; different numeric median output, not portrait visual approval. |
| Synthetic 6.3 MP Directional only | Two legs 0.133 s/307.5 MiB; eight limbs 1.484 s/614.6 MiB; nine-region equal 1.637 s/710.7 MiB vs mixed controls 1.773 s/709.3 MiB. No encoding/import/inference, not full pipeline. |
| Forced precache overlap, synthetic 1600 square | FS preview 1.07->1.28 s, D&B 0.73->0.90 s; Tools peak RSS 1.53->2.83 GiB. Production debounces and allows opt-out. |

Automated coverage uses synthetic CPU inputs, stub detections/services, temporary ports/
data/settings/exchange and isolated stdio `photocraft-cli`, never the live desktop documents.
Tests cover masks/profiles/RGB16, per-group overlaps/geometry, unavailable preset recovery,
native folder topology/flattened pixels/lost replies/TIFF cleanup, Combine before/after mix,
cache invalidation, shared presets and actual Firefox pointer/keyboard/responsive styles.
This publication's **prototype-free snapshot passed 270 isolated tests (one optional
desktop smoke test skipped)** via `tests/run.py`, including the actual Firefox fixtures
and isolated PhotoCraft stdio tests. Legacy layered-send fixtures now call Send Layers;
the existing stopped-module test asserts its documented 409 `module_stopped` response
rather than the obsolete 503 expectation. No supervisor behavior changed.
`uv build` produced wheel and source distributions. Package checks verify both UIs,
shared resources, shell, native helper and entrypoints; the seam node is source-release-only.
Tests/build use the clean snapshot, not dirty prototype imports. Optional
`SHP_DESKTOP_SMOKE=1` real GTK/WebKit tests require a graphical/tray host and explicit
authorization; fresh-machine setup, GPU inference and live-document activation were
not performed during publication.

## Crop, owned lifecycle and deployment invariants

Crop preserves signed ratio/free geometry, wheel/pan/result selection, incremental 1/2/3
variations and Cancel retaining finished results. Positive margins are capped at 999
**original** pixels per side (`SHPC_LIMIT` can lower), negative cropping unrestricted.
FLUX.2 remains default/legacy; FLUX.1 Fill has independent guidance/steps/megapixels/
feather/model/upscaler settings and always corrected colour/seam/paste-back pathway.
CPU/default text encoding is a saved choice, not a VRAM-fit promise.
Free Comfy memory refuses running/pending queue work; success requests asynchronous
unload, not measured reclaimed memory. No inference is needed for regression fixtures.

Supervisor owns only identity-verified children/capabilities; no adopting/killing unrelated
servers. Exact loopback Host, same-origin JSON, allowlists and revision/instance guards
protect controls. Mounted frames preserve view state; stopping removes only that module.
All four skin tabs represent one service. Startup preferences are strictly persisted;
manual Start does not silently enable startup. Parent-loss/shutdown drains active work.
Owner-only capabilities are never exposed through the fixed module proxy.

Native window X hides to tray; relaunch shows the same window. No tray host keeps an
accessible window. Quit/Restart recheck busy/unsent work and other leases. Native acknowledged
Quit starts draining before lease release; normal restart waits for old identities.
Explicit Force Restart pins only owned Linux session members with PID descriptors;
graceful/TERM/KILL escalation never targets PhotoCraft or external Comfy.
Partial cleanup blocks duplicate replacement and reports actionable errors.
Browser leases use 20 s polls/90 s expiry/15 s last-close grace; desktop keeps its own
lease while hidden. No automatic unsaved recovery is implemented.
Wayland placement is compositor-controlled; normal window sizes persist atomically.
Default native size 1399x949, WebView minimum 363x626. Native notifications are a separate
non-expanding row, not an overlay blocking shell tabs; persistent failures retain controls.

Optional managed Comfy uses the chosen checkout's existing venv/models/nodes with isolated
Tools user/input/output/temp and dedicated non-8188 port. Listener inode/child ownership and
real queue are verified. Crop startup starts it; stopping Crop alone does not stop it.
Whole-Tools shutdown drains only the owned child; unreadable established queues fail closed.
Tools neither installs nor auto-starts PhotoCraft for reachability status.

Fresh setup requires Python 3.13/uv, root lockfile, external weights/nodes and optional
CUDA 13/cuDNN plus NVIDIA driver. Full CPU-only analysis is not promised: Wholebody requests
CUDA. Native helper uses system GI, not uv ML Python. Models/ExifTool/Comfy paths are explicit;
manual model hashes and offline pose-cache preparation are in MODELS.md.
Installer is distinct/idempotent and refuses differing desktop assets; no silent cutover.
Module data/env aliases and preset formats remain compatible. Never share writable
production data/ports/token exchange for side-by-side validation.
