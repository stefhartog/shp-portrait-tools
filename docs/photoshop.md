# Photoshop Skin Tools connector - source preview

## Status and scope

For where Photoshop capture and Send fit into the rest of the backend, see the
[Skin Tools developer guide](skin-tools-architecture.md).

This branch implements a local UXP connector and a separate Skin Tools editor adapter.
The user confirmed live image capture and layer sending with version 0.1.3.
Version 0.1.6's experimental native Hue Color Lookup failed in Photoshop with
`The command "Set" is not currently available`. Version 0.1.7 reached verification
but rejected the returned profile. A separate disposable Photoshop diagnostic
returned the exact fixture as a direct ArrayBuffer and changed 24 of 32 synthetic
pixels. Version 0.1.8 accepts that observed representation while still validating
the actual transform. The user subsequently confirmed native Hue works and looks
acceptable with 0.1.8. This does not establish exact color fidelity, masks,
rollback, every profile or bit depth, or a measured speedup. The
cause of the 0.1.6 failure is not proven. Version 0.1.5's folder picker and the
multiple-profile cache have not been tested live.
Use only new disposable documents until the live checklist below passes.
No existing documents, photos, Lightroom catalogs or primary checkout were changed.
After the user's approval, Adobe Developer Mode was enabled for packaging; the
settings-file change and undo command are recorded below.

The target is Windows desktop Photoshop **28.0**, with an installed, licensed copy.
The plugin declares that minimum version; compatibility with other versions is not
validated. No cloud image processing, Photoshop source changes, special launcher or
PhotoCraft-compatible Photoshop server is involved. AI Fill is a separate app and not involved.

Implemented source paths:

- `plugins/photoshop`: manifest v5 panel, HTTP client, validation and disposable smoke script.
- `shared/photoshop.py`: bounded binary command broker, pairing and DPAPI credential store.
- `modules/skin/photoshop_bridge.py`: capture and Skin Tools layer-bundle adapter.
- Skin Tools **Settings & Info > connection**: Photoshop / PhotoCraft / Standalone, pairing code and
  revocation.

These are relative to the repository; Python modules live under
`src/shp_portrait_tools`.

If tabs remain disabled after selecting Photoshop in an older UI, save or finish
Tools work and use the tray's **Reload UI** after updating the Skin Tools UI file
(Python changes need a Tools restart). Reload resets unsent previews; it does not
restart Photoshop.

## Current limitations (not hidden conversions)

- Only **opaque RGB 8/16-bit composites**, up to 40 megapixels and 2 GiB per bundle.
  CMYK, Lab, 32-bit/HDR, untagged sources, transparent/trimmed canvases and non-opaque
  samples fail explicitly. Missing regions are not moved to the origin or matted white.
  Transparency support and padded-alpha processing remain future work.
- Capture and import use full-resolution chunky RGB and Photoshop's **full 0..65535
  range** for 16-bit. Python/OpenCV's BGR ordering is reversed only at the boundary.
  An 8-bit target is explicitly quantized by rounding to 0..255; it cannot retain
  16-bit precision. Use a disposable RGB16 copy for fidelity work.
- The default Get Image path uses raw pixel tiles. An experimental **PNG copy**
  option creates a merged temporary Photoshop duplicate, exports its lossless PNG,
  and reads the embedded ICC instead of asking for a manually selected profile.
  The PNG must be full-size RGB8/16, opaque and contain a valid RGB ICC matching
  the document's profile description; otherwise it fails explicitly. No profile
  is guessed and there is no automatic raw fallback. Until a disposable Photoshop
  host test confirms exact pixels, embedded ICC and unchanged source state, use
  this option only with disposable documents. It requires the updated backend and
  connector. One user reported a first PNG capture succeeded and a second had no
  valid ICC; the precise cause is unconfirmed. Adobe's documented PNG save options
  provide no ICC embedding switch, and an older Photoshop bug report describes
  first-save-only ICC embedding. Use raw capture if PNG capture fails.
- **Calculation timings** in Skin Tools shows connection, capture and transfer,
  input encoding and decoding, profile preparation, face and body detection,
  landmarks, masks and preview stages. Durations labeled **total** include their
  child stages and should not be added to them. Detection timings include any
  model initialization; capture and transfer are combined, and do not separately
  measure Photoshop's modal or pixel-read time. These wall-clock timings do not
  isolate GPU synchronization.
