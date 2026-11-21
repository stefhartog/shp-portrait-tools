# Skin Tools developer guide

Skin Mask, FS & Tone, Dodge & Burn (D&B), and Combine share one service in
[`modules/skin`](../src/shp_portrait_tools/modules/skin). Start with the boundary
that owns the behavior you want to change, not with the whole server.

| Change you want to make | Where to start |
|---|---|
| An HTTP route, a request setting, or a button | [`ui_server.py`](../src/shp_portrait_tools/modules/skin/ui_server.py) wires routes, validates requests, and retains server-side editor and provenance checks. [`ui/index.html`](../src/shp_portrait_tools/modules/skin/ui/index.html) is the browser UI. |
| What gets replaced on Load/Get, or what makes a snapshot stale | [`image_session.py`](../src/shp_portrait_tools/modules/skin/image_session.py) publishes the source, advances its generation, resets source-derived state, and captures full-resolution inputs by reference. Decode, profile handling, and source analysis still run through `ui_server.py` before publication. |
| Preview response, tab switching, or preview cache reuse | [`preview_service.py`](../src/shp_portrait_tools/modules/skin/preview_service.py) coordinates `/api/run`, decides whether to keep or queue background preparation, and attaches status. The preview renderers, preview-size keys, and component cache helpers remain in `ui_server.py`. |
| Full-resolution composition, shared mask use, or editable layer pixels | [`processing_service.py`](../src/shp_portrait_tools/modules/skin/processing_service.py) assembles results and layers through shared mask, split, and band helpers. `ui_server.py` still supplies those helpers, their keys, and the full-mask cache. Algorithms live in [`skin_mask.py`](../src/shp_portrait_tools/modules/skin/skin_mask.py), [`frequency_separation.py`](../src/shp_portrait_tools/modules/skin/frequency_separation.py), [`skintone.py`](../src/shp_portrait_tools/modules/skin/skintone.py), [`directional_blur.py`](../src/shp_portrait_tools/modules/skin/directional_blur.py), and [`dodge_burn.py`](../src/shp_portrait_tools/modules/skin/dodge_burn.py). |
| Background scheduling, cache retention, or readiness status | [`cache_coordinator.py`](../src/shp_portrait_tools/modules/skin/cache_coordinator.py) owns priority, job status, and stale-work coordination. [`mask_precache.py`](../src/shp_portrait_tools/modules/skin/mask_precache.py) runs the debounced, coalescing workers for both mask and layer jobs. [`layer_cache.py`](../src/shp_portrait_tools/modules/skin/layer_cache.py) owns bounded full-resolution component storage, not scheduling or pixel math. |
| Save artifacts, local Send, or editor dispatch | [`transfer_service.py`](../src/shp_portrait_tools/modules/skin/transfer_service.py) writes Save bundles, encodes results for [`shared/local_output.py`](../src/shp_portrait_tools/shared/local_output.py), and dispatches prepared payloads. [`photoshop_bridge.py`](../src/shp_portrait_tools/modules/skin/photoshop_bridge.py) and [`photocraft_bridge.py`](../src/shp_portrait_tools/modules/skin/photocraft_bridge.py) own editor-specific capture and import. |

## Request and data flow

1. **Load/Get:** Decode and analyze the image, then publish its analysis pixels,
   native pixels, ICC metadata and mask references together. A failed
   preparation does not replace the live source. Batch uses prepared inputs
   without publishing them to the live session.
2. **Preview (`/api/run`):** The preview service coordinates the foreground
   request under the preview lock, calls the existing renderer, and selects or
   queues matching background work. Settings that only change the Combine mix
   can reuse matching components. Preview components are sized for the preview,
   not for Send.
3. **Background preparation:** Capture the source snapshot and effective
   settings before queuing. Layer work uses the full-resolution gate, not the
   preview lock, and yields between stages when foreground or mask work is
   waiting. The scheduler guard and source identity check prevent a superseded
   layer job from publishing results or reporting them ready.
4. **Save/Send (`/api/save`, `/api/send-result`, `/api/pc/send*`):** The server
   validates the request and resolves the source or editor destination. The
   processing helpers assemble full-resolution pixels and reuse matching
   components; the transfer service writes export files or invokes an editor
   adapter. In the parallel FS path, the processing service keeps the deferred
   Blur worker alive during the synchronous transfer callback.

