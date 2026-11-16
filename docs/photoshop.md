# Photoshop Skin Tools connector - source preview

## Status and scope

This branch implements a local UXP connector and a separate Skin Tools editor adapter.
**It has not been loaded or run in real Photoshop. It is not a finished CCX release.**
Use only new disposable documents until the live checklist below passes.
No Photoshop preferences, existing documents, photos, Lightroom catalogs or primary
checkout were changed during implementation.

The target is Windows desktop Photoshop **28.0**, with an installed, licensed copy.
The plugin declares that minimum version; compatibility with other versions is not
validated. No cloud image processing, Photoshop source changes, special launcher or
PhotoCraft-compatible Photoshop server is involved. Crop/AI Fill remains unchanged.

Implemented source paths:

- `plugins/photoshop`: manifest v5 panel, HTTP client, validation and disposable smoke script.
- `shared/photoshop.py`: bounded binary command broker, pairing and DPAPI credential store.
- `modules/dnb/photoshop_bridge.py`: capture and Skin Tools layer-bundle adapter.
- Skin Tools **Settings & Info > Source editor**: editor choice, pairing code and revocation.

These are relative to the repository; Python modules live under
`src/shp_photocraft_tools`.

## Current limitations (not hidden conversions)

- Only **opaque RGB 8/16-bit composites**, up to 40 megapixels and 2 GiB per bundle.
  CMYK, Lab, 32-bit/HDR, untagged sources, transparent/trimmed canvases and non-opaque
  samples fail explicitly. Missing regions are not moved to the origin or matted white.
  Transparency support and padded-alpha processing remain future work.
- Capture and import use full-resolution chunky RGB and Photoshop's **full 0..65535
  range** for 16-bit. Python/OpenCV's BGR ordering is reversed only at the boundary.
  An 8-bit target is explicitly quantized by rounding to 0..255; it cannot retain
  16-bit precision. Use a disposable RGB16 copy for fidelity work.
- The Imaging API gives a profile **name**, not embedded ICC bytes. Select the exact
  source ICC file once in the connector. It is cached in plugin-owned storage.
  Tools checks that it is an RGB ICC whose description exactly matches the source,
  retains its bytes and hashes that selection for subsequent output. No OS-wide
  profile search, guessed sRGB profile or silent name alias is used.
  **Matching descriptions are not proof that an independently supplied ICC is
  byte-identical to Photoshop's embedded profile**; correct file selection is a user
  requirement. Direct embedded-profile extraction is not implemented.
- FS & Tone retains native numeric source pixels. The existing analysis preview may
  convert other RGB profiles to sRGB; that is reported by the existing colour note.
  D&B and Combine sending are blocked if analysis changed the profile. They currently
  require the existing native sRGB/Adobe RGB processing paths. No converted D&B
  correction is labelled as if it were native-source data.
- Returned Color, Blur, Directional Blur, Detail, D&B and Hue are individually editable
  **pixel layers**, with group/per-layer masks, opacity and blend modes. Hue uses the
  existing trilinear LUT sampler to render its pixels, rather than an undocumented
  Photoshop Color Lookup action descriptor. Editing a lower layer does not recompute
  Hue. A live editable LUT adjustment is not implemented.
- Normal, Soft Light, Linear Light, Pass Through, nested groups and gray8 masks are
  represented. Group-mask support relies on `putLayerMask` and remains **live unverified**;
  failure is supposed to roll back, not silently omit the mask.
- The panel must be loaded and explicitly connected. Hidden-panel persistence,
  reopen, Photoshop restart and packaged lifecycle behavior remain unverified.
  There is no promise of automatic headless/background startup.
- Each successful import consumes its captured snapshot. **Get Image again after
  each Send**, even if only changing settings. This deliberately prevents reusing a
  stale target or inserting duplicate corrections on top of a modified source.
- Native FS diagnostic and watch-folder editor handoff remain PhotoCraft-only.
  PNG/TIFF/compression transfer options remain for PhotoCraft/files; Photoshop uses
  raw lossless binary tiles. Processing math is shared and unchanged.

## Development setup and pairing

This checkout has source, not a tested installer. Adobe UXP Developer Tool (UDT)
was not discovered on PATH or in the checked Adobe installation locations.
Available automation cannot click Photoshop/Creative Cloud consent or safely
enable developer preferences. Those actions must be performed by the user.

1. Install/open Adobe **UXP Developer Tool** through Adobe's supported workflow.
   If Photoshop/UDT requests developer-mode or connection consent, decide and
   approve it manually. Do not copy plugin files into Photoshop system folders.