- Raw Get still receives bounded chunks into temporary request files. Tools uses
  the verified pixels and ICC directly for analysis and keeps no inbox PNG.
  Debug Save bundles identify the Photoshop document in `settings.json` without
  linking to a source file. The internal **PNG copy** diagnostic uses the inbox PNG path; it is not exposed in Settings & Info.
- Photoshop Get checks the connector as part of the capture request rather than
  sending a separate status command first. Capture still checks the document and
  connector and reports any errors; PhotoCraft's connection check is unchanged.
- For raw capture, the Imaging API gives a profile **name**, not embedded ICC bytes. Select the exact
  source ICC file for each document profile in the connector. Multiple selections
  are cached in plugin-owned storage; one matching name is reused automatically.
  If several cached files match, explicitly choose the exact file for the current
  document, or add it again. A choice in that ambiguous case must be reconfirmed
  if the document's history changes. The previous single cached ICC is preserved
  as an unassigned selection until explicitly confirmed for the current document.
  Tools checks that it is an RGB ICC whose description exactly matches the source,
  retains its bytes and hashes that selection for subsequent output. No OS-wide
  profile search, guessed sRGB profile or silent name alias is used.
  **Matching descriptions are not proof that an independently supplied ICC is
  byte-identical to Photoshop's embedded profile**; correct file selection is a user
  requirement. The Imaging API does not expose the embedded profile bytes directly.
- FS & Tone retains native numeric source pixels. The existing analysis preview may
  convert other RGB profiles to sRGB; that is reported by the existing colour note.
  D&B and Combine sending are blocked if analysis changed the profile. They currently
  require the existing native sRGB/Adobe RGB processing paths. No converted D&B
  correction is labelled as if it were native-source data.
- Returned Color, Blur, Directional Blur, Detail and D&B are individually editable
  pixel layers. Hue uses **Native Color Lookup**, a generated RGB device-link
  ICC adjustment which updates when lower layers change. The rendered-pixel
  alternative is retained for development only. Native output may differ visibly
  from the preview due to interpolation or host handling and has no guaranteed
  speedup. It requires the updated connector and Tools backend.
  If Photoshop rejects the action or cannot return matching transform data, the
  import requests rollback and reports any rollback failure; it never silently
  switches methods.
- Normal, Soft Light, Linear Light, Pass Through, nested groups and gray8 masks are
  represented. Group-mask support relies on `putLayerMask` and remains **live unverified**;
  failure is supposed to roll back, not silently omit the mask.
- The panel must be loaded and explicitly connected. Hidden-panel persistence,
  reopen, Photoshop restart and packaged lifecycle behavior remain unverified.
  There is no promise of automatic headless/background startup.
- Connector 0.1.10 allows repeated Sends from the original Get, appending groups
  that can stack. Photoshop content edits may not be detected; Get again to use
  the current composite. A reconnect requires a new Get.
- The native FS diagnostic remains PhotoCraft-only.
  Files and supported PhotoCraft transfers use lossless PNG; Photoshop uses
  raw lossless binary tiles for editable layers. Processing math is shared and unchanged.

## Development setup and pairing

This checkout has connector 0.1.14 source (matching the packaged CCX below). Adobe UXP Developer Tool
(UDT) was not discovered in the checked Adobe installation locations. The official
CLI service can package without loading into Photoshop. Package or manifest
validation alone does not establish rendering fidelity.

1. Install/open Adobe **UXP Developer Tool** through Adobe's supported workflow.
   If Photoshop/UDT requests developer-mode or connection consent, decide and
   approve it manually. Do not copy plugin files into Photoshop system folders.
2. In UDT choose **Add Plugin / Add Existing Plugin**, select
   `plugins/photoshop/manifest.json`, and choose **Load** with Photoshop 28.0 running.
   Open **Plugins > Skin Tools Connector** in Photoshop.
3. The normal Windows shortcut starts the same Skin Tools service. It must target
   an installed release containing these changes; quit the old Tools instance
   normally before relaunching. See the session setup guide for this machine's
   persistent release, shortcut backups and rollback. The standard Skin Tools
   service is `http://127.0.0.1:18765`. Updating a shortcut does not update an
   already-running service: finish work, quit Tools from its tray and close
   other Tools browser windows, then open the updated shortcut. Do not use the
   old tray's Restart or Reload UI to switch installations. For a developer start:

   ```powershell
   python -m uv run shp-skin-tools --port 18765
   ```

   The connector uses `http://localhost:18765`, while Tools remains bound to
   `127.0.0.1`. An older Tools process must be restarted from an updated checkout
   to accept that exact Host header. Do not start a second listener on the port.