**Settings & Info → Calculation timings** shows the latest full-resolution
preparation of Skin Mask, FS & Tone, D&B, and Combine. Elapsed time starts
when each tab's work begins and may include pauses for foreground work.
Cache reuse is labeled separately, and shared FS components are not timed
again under Combine.

Load/Get separately reports the source-mask total and its measured components
(model setup, inference and refinement per crop, face exclusions and colour
fitting/filtering when used). The Skin Mask cache row instead measures applying
the current settings to those stored components, including tone and structure
when enabled. Indented durations are part of their parent total; reusing a
cached mask does not repeat those calculations.

The independent **Skin Mask backend** setting defaults to CPU. Experimental
CUDA accelerates guided edge refinement and sharpening for each full-resolution
Load/Get segmentation crop and applies feathering, selected colour range,
luminance, tone and structure protection in the full-resolution final-mask
cache. Wide feathering uses the requested Gaussian radius in bounded CUDA passes.
Stored components are combined on CPU. ONNX provider selection, feature and
colour masks at Load/Get, and preview code are unchanged. Reload the same image
after restarting Tools to compare modes. Calculation timings identify CUDA
and any CPU fallback; small mask variations are possible.

With enough free RAM, background Color/Detail separation can run while the
shared mask is pending. After the mask is ready, skin-aware Blur can overlap
standalone D&B when CUDA is selected. CPU D&B stays serial, including CUDA
fallback, and Combine waits for the cached FS components. Tab elapsed times
can overlap; do not add them together to estimate total preparation time.
The current test reserve is 2 GiB free system RAM plus 128 bytes per source
pixel for background admission; parallel overlap requires 2 GiB plus 256
bytes per pixel. Layer cache reads and writes require 2 GiB plus 64 bytes per
pixel. The separate layer-cache capacity remains 2 GiB. If available system
RAM is insufficient or cannot be measured, cache preparation reports the
stage and threshold and fails closed.

## Experimental CUDA D&B

`processing_service.cached_dnb_band` selects the backend only on a full-resolution
editable D&B cache miss (standalone D&B or Combine). The desktop service uses
CUDA for D&B and final Skin Mask when available and falls back to CPU otherwise.
Settings & Info reports the backend; it is no longer a configuration control.
[`cuda_bands.py`](../src/shp_portrait_tools/modules/skin/cuda_bands.py) compiles
the adjacent `.cu` resource using the existing Windows CUDA 13 NVRTC runtime.
It uses device 0 with compute capability 8.0 or newer; validation so far is on
an RTX 3090. No new Python packages are needed on the tested install.

The shared final mask, profile handling, and cache keys and admission stay the
same. Standalone D&B also keeps its CPU distance transform. The renderer uploads
per uncached band, keeps the band intermediates on the GPU, and downloads the
final layer. Jobs still yield between bands, not within a running CUDA band.
Unsupported inputs, runtime or device errors, and VRAM limits use CPU with a
logged reason (also in Send progress when available). Near-white pixels do
not trigger fallback; small tone-curve rounding differences can be amplified
by Soft Light inversion.
The backend is fixed for the process so restarting also starts an empty cache.
CUDA is not pixel-identical to CPU: a synthetic deep-shadow stress case at
low edge and high strength showed a difference of 21 RGB16 layer codes.
Use CPU when matching the reference output matters.

For fallback tests use `python -m uv run --no-sync python -m unittest discover -s tests -p test_cuda_bands.py`.
Set `SHP_TEST_DNB_CUDA=1` to also run hardware parity checks. The test compares
editable pixels and a quantized software Soft Light composite separately, and
verifies that near-white fixtures can run on CUDA. It does not
substitute for validating the actual blend in Photoshop or PhotoCraft.

## Rules for focused changes

- Keep route names, JSON fields, progress/error behavior, and editor checks
  stable unless the task explicitly changes the contract. Do not bypass
  source-generation checks, editor pairing, or document validation.