2. In UDT choose **Add Plugin / Add Existing Plugin**, select
   `plugins/photoshop/manifest.json`, and choose **Load** with Photoshop 28.0 running.
   Open **Plugins > Skin Tools Connector** in Photoshop.
3. Start the Tools checkout normally. Its standard Windows Skin Tools service is
   `http://127.0.0.1:18765`. For an isolated developer start without the desktop host:

   ```powershell
   python -m uv run shp-photocraft-dnb --port 18765
   ```

   Do not start a second listener if Tools already owns that port. The connector
   permits only this exact local endpoint, not arbitrary ports or remote hosts.
4. In the connector, **Select exact source ICC profile** (only the selected file is
   read). In Tools choose **Settings & Info > Source editor > Photoshop (UXP preview)**,
   then **Create pairing code**.
5. In Photoshop paste that code and click **Pair with local Tools**. The code is
   single-use, expires in 120 seconds and allows at most five guesses. The panel
   must display **Connected**.
6. Create a **new disposable**, opaque RGB8 or RGB16 document, with an explicit
   matching profile. Use **Get Image** in Tools, process it, then **Send Layers** or
   **Send Result**. Nothing saves the Photoshop document.

Pairing persists across launches using Windows user-bound DPAPI in Tools and UXP
secureStorage in the plugin. After Photoshop restart, open the panel and click
**Connect / resume**; the selected ICC is restored from plugin-owned storage.
**Disconnect locally** stops panel work and discards snapshots, but retains pairing.
**Forget pairing** in Tools revokes the stored credential and target. Pair again to
connect. It is not a promise to interrupt a modal import already being committed.
Editor selection is service-local and resets to PhotoCraft when Tools restarts.

If Get reports an ICC mismatch, disconnect, select the exact matching file,
reconnect and Get again. If a timeout/lost response occurs, **inspect the document
before doing anything else**. The connector does not silently replay a mutation.

## Small disposable real-API check (user action required)

`plugins/photoshop/disposable-smoke.psjs` can be manually run using Photoshop's
**File > Scripts > Browse** UXP-script workflow. It creates a new, unsaved 8x4 RGB16
document with an explicit sRGB profile and modifies only that document. It never
opens, activates or closes another document, changes preferences or saves a file.

The script writes a ramp, captures the composite with full-range samples, adds
one masked inverse-ramp pixel layer and compares the resulting composite. It reports
maximum capture error (required <= 2/65535) and masked Normal composite error
(required <= 4/65535). On failure it throws and reports the error, rather than
printing success. One Undo should remove the two test layers. The document is
left open for inspection.

**This script was syntax-checked only, not run in Photoshop.** It does not validate
the connector transport, group masks, Soft Light/Linear Light or full workflows.

## Wire contract and safety

Protocol 1 is exposed at `/api/photoshop/` on the **existing** Skin Tools HTTP server,
outside its processing lock so the plugin can answer while analysis requests wait.
The host binds IPv4 loopback; handlers additionally check peer and Host, reject
browser origins except UXP's absent/opaque `null` origin, expose no CORS grant, and
require a bearer credential and connector session on all operational requests.
Pairing requires the explicitly generated unguessable short-lived code.
Tools' editor/pairing UI uses same-origin JSON POSTs.

The plugin connects out; it never listens. Routes are limited to pair, hello, poll,
reply, chunk and request status. There is no filesystem path, executable, arbitrary
JavaScript, save/close or generic Photoshop command API.

Commands are status, capture and apply, each with a request ID, protocol and plugin
session. Captures carry document ID, dimensions, depth, profile, alpha policy,
snapshot token and a history-state fingerprint. Capture checks that fingerprint
before/after reads. Import resolves the **original document ID**, checks its
fingerprint again *inside* the modal scope, and refuses closed, resized, edited,
mode/depth/profile-changed or expired-session sources. Switching tabs cannot choose
a different target.

RGB and mask buffers use ordered binary chunks <= 2 MiB, never JPEG/base64 or JSON
sample arrays. Python spools a single operation to a private per-request temporary
directory; identifiers cannot contain paths. Declared shapes and byte counts are
checked. UXP downloads/validates all output to plugin-owned temporary tiles before
entering `executeAsModal`, and reads/disposes one tile at a time during import.
No analysis or network wait occurs inside the import modal scope.

Import suspends history for one coherent result and requests rollback on error or
cancellation; actual host rollback/Undo still needs live testing. The previous
active tab is restored after import. A delivered command is never polled twice,
completed outcomes are retained (last 32, no pixel payloads), uncertain timeouts are
not retried, and plugin restart invalidates snapshots. Temporary image buffers are
disposed and temporary tiles removed after each attempt; abrupt process crashes
can bypass normal cleanup.