4. For raw capture, open the intended document. In the connector, choose **Add exact ICC for active
   document** and select its exact ICC file. Alternatively, choose **Add ICC from
   saved profile folder** and select a folder once when prompted. On Windows the
   Adobe profiles may be in `C:\Program Files (x86)\Common Files\Adobe\Color\Profiles\Recommended`.
   UXP remembers the user-granted folder and suggests it in later ICC pickers; the
   system picker may choose a different initial location. Use **Choose or change
   profile folder** if the saved grant expires. The folder picker grants access to
   its contents; this connector uses it only as a picker location and reads the
   file you explicitly choose. Manual browsing remains available. The previously cached ICC is available
   as **Previously selected ICC**; click it to explicitly associate it with the
   active document. In Tools choose **Settings & Info > connection > Photoshop**,
   then **Create pairing code**.
5. In Photoshop paste that code and click **Pair with local Tools**. The code is
   single-use, expires in 120 seconds and allows at most five guesses. The panel
   must display **Connected**.
6. With that **new disposable**, opaque RGB8 or RGB16 document and its explicit
   matching profile active, use **Get Image** in Tools, process it, then **Send Layers**
   (on the Skin Mask tab it adds the mask layer). The flattened RGB16 result is no longer
   offered for editors; it is Standalone's **Send Result**, saved as a local file.
   Nothing saves the Photoshop document.

Pairing persists across launches using Windows user-bound DPAPI in Tools and UXP
secureStorage in the plugin. After a Photoshop restart, open the panel: it
automatically resumes the saved pairing and keeps retrying while Tools is
offline; no Connect / resume click is needed. The manual button remains available
after **Disconnect locally**. Automatic connection starts only once the panel code
is running; it does not launch Tools or create a pairing. Saved ICC files are
restored from plugin-owned storage.
**Disconnect locally** stops panel work and discards snapshots, but retains pairing.
**Forget pairing** in Tools revokes the stored credential and target. Pair again to
connect. It is not a promise to interrupt a modal import already being committed.
Editor selection is saved in existing Tools settings and restored on service restart.

If Get reports an ICC mismatch or ambiguity, select the exact matching file for
the current document and Get again. If a timeout/lost response occurs, **inspect the document
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
identity again *inside* the modal scope, and refuses closed, resized,
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
  resized source, duplicate suppression, ICC cache migration, matching and ambiguity.
  Mocks are **not host rendering evidence**.
- Editor selection tests use temporary settings, including an isolated Windows
  service restart, and do not alter the user's configured editor.
- The `test_skin_tools.SkinApiTests` fake bridges now accept `mask_placement`, so
  the two send-with-ICC tests that used to fail on that argument run again.
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
complete Get -> FS & Tone/D&B/Combine -> Send; gray masks and
group masks; one Undo; cancellation and partial-import rollback; native sRGB and
Adobe RGB plus deliberate custom-profile rejection/mismatch; 8/16-bit quantization;
actual composite tolerances (not yet measured for the full Photoshop stack);
tab switching/edit/resize/close/modal conflicts; lost response/reconnect; both app
restarts; hide/reopen behavior; and real 24-40 MP memory/time.

## Packaging and non-developer installation

Download the [Photoshop connector 0.1.14 CCX](../plugins/photoshop/releases/0.1.14/shp.skin-tools.connector_PS.ccx)
from the repository (use **Download raw file** on GitHub), or use the same file
from a clone. Its SHA-256 is
`9252b70b6088b84bdf4367c6dbd78fcada75c89a835233dfd8b758bdb185e89e`.
The official Adobe CLI validated the package for Photoshop 28.0.

On a system with Photoshop 28.0 or later, open the `.ccx` with **Creative Cloud
Desktop** and approve the installation prompts. UXP Developer Tool is not
required. Open the **Skin Tools Connector** panel in Photoshop, then follow the
pairing instructions above with Tools running on the same machine.

For each connector release, commit the Adobe-generated
`shp.skin-tools.connector_PS.ccx` under `plugins/photoshop/releases/<manifest-version>/`
alongside the corresponding source, and update the link and SHA-256 here.
The source ZIP is not an installer; do not rename a ZIP to CCX.

The user confirmed capture and layer sending with
0.1.3, and native Hue in 0.1.8; repeated Send, PNG copy and the folder shortcut remain unverified
in Photoshop.

The source bundle overlays this repository's source paths; it is not a standalone
Tools application distribution.