- Preserve native RGB and ICC where the output requires them. The D&B working
  profile, masks, and numeric correction layers have different semantics from
  color photographs. Preview-only mask application and D&B contrast
  exaggeration must not change full-resolution Send pixels.
- Key reuse on exact dependencies. Keep preview and full-resolution components
  separate; don't invalidate expensive work on mix-only changes or reuse it
  after a pixel-affecting change. Keep snapshot images and masks read-only by
  convention; do not copy them just to queue work.
- Preserve lock ordering: full-resolution foreground work takes the
  full-resolution gate before the preview lock. The mask calculation lock is
  separate so the FS worker can prepare a mask during Send. Keep expensive
  background work outside the preview lock and retain the commit guards.
- Keep filenames, bit depth, blend modes, and mask placement stable when
  refactoring output. Use mocks to test dispatch and disposable documents for
  live integration checks; passing a mock test does not verify a live editor.

## Focused tests

From the repository root with dependencies already synced, choose the test
files for the boundary you changed. Set `$env:PYTHONPATH = 'tests'` in
PowerShell or `export PYTHONPATH=tests` in a POSIX shell, then append the
selectors below to `python -m uv run --no-sync python -m unittest`.

| Concern | Selectors |
|---|---|
| Load/Get state and stale background work | `test_image_session test_layer_cache.LayerSendCacheTests.test_stale_generation_cannot_publish_components_or_ready_status` |
| Preview pixels, dependencies, and tab reuse | `test_preview_cache test_combine` |
| Scheduling, mask reuse, and full-resolution retention | `test_layer_cache test_mask_precache test_full_skin_cache` |
| Algorithms and mode-specific assembly | `test_dnb` for D&B; `test_fs_pipeline` or `test_directional_blur` for FS & Tone; `test_combine` for Combine |
| Save and local Send pixel/ICC equivalence | `test_local_results.LocalResultTests.test_one_full_resolution_file_per_mode_with_profile_and_precision` |
| Result/mask dispatch to both editors | `test_local_results.LocalResultTests.test_editor_send_routes_forward_result_or_mask_to_the_loaded_document` |
| Editor layer payloads and transfer options | `test_photoshop test_skin_tools_transfer test_transfer_options` |

See the [Photoshop connector guide](photoshop.md) for wire and live-validation
details. The [historical D&B notes](source/dnb-readme.md) describe the original
standalone project, not the current source layout or run commands.

## Background cache debounce trial

Background preparation waits for 150 ms of inactivity for the full-resolution
mask and 300 ms for layers, temporarily replacing 600 ms and 1,500 ms.
Foreground or explicit preparation does not wait for these timers, but can
still wait for work in progress. To end the trial, restore the previous
coordinator delays; the worker's general default is unchanged.

Open/Get, including re-Get of the same document, supersedes background jobs
and clears the previous full-resolution layer, mask, and preview caches before
new image analysis. The old source remains available until replacement succeeds,
so a failed load or cancelled file selection can restore the old preview; its
cleared caches are recomputed when needed. Preview panes are blank during the
attempt. Active work may retain temporary references until it finishes, and the
OS may retain freed memory in the process working set.

## Automatic S estimator

**Torso + head** is the default for Open/Get, batch, and watch-folder.
Interactive Analysis reports the Torso + head estimate and the face-led
baseline for the same subject when available. Typing a value in **px / 63 mm**
overrides the automatic estimate for the current image. Batch and watch-folder
estimate scale independently for each image; they do not inherit that override.
The baseline estimator is retained for development but is no longer selectable
in the desktop UI.

The torso + head estimate weights shoulder-to-hip midpoint distance at 75%
and YuNet face-box height (a head-size proxy, not a crown-to-chin measurement)
at 25%. The temporary image-calibrated test references are about 419.3 mm
for the torso and 174.7 mm for the face-box proxy, fitted to the baseline on
one frontal example. They are not general anthropometric averages. With two
usable cues, a larger estimate up to 20% above the smaller is medium
confidence; above 20% and up to 35% is low confidence. A single usable cue
is also low confidence. If neither qualifies or the larger estimate is more
than 35% above the smaller, the estimator returns the face-led baseline and
its confidence, with a fallback reason. Projected lengths still vary with
camera angle, posture, and individual anatomy.