## Automated evidence and remaining live checklist

Automated checks run without desktop Photoshop or user photographs:

- 13 Python tests: pairing expiry/rate limit/revoke, real DPAPI encrypted round trip,
  loopback HTTP Host/Origin/auth/session guards, bounded ordered transfers, timeout/
  restart/no-replay, RGB8/16 ramps, RGB/BGR order, ICC bytes, malformed sizes, layer
  order/modes/masks/opacity, existing LUT math, editor isolation and replies while
  the Tools processing lock is held.
- Node host-free UXP mocks: full-range capture, alpha/trim rejection, nested ordering,
  original target after tab switch, buffer disposal, rollback/cancel, stale/closed/
  resized source and duplicate suppression. Mocks are **not host rendering evidence**.
- Focused existing suite: 69 tests, 9 skips, **2 existing failures** in
  `test_skin_tools.SkinApiTests` whose fake bridges do not accept `mask_placement`.
  The same failures were reproduced with the unchanged HEAD fetch/send functions.
  No unrelated processing/test-double fix was included.
- Synthetic 6000x4000 RGB16 two-band + mask serialization: **312,000,000 bytes**,
  **0.374 s**, process working set before **203.7 MiB**, peak **208.2 MiB** on this
  Windows host. Two bands reused the same input array. This measures serialization,
  not analysis, Photoshop, a real portrait, disk durability or network time.
  UXP/network throughput and real Photoshop memory remain unmeasured.

Reproduce the focused new checks from the checkout:

```powershell
python -m uv run python -m unittest discover -s tests -p test_photoshop.py -v
node tests\photoshop_plugin.cjs
```

If Node is absent, the validation here used
`python -m uv tool run --from nodejs-wheel node tests\photoshop_plugin.cjs`.
The isolated environment lacks CUDA DLLs and emits existing ONNX Runtime warnings;
these host-free checks do not run neural analysis or claim GPU validation.

Before calling this production-ready, on **new disposable documents only**, verify
the smoke script; complete Get -> FS & Tone/D&B/Combine -> Send; gray masks and
group masks; one Undo; cancellation and partial-import rollback; native sRGB and
Adobe RGB plus deliberate custom-profile rejection/mismatch; 8/16-bit quantization;
actual composite tolerances (not yet measured for the full Photoshop stack);
tab switching/edit/resize/close/modal conflicts; lost response/reconnect; both app
restarts; hide/reopen behavior; and real 24-40 MP memory/time.

## Packaging and non-developer installation (blocked)

The supplied ZIP is a **source bundle**, not an installer. Renaming a ZIP to CCX
is not Adobe's supported packaging workflow and was not done.
The source bundle overlays this repository's source paths; it is not a standalone
Tools application distribution.

The official `@adobe/uxp-devtools-cli` alternative was also attempted in an isolated
temporary npm environment. Its published package omitted required `tar` and
`@adobe/uxp-devtools-app` dependencies; supplying those reached a further missing
`yarn` build step. No developer-access enable command was run. Rather than modify
Adobe's toolchain or user preferences, packaging is left to supported UDT loading/
packaging with user consent. These failures are not Photoshop plugin test results.

Once live validation passes, use UDT's plugin **Actions > Package**, selecting
this manifest, to produce the real `.ccx`. The stable ID `shp.skin-tools.connector`
is an independent-development ID, not an Adobe Marketplace listing or entitlement.
Marketplace publication needs the portal-assigned ID; do not claim that listing.
Per current Adobe documentation independent distribution can use a separate stable ID.

Test a fresh install by double-clicking the UDT-produced CCX through Creative Cloud
Desktop, approving the requested permissions manually, then opening the connector
from Photoshop's Plugins menu. Test pairing, normal launches and update/revoke again
without UDT. **No CCX has been produced, installed or verified here**. User-assisted
UDT loading, consent and packaged installation are the exact remaining blockers;
a source preview is not a substitute for that acceptance criterion.

Adobe references:

- [Imaging API](https://developer.adobe.com/photoshop/uxp/2022/ps-reference/media/imaging)
- [Modal execution / history rollback](https://developer.adobe.com/photoshop/uxp/2022/ps-reference/media/executeasmodal)
- [Document DOM](https://developer.adobe.com/photoshop/uxp/2022/ps-reference/classes/document)
- [Add an existing plugin in UDT](https://developer.adobe.com/photoshop/uxp/2022/guides/devtool/plugin-management)
- [Supported CCX packaging](https://developer.adobe.com/uxp/guides/how-to/distribution/package/)