The official `@adobe/uxp-devtools-cli` 1.2.0 was installed in session-local storage.
Its package omitted a runtime `tar` dependency; Adobe's own native-helper extraction
then succeeded after supplying `tar` and the referenced helper package locally.
The official `@adobe/uxp-devtools-cli` 1.2.0 and Adobe native helper were used from
session-local storage. Adobe's signed helper created
`%CommonProgramFiles%\Adobe\UXP\Developer\settings.json` with exactly
`{"developer": true}`. That file did not exist beforehand; this was the only global
developer setting changed, after explicit user approval. The Developer Service ran
on `127.0.0.1:14001` during packaging and has been stopped after use.

### Transfer timings

After Get Image or Send Layers, open **Settings & Info → Photoshop transfer timings**.
The panel keeps the latest breakdown for each direction. Get's transfer total
ends after Tools validates and prepares the input (including staging the file
in PNG-copy mode). The broader Load/Get total also includes analysis and is
shown separately.
If Get reports a missing progress element, quit and reopen the updated Tools
from the tray. This fix is in Tools; an existing 0.1.12 connector can stay installed.

- **Get** separates the bridge round trip from Tools input validation/preparation.
  PNG-copy mode also reports Tools PNG encoding and disk write. Connector 0.1.12 adds raw pixel capture,
  serialization, and upload/receipt inside the bridge span. PNG-copy Get
  reports Photoshop's combined PNG encode/write call, temporary-file reading,
  and upload instead.
  Connector 0.1.14 and updated Tools split raw serialization into Adobe
  `getData()` readback and connector validation/RGB packing. The raw report
  shows dimensions, depth, source channels, separate pixel and ICC byte counts,
  and an expandable per-tile breakdown. Tile times are included in the aggregate
  rows. Only the RGB payload is uploaded; RGBA readback still requires opaque alpha.
- **Send Layers** separates Tools preparation, serial payload encoding/staging,
  and the bridge round trip. Per-layer and group-mask encoding rows are included
  in the encoding wall time. Connector 0.1.12 splits the bridge span into
  download, temporary staging, and modal import/layer creation.

Indented rows are included in their parent. Totals can also include unlisted
overhead such as polling and setup. Bytes are payload sizes, not HTTP traffic;
the same bytes can appear during encoding, transfer, and disk write, so don't
add them together. Older connectors still show the Tools-side timings and bytes.

Adobe CLI's `devtools disable` command overwrites the settings file with
`{"developer": false}`. If the file has since acquired other settings, back up and
preserve them before using it. Developer Mode is not needed to install or use the
CCX. When the file still contains only `developer`, disable it with any of:

- **UXP Developer Tool** (Creative Cloud Desktop > All apps): turn Developer Mode off.
- Edit `%CommonProgramFiles%\Adobe\UXP\Developer\settings.json` as administrator so
  it reads `{"developer": false}`.
- The Adobe CLI used for packaging, if a copy is still available:
  `node <uxp-devtools-cli>\src\uxp.js devtools disable` (requests administrator approval).
  It was run from a temporary tool-session folder outside this repository, so that copy
  may no longer exist.

For developer loading, UXP Developer Tools is also available in Creative Cloud
Desktop > **All apps**. It requires administrator privileges and prompts for
Developer Mode on first launch.

The stable ID `shp.skin-tools.connector` is an independent-development ID, not an
Adobe Marketplace listing or entitlement.
Marketplace publication needs the portal-assigned ID; do not claim that listing.
Per current Adobe documentation independent distribution can use a separate stable ID.

Further live testing of profile reuse and migration should use new disposable documents.
Exact compositing, masks, Undo, panel lifecycle and the revised cache remain
unverified. A source preview or successful
archive check is not a substitute for that acceptance criterion.

Adobe references:

- [UXP file picker and persistent tokens](https://developer.adobe.com/photoshop/uxp/2022/uxp-api/reference-js/modules/uxp/persistent-file-storage/file-system-provider)
- [Imaging API](https://developer.adobe.com/photoshop/uxp/2022/ps-reference/media/imaging)
- [Modal execution / history rollback](https://developer.adobe.com/photoshop/uxp/2022/ps-reference/media/executeasmodal)
- [Document DOM](https://developer.adobe.com/photoshop/uxp/2022/ps-reference/classes/document)
- [Add an existing plugin in UDT](https://developer.adobe.com/photoshop/uxp/2022/guides/devtool/plugin-management)
- [Supported CCX packaging](https://developer.adobe.com/uxp/guides/how-to/distribution/package/)
