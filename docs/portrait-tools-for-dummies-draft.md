# SHP Portrait Tools for Dummies

> **DRAFT for review - 10 November 2026.** This guide describes the repository's
> current Linux implementation described in the coding session's updated README,
> HANDOFF and MODELS on 10 November 2026, including its **uncommitted** rename and
> AI Fill changes based on `6e23398`. Those changes are not in that commit itself.
> This isolated manual worktree still contains older base docs/code; review the
> draft alongside the coding session's updated canonical docs, not as evidence
> that the older checkout contains these features. Instructions were checked
> against docs/source, not by operating the app or processing photographs.
>
> **SHP Portrait Tools** is the public product name. Existing `shp-photocraft-*`
> commands and config/data paths keep their old names for compatibility.
> **PhotoCraft** below means the optional editor integration.

## In this draft

1. [What the tools do](#1-what-the-tools-do)
2. [What must be set up first](#2-what-must-be-set-up-first)
3. [Your first portrait](#3-your-first-portrait)
4. [Understanding the controls](#4-understanding-the-controls)
5. [Sending and saving](#5-sending-and-saving)
6. [AI Fill: Crop & Expand and Patch Fill](#6-ai-fill-crop--expand-and-patch-fill)
7. [Processing a folder with Combine](#7-processing-a-folder-with-combine)
8. [Optional PhotoCraft export inboxes](#8-optional-photocraft-export-inboxes)
9. [Troubleshooting and help](#9-troubleshooting-and-help)
10. [Questions for the next draft](#10-questions-for-the-next-draft)

## 1. What the tools do

Think of the app as two workspaces: **Skin Tools** for portrait adjustments, and
**AI Fill** for generating extra surroundings or replacing painted areas.

| Tab | What it is for | A good first use |
| --- | --- | --- |
| **Skin Mask** | Defines where the skin corrections apply and shows image analysis. A mask is a grayscale selection: white allows correction, black protects, gray allows some. | Check the selected person, skin coverage and scale before adjusting effects. |
| **FS & Tone** | Separates broad color/shading from fine detail. FS means *frequency separation*. Offers additional Blur, Directional Blur and hue-based Skintone Correction. | Compare **Original** and **Result**, then adjust one effect at a time. |
| **Dodge & Burn** | Adjusts local brightness differences through the shared mask, using one or two independently controlled bands of feature sizes. D&B is the short form in mix and diagnostic labels. | Start with Band 1 and adjust its **Strength**. |
| **Combine** | Combines the configured FS and D&B effects with separate mix sliders. Also contains local batch processing. | Balance the two effects and inspect **Combined**. |
| **Crop & Expand** | AI Fill mode that repositions/resizes a frame and uses ComfyUI to generate areas outside the original picture. | Add a little space around a portrait. |
| **Patch Fill** | AI Fill mode that replaces painted areas inside the existing picture using FLUX.1 Fill. | Paint a small area, generate alternatives, then inspect the selected result. |
| **Settings & Info** | Global appearance, output/transfer options, mask precaching and calculation timings. | Keep defaults initially; choose output options before saving. |

The four skin tabs share **one loaded image and one settings bundle**. Switching
between them does not fetch another image or repeat its analysis. Settings hidden
in one tab still affect the other tabs. AI Fill has a separate source: do not
assume an image loaded there is also loaded in Skin Tools. Crop & Expand and
Patch Fill share that AI Fill source, ComfyUI connection and lifecycle. Switching
between them retains the mask, selected result preview and source-view zoom/pan;
loading a new source clears the painted mask.

The top tab order is **Skin Mask / FS & Tone / Dodge & Burn / Combine /
Crop & Expand / Patch Fill / Settings & Info**. Stopping Skin Tools hides its
four tabs; stopping AI Fill hides both of its tabs.

**Nothing is sent to PhotoCraft just because you move a slider.** Get Image takes
a merged copy for processing; Send Result or Send Layers is the deliberate step
that changes the fetched PhotoCraft document. Keep your original photograph and
save your PhotoCraft document separately.

## 2. What must be set up first

If someone has already installed a working Tools launcher, start there and go to
[Your first portrait](#3-your-first-portrait). If not, use the current
[README setup instructions](../README.md#setup); this manual does not replace the
installation checklist.

| Needed for | Requirements |
| --- | --- |
| Current application | Linux, Python 3.13, uv and the repository's locked dependencies. Windows support is not implemented or verified. |
| Any of the four skin tabs | The three manually supplied ONNX models listed in [MODELS](../MODELS.md), plus pose-model weights downloaded on first analysis or prepared ahead of time. Full CPU-only skin analysis is **not guaranteed**; the pose helper explicitly requests CUDA. |
| Native desktop window, tray and native file pickers | System GTK3, WebKit2-4.1 and AppIndicator3 bindings, a graphical session and suitable tray support. See README for the installer and distro package examples. |
| AI Fill generation | A separate, working ComfyUI installation with its own environment and models. Crop & Expand supports FLUX.2 dev, FLUX.1 Fill or FLUX.2 Klein; Patch Fill requires FLUX.1 Fill. Tools does not install these for you. |
| PhotoCraft integration only | A compatible PhotoCraft build, authenticated control/automation enabled at launch, matching token and permitted file-exchange directories, and an open document. Tools does not install or start PhotoCraft for you. |

**Optional, not a prerequisite for local skin processing:** PhotoCraft, ComfyUI,
ExifTool for Sony autofocus-point subject selection, and research/notebook
dependencies. ExifTool is not required merely to open a photograph. AI Fill does
require ComfyUI for generation even when its source is a local file.

Models are not bundled with dependency installation. Some pose weights download
only when analysis first runs; a window opening successfully does not prove that
models, GPU inference or offline use are ready. See [MODELS](../MODELS.md) for
exact filenames, preparation and GPU limitations. No minimum VRAM or processing
time is promised here.

**Optional experiment:** Settings & Info now has a **Pose analysis backend** choice.
Keep **rtmpose / rtmlib (default)** unless you deliberately want an experimental
alternative. Google Full needs its [separate isolated runtime and model](../MODELS.md#optional-google-pose-landmarker-full-experimental),
has a two-person cap, and runs in a network-blocked worker. Its runtime NOTICE
discloses metrics transmission; blocking egress does not disable telemetry
generation. OpenCV Zoo instead needs its [verified local model pair](../MODELS.md#optional-opencv-zoo-mediapipe-onnx-pose-backend-experimental)
and uses CPU OpenCV DNN in the Tools process. Neither is known to improve
accuracy; positive portrait quality is untested. Both omit the default model's
whole-body eye-corner/hand observations, and neither silently falls back.
Saving a backend applies to the next Get/load or batch, not the current image.
The hint identifies the current image's backend. Retouching presets do not
change this global preference.

### Opening the application

Use your installed Tools launcher if available. The native window is the simplest
route for the file-picking and batch instructions in this guide.

For an already prepared checkout, standalone services can instead be started with:

```sh
uv run --no-sync shp-photocraft-dnb --dir /absolute/path/to/pictures
uv run --no-sync shp-photocraft-crop --dir /absolute/path/to/pictures --comfy http://127.0.0.1:8188
```

These are **alternatives**, not commands you must run alongside the installed
launcher. Skin Tools defaults to `http://127.0.0.1:8765/`; AI Fill defaults to
`http://127.0.0.1:8768/`. They do not manage ComfyUI or read the desktop launcher
configuration. Use the exact `127.0.0.1` address, not `localhost` or a LAN address.
The supervised shell normally uses `http://127.0.0.1:18760/`.

Standalone Skin Tools uses a **skin tool** selector rather than the shell's top
tabs. Native choosers are not available in a plain browser. AI Fill provides a
configured file list when disconnected. **Release caveat:** README describes
skin-tools path/list controls, but their container is hidden in the current UI
source. This draft therefore does not promise a working browser-only local
skin-image picker. Prefer the native desktop's disconnected Get Image route.
Combine's batch source chooser also requires the Tools desktop.
Standalone Patch Fill opens at `http://127.0.0.1:8768/?mode=patch`
(use the configured AI Fill server address if different).

## 3. Your first portrait

Start with one image, not a folder. The order below is a suggested learning
workflow, not a requirement to use every effect.

1. **Open Tools and select Skin Mask.** The shared window starts here when Skin
   Tools is loaded. Otherwise it falls back to Crop & Expand, then Settings & Info.
   If skin tabs are missing, start **Skin Tools** from the Tools tray menu;
   the four tabs are one service, not four installations.
2. **Get the image.** For PhotoCraft, make the intended document active and click
   **Get Image**. For a local file in the native desktop, Get Image opens a file
   chooser when PhotoCraft is disconnected. If PhotoCraft is connected, it fetches
   that document instead. For this draft's local-file workflow, use Get Image
   while PhotoCraft is disconnected; there is no verified always-visible
   local-open alternative in the current skin UI. Wait for analysis and previews.
   Cancelling a native chooser keeps the previous work.
3. **Check the analysis.** In Skin Mask, inspect **Mask** and **Analysis** on the
   right. Analysis offers **faces & bodies**, **skin** and **structure** overlays.
   Check the intended person and make sure important areas are not unexpectedly
   excluded. Detection is a starting point, not a guarantee of correct coverage.
4. **Check the scale.** Open **Image analysis** and look at **px / 63 mm**.
   This is scale S: the estimated image pixels spanning 63 mm at the subject.
   It lets size-based adjustments adapt to the photograph. If analysis cannot
   find a usable scale, correct it rather than inventing a number; see below.
5. **Try FS & Tone.** Compare **Original** with **Result** on the left.
   Start with the existing settings. Change one control, release it, then wait
   for the preview. Switch additional **Blur** or **Skintone Correction** off/on
   to understand its contribution. Leave Directional Blur off for this first pass.
6. **Try Dodge & Burn.** Keep Band 2 off initially. Adjust Band 1 **Strength** modestly and
   compare Original/Result. Judge the photograph, not the gray diagnostic layers.
7. **Balance in Combine.** Adjust **FS mix** and **D&B mix** between 0 and 100%.
   Inspect **Combined**. To change an ingredient itself, return to its own tab.
   You do not need to Get Image again.
8. **Choose the output deliberately.** Use **Send Result** for the flattened
   photograph, or **Send Layers** for editable PhotoCraft ingredients.
   A locally loaded image saves locally instead. See the destination rules below.
   Inspect the full-resolution saved/sent result before treating it as finished.

### If the scale estimate is wrong

Under **Skin Mask > Image analysis**, choose **Adjust scale landmarks**.
Select the **subject** and **point**; click to place a point, drag to move it, or
use arrow keys to nudge it. Eye centers mean the visible pupil centers.
Use **Mark missing** for an unavailable point and exclude unreliable or
foreshortened segments. **Undo**, **Reset** and **Cancel** are available.
Review the proposed scale and uncertainty, then choose **Apply scale**.

These edits adjust **scale only**. They do not rebuild the skin segmentation,
change the selected skin region, or repair Directional Blur's pose geometry.
They clear on a new image load. Directly typing **px / 63 mm** is also possible,
but only do that if you have a defensible scale value.

## 4. Understanding the controls

### Common habits

- Click a section heading to expand/collapse it. A section's switch enables its
  effect; collapsing the section is not the same as turning it off.
- Sliders update their number while dragged; image processing waits for release.
  Keyboard changes are briefly delayed to avoid processing every keystroke.
  Busy controls and navigation are temporarily locked; wait for completion.
- In skin previews, the mouse wheel zooms and dragging pans; the two panes stay
  synchronized. Double-click to fit again. During landmark editing, use
  **Alt-drag** to pan.
- Retouch previews start on **Result** (or **Combined**) at startup and after
  loading a new source; Skin Mask shows Original only. Hold **Ctrl + primary
  mouse button** in either pane to temporarily flip Original/Result. Release
  to restore the prior selection. Comparison is unavailable while processing,
  picking colors, editing landmarks or using Skin Mask.
- The preset selector has **Save**, **Save New** and **Delete**. Save overwrites
  the selected preset; Save New asks for a name. A star means settings differ
  from that preset. This **Save saves settings, not a finished photograph**.

Skin presets include the whole shared mask/FS/D&B/Combine bundle, including
settings hidden in another tab. Switching tabs alone does not mark a preset
changed. Crop & Expand and Patch Fill each have separate AI-settings presets.
Their prompts are saved separately too; the painted mask is not saved in a preset.
Presets do not contain your image
or save a PhotoCraft document. Global output settings are separate from presets.

### Skin Mask: where corrections are allowed

**Skin mask** limits correction to detected skin, with exclusions for facial
features and off-color regions. **feather mm** softens its edge inward.
**Structure protection** reduces correction on folds, creases and contours.
**Light range** limits correction by brightness; **Color range** limits it to
colors near a picked hue and saturation.

To pick a range, click its small swatch button, then click the **Original**
preview. Picking a color enables Color range; it is unavailable before a color
has been picked. Check **Mask** after changing these controls: a narrow range
can exclude more skin than expected.

These are shared settings. If an effect seems absent in FS, D&B or Combine,
return here before increasing its strength.

### FS & Tone: color, detail and smoothing

**Frequency separation** splits the image into **Color** (broad color/shading)
and **Detail** (fine information). The **method** choices are **gaussian**,
**median** and **median fast**. Median Fast is an alternative calculation, not
an identical result with a speed switch.

The separation radius is calibrated to scale S. A larger radius moves more
image information into Detail. The additional **Blur** has its own size and
strength controls. Turning Blur off **does not turn frequency separation off**:
Color and Detail remain.

**Skintone Correction** adjusts hue, not a separate saturation correction.
Its **Center**, **Range ±**, **Softness** and **Amount** define the target hue
range and correction strength. Normal Load/Get estimates Center again; batch
processing instead keeps the captured setting fixed. Compare against Original
and reduce Amount if the change is excessive.

**Directional Blur**, off by default, smooths along selected limbs or the torso.
The side selectors refer to the **person's anatomical left/right**, not the
screen side. Camera-facing rows put anatomical right on your left.
Each pair/torso has Distance, Width, Taper, Strength, Feather and Start/End controls
(the endpoint control is labeled **region %**). Width is across the limb's axis;
torso Width is a percentage of the detected shoulder/hip box.
Use the **Region** preview to check actual masked coverage.

A gray unavailable selector is not necessarily a failure. Missing selected
regions are skipped, and their saved selection is retained for another image.
Hover the selector or its label for the geometry reason. If none are usable,
Directional Blur is bypassed while other effects remain available.

### Dodge & Burn: two bands, one shared mask

A band selects a range of feature sizes to correct. Use its size-range/taper
controls and gain graph to understand which sizes are affected, and **Strength**
for how strongly it acts. Band 1 starts enabled; Band 2 starts disabled.
Both analyze the original image, not each other's output.

Band 2 **invert** reverses its layer to enhance rather than reduce tonal
differences. **Layer blur**, **edge** and **tau** are shared advanced controls;
leave them alone until you understand the band adjustment.

**Important:** **Band 1**, **Band 1 + mask**, **Band 2** and **Band 2 + mask**
diagnostic previews are always exaggerated **8 times** around neutral gray.
That is only a viewing aid. Result, saved photographs and sent layers use actual
strength. Band 2 preview tabs can remain visible when its contribution is off.

### Combine: judge the final mix

**FS mix** blends the configured FS effect with Original. **D&B mix** then
controls the D&B contribution on that base. Both start at 100%; these are mixes,
not replacements for the ingredient controls.

The right-hand **FS result** and **D&B result** show each configured ingredient
**before** these mixes, even when its mix is zero. Judge the mixed photograph in
**Combined**, not those ingredient previews.

## 5. Sending and saving

**The source determines the destination.** Connecting PhotoCraft later does not
turn a locally loaded image into a PhotoCraft source.

| Image was loaded from | Action | What happens |
| --- | --- | --- |
| PhotoCraft, in FS/D&B/Combine | **Send Result** | Adds the flattened corrected RGB16 photograph as a Normal layer in the fetched document. |
| PhotoCraft, in FS/D&B/Combine | **Send Layers** | Adds one editable **SkinTools** folder with enabled ingredients. |
| PhotoCraft, in Skin Mask | **Send Result** | Adds a grayscale skin-mask layer. |
| PhotoCraft, in Crop & Expand | **Send Result** | Changes the target canvas size and adds the selected generated area as a layer. This is not just a new same-size correction layer. |
| PhotoCraft, in Patch Fill | **Send Result** | Adds the selected masked RGBA patch layer without resizing the canvas. |
| Local file, in any module | **Send Result** | Saves a numbered result beside the source; does not overwrite the source or send it to PhotoCraft. Send Layers is not offered for local sources. |

Local names include `_shpfs_001`, `_shpdnb_001`, `_shpcombine_001`,
`_shpskinmask_001` or `_shpcrop_001`; the number advances to avoid overwriting.
FS/D&B/Combine results are RGB16 PNG or TIFF, Skin Mask is grayscale PNG, and both
AI Fill modes save RGB8 PNG with the existing `_shpcrop_` suffix. AI Fill does
not have the same ICC-embedding guarantee as skin exports.
Read the success status for the actual saved path.

For PhotoCraft, Get Image again after changing the target's size/profile or
replacing the document. Tools does not convert the target color profile.
If a send reports an uncertain final insertion/lost reply, **inspect the document
before retrying** to avoid a duplicate. A disconnected PhotoCraft source produces
an error; it does not silently become a local export.

### Flattened result or editable layers?

Choose **Send Result** when exact calculated appearance is the priority.
Choose **Send Layers** when you want editable components in PhotoCraft:
Color, optional Blur/Directional Blur/Hue, Detail, and enabled D&B bands as
appropriate to the current mode.

Editable Combine output can differ from its flattened photograph because of
nonlinear hue adjustments, clipping and rounding. The default **Mask on top
folder** also applies the shared skin mask to the whole stack, which can look
different from masks on individual ingredients. Editable does not mean identical.

Under **Settings & Info > Result file / PhotoCraft layer-transfer options**:

- **format** and **compression** select PNG/TIFF and lossless compressed/uncompressed
  output for flattened skin results and supported layer transfers.
- **exclude opaque alpha**, on for new settings, omits an all-opaque alpha channel.
  This is not a switch for removing a meaningful mask.
- **send layers mask** chooses **Mask on top folder** (default) or
  **Mask on every layer** (ingredient-specific masks; Color/Detail stay unmasked).
- **parallel encoding (two encoders)** applies to supported multi-layer transfers,
  not simultaneous batch images.

These choices persist globally, independently of presets. They do not change
AI Fill or standalone mask output. TIFF is not a proven real-world speedup and has
a classic 4 GiB-per-file limit.

### The other Save button

The source also contains a separate image-export **Save** action, distinct from
preset Save and Send Result. It exports a full-resolution bundle into the app's
output area, but sits in the same currently hidden container as the standalone
file controls. Its backend supports mode-specific ingredients/settings, not a
standalone layered PhotoCraft document format. **Do not rely on finding this
button in the current UI.** For the beginner workflow above, use Send Result;
the visible Save beside the preset selector saves only the preset.

## 6. AI Fill: Crop & Expand and Patch Fill

Use **Crop & Expand** to add surroundings and **Patch Fill** to replace painted
areas inside the picture. Neither runs as part of Combine or its batch processing.
Both require the existing ComfyUI setup; check readiness before generating.

### Crop & Expand: add room around the picture

1. Select **Crop & Expand** and **Get Image**, or load a local file as described above.
2. In **Frame**, select a ratio such as **4:3**, or **Free**. Clicking the selected
   ratio again flips portrait/landscape. Drag the frame/corners to place it.
3. Check the original/new resolution and the four side shifts. Positive values
   add space; negative values crop away space. Expansion is capped at **999
   original-image pixels per side**, possibly lower in a configured installation.
4. Add space outside the picture. **Generate** requires new area; an entirely
   inward crop alone shows "Nothing to generate" and is not this generation path.
5. In **AI SETTINGS**, leave the prepared model settings initially. An optional
   prompt describes the new area; an empty prompt asks to continue the scene.
   Select **1**, **2** or **3** variations, then **Generate**.
6. Inspect each finished result tab. Results become available as they finish.
   The selected result is the one Send Result uses; later variations do not
   automatically replace your selection.
7. Wait for generation to stop, then **Send Result**. For PhotoCraft this changes
   the canvas; for a local source it saves the selected final canvas beside it.

Generate becomes **Cancel** while running. Cancellation or later failure keeps
already completed variations. In a result view, wheel to zoom, drag to pan, and
double-click to fit. Middle-button dragging pans in source and result views
without moving the crop frame.

The **method** choices are **FLUX.2** dev (default), **FLUX.1 Fill** and **FLUX.2 Klein**, with separate
settings and model requirements. Changing the dropdown does not install weights.
Klein defaults to 4 steps, 2 MP and Lanczos; its optional Turbo LoRA is off.
Only choose weights made for the selected Klein model: dev Turbo weights do not fit.
Its generation mask grows 24 original-image pixels inward and softens at radius
8 pixels regardless of working resolution. **Feather** changes only the final
blend; zero leaves a hard join. **Seam colour correction** adjusts smooth colour
drift without blurring texture. The once-blended opaque result places exactly
as previewed, keeping original pixels outside the transition unchanged.
**Lanczos (no model)** avoids a neural upscaler, not the generation models.
**Free Comfy memory** only requests unloading while the queue is idle; it does
not guarantee a particular RAM/VRAM reduction.

### Patch Fill: replace only what you paint

1. Select the top-level **Patch Fill** tab. It uses AI Fill's loaded source.
   If necessary, **Get Image** first. Loading a new source clears the painted mask.
2. Select **Paint** in **Paint patch**, then paint the area you want replaced.
   Use **Erase** to remove part of the mask or **Clear mask** to start over.
   **brush size** is diameter in original-image pixels, from 1 to 512; the circle
   cursor shows its displayed size at the current zoom.
3. Use the wheel to zoom, **Space + left-drag** or **middle-button drag** to pan.
   Choose **context**: start at **100% Whole image**, the default, when learning.
   Check the **Processing ... of ... px** readout to see the region the model sees.
4. Open **AI SETTINGS**. Patch Fill uses **FLUX.1 Fill**; it does not switch
   Crop & Expand's saved generation method. Set an appropriate prompt and review
   its model, steps, megapixels and **feather px**. No universal retouching values
   are prescribed here; use your prepared setup and inspect each result.
5. Choose **1**, **2** or **3** variations, then **Generate**. An empty mask cannot
   generate. **Cancel** or a later failure keeps already finished variations.
6. Select a finished result to inspect it. Once generation stops, **Send Result**
   saves a numbered full-image PNG beside a local source, or sends a masked patch
   layer to the captured PhotoCraft document **without changing its canvas size**.

**Context is what the model sees, not what it replaces.** At **0% Local context**,
one rectangle encloses all painted strokes, padded on each side by 25% of the
painted width/height or at least 64 original pixels, clipped to the image.
Higher percentages expand that rectangle toward the image edges; 100% uses the
whole image. Widely separated strokes share one rectangle, even at 0%.
The megapixel setting applies to this region. Local context can give a small
patch more model detail but less scene information; it does **not** promise
faster generation at unchanged megapixels/steps.

Only the painted mask is composited back; surrounding context is not replaced.
Feathering fades inward. Dimensions stay unchanged, and pixels outside the mask
match the loaded RGB8 sRGB working source. That is not a promise to preserve
the original file's encoded high-depth samples or ICC data.

### What AI Fill shares and what it saves separately

Crop & Expand and Patch Fill share the source, ComfyUI connection, queue,
**Free Comfy memory** and CPU/Default **text encoding** resource preference.
That text-encoding choice is outside both modes' presets and resets.

Model/upscaler choices, generation settings, prompts and named presets are
independent per mode. Patch Fill uses `patch-settings.json` and
`patch-presets.json` in the existing `crop/` data directory; expansion retains
`settings.json` and `presets.json`. First use copies old saved Fill values/presets
once, without overwriting expansion's files. Later saves, preset deletes and
Factory settings resets affect only the selected mode. Patch context saves in
Patch settings/presets and defaults/resets to 100%. Variation counts are separate
browser preferences. The painted mask and Paint/Erase selection are session-only.

AI Fill and Skin Tools remain separate. After sending a resized or patched image
that you want to retouch, explicitly Get Image in Skin Tools again.

## 7. Processing a folder with Combine

First review one representative image and save a useful preset. Batch applies
the current settings; it does not decide the best settings for every photograph.

1. In the native Tools desktop, select **Combine** and open **BATCH PROCESSING**.
2. Choose **Choose source**, then **Folder** or **Images**. Selected images must
   come from one folder. Check the discovered image count.
3. Confirm ingredient settings, mixes and global output choices before
   **Start Batch**. The run takes a snapshot of them.
4. Watch progress and errors. Check the output folder and successful results
   before considering the batch complete.

Folder selection includes direct JPEG/PNG/TIFF/WebP children, not nested folders.
Symbolic-link inputs are refused. Each image gets its own analysis, skin mask,
pose and scale-based FS radii. Other settings, including Hue Center, remain fixed.
An image without a usable detected scale fails rather than borrowing the editor's
scale. One image's failure normally does not prevent later images being attempted.

Output goes into a new numbered folder such as `shpcombine_batch_001` **inside
the input folder**. Results are flattened RGB16 PNG/TIFF files, for example
`portrait_001.png`. One `preset.json` records shared settings and per-image
status/output information. Do not edit that file during the run. Same-stem
inputs, such as `portrait.jpg` and `portrait.tiff`, can collide; the later output
is reported as a failure rather than overwritten.

**Cancel** waits for a safe boundary; an in-progress calculation/save may finish
first. Completed files remain. The manifest records cancelled/failed items.
Batch does not run either AI Fill mode, contact PhotoCraft or replace your loaded editor image.

## 8. Optional PhotoCraft export inboxes

Skip this section until direct Get Image/Send works. The current implementation
has installation-specific, fixed Linux inbox paths:

| Destination | PNG export inbox |
| --- | --- |
| Skin Tools | `/home/stef/shp-photocraft-tools/watchfolder` |
| AI Fill, importing into Crop & Expand | `/home/stef/shp-photocraft-tools/watchfolder_crop` |

**These are literal current paths, not portable examples.** Installation/service
startup does not create them. Do not assume changing a project folder relocates
them; use direct Get Image if this installation does not provide suitable inboxes.

The relevant service must be running. Tools waits for a direct PNG file to stop
changing for at least two seconds and validates it; nested folders, other formats
and symlinks are not imported. The running desktop controller can show the
existing window from the tray; it does not launch a second app.

**Keep the exporting PhotoCraft document active until import finishes.** Tools
associates the PNG with the active document at processing time, without a dialog.
It cannot prove which document produced a same-size PNG.

Successful skin imports select **Skin Mask**; AI Fill inbox imports select
**Crop & Expand**, not Patch Fill.
Busy work delays imports. The inbox PNG is removed only after successful load and
association; failed files remain with status/log errors. Subsequent Send actions
target the captured PhotoCraft document, not a local file.

## 9. Troubleshooting and help

| Symptom | First checks |
| --- | --- |
| Skin tabs or AI Fill tabs are missing | A stopped service hides its tabs. Start Skin Tools or AI Fill from the tray. Skin Tools owns four tabs; AI Fill owns Crop & Expand and Patch Fill. |
| Get Image opens the wrong kind of source | It fetches the active PhotoCraft document when connected; the native local chooser is the disconnected fallback. The current skin UI has no verified visible separate local-open control. |
| File chooser or batch source selection is unavailable | Use the native Tools desktop. AI Fill has a standalone configured file list; skin-tools local file controls are currently hidden. Batch choosing requires the desktop. |
| Analysis fails on a fresh machine | Verify all three manual models, pose-weight cache/download access, GPU libraries and compatible NVIDIA driver using README/MODELS. Startup alone does not test inference. |
| Effect is missing or too weak | Compare Original/Result or Combined, check effect switches and mixes, then inspect the shared Skin Mask/light/color ranges and scale. Do not judge D&B strength from exaggerated band previews. |
| Directional selectors are gray | Hover for missing-geometry details. Unusable regions are skipped; scale-landmark edits do not repair the pose. |
| Crop & Expand Generate is disabled | Add area outside the original frame and check ComfyUI readiness, selected model files and nodes. Readiness is not a guarantee of sufficient GPU memory. |
| Patch Fill Generate is disabled or fails | Paint a nonempty mask, verify FLUX.1 Fill models/nodes and read the status. A patch too faint for its feathering needs a larger painted area or less feathering. |
| FS/Combine reports source/profile errors | These paths require opaque, finite, supported SDR samples and captured ICC data. Inspect the error and prepare a supported source/profile in the editor rather than forcing the operation. |
| PhotoCraft sending fails | Check authenticated control, token, permitted read/exchange paths and target document. Tray reachability is TCP-only, not proof of authentication. A rejected token is an error, not a local-file fallback. Get Image again after target size/profile/replacement changes; inspect before retrying uncertain insertion. |
| Local saving fails | Check the reported directory, permissions, free disk space and that the source/directory was not replaced. Do not expect a failed PhotoCraft send to save locally. |
| Editing feels less responsive | Try disabling **precache skin mask** in Settings & Info. Send still computes/reuses the correct mask. Large full-resolution work can use substantially more memory than previews. |
| Closing the window did not stop Tools | Native **X** normally hides to the tray. Use tray **Show** to return or **Quit** to stop. Save/send wanted work before shutdown. |
| Refresh/restart lost the loaded view | Browser-only state is not automatically recovered. Presets are settings, not session backups. After code updates, a page refresh alone does not reload backend Python. |

Use normal **Restart** only after dealing with work you want to keep. **Force
Restart** is an explicit last resort with all-window state-loss warnings, not a
routine fix. Stopping AI Fill alone can leave its Tools-owned ComfyUI running.
Ordinary Tools service controls do not stop/start PhotoCraft.

### Optional LightCraft handoff

This is not required for local processing or ordinary PhotoCraft Get/Send.
The updated README documents a separate opt-in authenticated PhotoCraft launcher
and LightCraft external-editor helper. Follow its installation/backup instructions
before configuring **Settings > External Editing** in LightCraft with the full
path to `photocraft-lightcraft` (program path only, without quotes or flags).

Do not use the direct PhotoCraft `--control`/automation-root launch command as
LightCraft's external-editor target: those flags opt out of normal single-instance
file forwarding. A GUI control-port preference alone does not configure the
shared token and permitted filesystem roots.

The helper acknowledges file-open requests rather than silently launching another
GUI. After an uncertain handoff, inspect PhotoCraft before retrying. It does not
fix LightCraft catalog-add/duplicate-copy errors occurring before the editor call.
These installation-specific details are source/documentation-verified, not a
claim that a live LightCraft-to-PhotoCraft transfer was tested for this manual.

### What to collect when asking for help

Read the visible status first. Skin Mask > **Image analysis > Log** holds analysis
and operation notes; **Settings & Info > Calculation timings** is separate and
temporary. Collect relevant messages before refreshing.

Report the app/repository revision if known, Linux distribution, launch mode
(native/standalone), tab and action, local versus PhotoCraft source, exact error,
and whether the problem repeats. Remove tokens, personal paths and private image
information before sharing logs.

Current reference material:

- [README](../README.md): installation, operation, data locations and commands.
- [MODELS](../MODELS.md): exact external model/node requirements and offline setup.
- [HANDOFF](../HANDOFF.md): detailed implementation and known limitations.

Ask the project maintainer through the repository's available support/issue
channel; no public support address is established by this draft. The documents
in `docs/source/` are historical, **not current setup instructions**.

## 10. Questions for the next draft

These are editorial/release decisions left open, not prerequisites for using the
verified workflows above:

- **Target release:** Which committed/package version should the finished manual
  describe? SHP Portrait Tools is the confirmed public name; the current canonical
  docs include later uncommitted features, not just `6e23398`.
- **Reader's starting point:** Will readers receive a ready-to-use Linux desktop
  installation, or be expected to install Python/models/ComfyUI themselves?
  The final quick start should prioritize that actual delivery route.
- **Local loading/export UI:** Should the hidden skin-tools file controls and
  separate export Save become visible, or should the README's standalone
  instructions change? This draft uses the verified disconnected native chooser
  and Send Result instead of assuming those hidden controls are accessible.
- **Visual recommendations:** Which reviewed portrait examples and conservative
  starting presets should illustrate acceptable results? This draft deliberately
  does not invent universal retouching values or claim visual approval.
- **Distribution and help:** Is this an internal/private guide or a public manual,
  and what support contact and model-license guidance should accompany it?
- **Export inbox portability:** Should the final guide document only the existing
  fixed-path installation, or wait for configurable inbox paths?
