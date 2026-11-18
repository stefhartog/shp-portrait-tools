# Skin Tools developer guide

Skin Mask, FS & Tone, Dodge & Burn (D&B), and Combine share one service in
[`modules/dnb`](../src/shp_photocraft_tools/modules/dnb). Start with the boundary
that owns the behavior you want to change, not with the whole server.

| Change you want to make | Where to start |
|---|---|
| An HTTP route, a request setting, or a button | [`ui_server.py`](../src/shp_photocraft_tools/modules/dnb/ui_server.py) wires routes, validates requests, and retains server-side editor and provenance checks. [`ui/index.html`](../src/shp_photocraft_tools/modules/dnb/ui/index.html) is the browser UI. |
| What gets replaced on Load/Get, or what makes a snapshot stale | [`image_session.py`](../src/shp_photocraft_tools/modules/dnb/image_session.py) publishes the source, advances its generation, resets source-derived state, and captures full-resolution inputs by reference. Decode, profile handling, and source analysis still run through `ui_server.py` before publication. |
| Preview response, tab switching, or preview cache reuse | [`preview_service.py`](../src/shp_photocraft_tools/modules/dnb/preview_service.py) coordinates `/api/run`, decides whether to keep or queue background preparation, and attaches status. The preview renderers, preview-size keys, and component cache helpers remain in `ui_server.py`. |
| Full-resolution composition, shared mask use, or editable layer pixels | [`processing_service.py`](../src/shp_photocraft_tools/modules/dnb/processing_service.py) assembles results and layers through shared mask, split, and band helpers. `ui_server.py` still supplies those helpers, their keys, and the full-mask cache. Algorithms live in [`skin_mask.py`](../src/shp_photocraft_tools/modules/dnb/skin_mask.py), [`frequency_separation.py`](../src/shp_photocraft_tools/modules/dnb/frequency_separation.py), [`skintone.py`](../src/shp_photocraft_tools/modules/dnb/skintone.py), [`directional_blur.py`](../src/shp_photocraft_tools/modules/dnb/directional_blur.py), and [`dodge_burn.py`](../src/shp_photocraft_tools/modules/dnb/dodge_burn.py). |
| Background scheduling, cache retention, or readiness status | [`cache_coordinator.py`](../src/shp_photocraft_tools/modules/dnb/cache_coordinator.py) owns priority, job status, and stale-work coordination. [`mask_precache.py`](../src/shp_photocraft_tools/modules/dnb/mask_precache.py) runs the debounced, coalescing workers for both mask and layer jobs. [`layer_cache.py`](../src/shp_photocraft_tools/modules/dnb/layer_cache.py) owns bounded full-resolution component storage, not scheduling or pixel math. |
| Save artifacts, local Send, or editor dispatch | [`transfer_service.py`](../src/shp_photocraft_tools/modules/dnb/transfer_service.py) writes Save bundles, encodes results for [`shared/local_output.py`](../src/shp_photocraft_tools/shared/local_output.py), and dispatches prepared payloads. [`photoshop_bridge.py`](../src/shp_photocraft_tools/modules/dnb/photoshop_bridge.py) and [`photocraft_bridge.py`](../src/shp_photocraft_tools/modules/dnb/photocraft_bridge.py) own editor-specific capture and import. |

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
