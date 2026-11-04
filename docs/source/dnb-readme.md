# dnb: scale analysis for automatic dodge & burn

Goal: an automatic dodge & burn tool (Linux standalone first, later Windows/macOS and PhotoCraft). The first
building block is **scale analysis**: how many pixels a subject occupies, so brush radii and filter sizes can be
expressed relative to the subject instead of in raw pixels.

Status: Python prototype. Estimates are rough and unvalidated (see Limits).

## What S means

**S is the number of pixels in the original image that correspond to 63 mm at the subject's distance from the camera.**

63 mm is roughly the average adult interpupillary distance (IPD, pupil to pupil). S answers:
"if this person had an average adult's eye spacing, how many pixels wide would it be?"

- mm per pixel = 63 / S
- 1 mm = S / 63 pixels
- Example: S = 297 px means about 4.7 px per mm, or 0.21 mm per pixel.
- S is measured in pixels of the file that was analysed (full resolution), and is computed **per person**, not per image.

### What S is not

- Not the person's real IPD in pixels. Real IPD varies roughly 54-74 mm, so converting S to millimetres can be
  off by about +-10% for an individual.
- Not a physical measurement. No S has been checked against a known real-world distance.
- Assumes an adult. For a child, S is still an adult-equivalent value.
- Describes the scale at the subject's depth. In wide shots a near limb looks larger than the face, so one S per
  person is an approximation. A subject and their mirror reflection get different S values (different distances).

### How S will be used

Express sizes as fractions of S, e.g. "fine pass at 0.05 x S, volume shaping at 0.5 x S", so the same setting scales
on a close-up and a full-length shot. A low-confidence S should be overridable by hand.

## How S is estimated (cues)

Every cue is converted to "what would the IPD be in pixels" so the cues can be compared and averaged.

| Cue | Source | Conversion to S | Assumed error (sigma) |
|---|---|---|---|
| `face_ipd` | iris centres from MediaPipe Face Landmarker (478 pts) | measured directly | 8%, 15% if it disagrees with YuNet's eye distance; grows with head turn |
| `iris` | iris diameter, larger of width/height (a turned head squeezes width only) | px x 63 / 11.7 | 12% (only if iris >= 20 px) |
| `eye_corners` | outer eye corners, RTMW whole-body face points | px x 63 / 88 | 15%, grows with head turn; only for near-frontal faces |
| `yunet_ipd` | YuNet eye distance, only when the landmark model rejects the face | measured directly | 15% |
| `body` | RTMW body keypoints: upper arm, forearm, thigh, shank, shoulders, torso | implied height px x 63 / 1680 | 20% |

Body segments use average proportions (Drillis & Contini style fractions of height). Because foreshortening only
shortens a segment, the body cue uses the median of the longer half of the segments.

### Combining

1. With 3+ cues, any cue more than 25% from the median is dropped.
2. The rest are averaged in log space, weighted by 1/sigma^2.
3. Head turn ("yaw", estimated from where the nose sits between the face edges) only **inflates sigma**:
   `sigma = base + 0.25 * (1 - cos(yaw))`, yaw capped at 75 degrees. It does not "un-turn" the measurement; that
   overshoots because the yaw estimate is rough.
4. Confidence: `high` (>= 2 trusted cues within 12%), `medium` (within 25%), `low`, or `none`. Cues with sigma
   above 20% are averaged but do not decide confidence.

### Which person to measure

If the file carries a Sony autofocus point (`Sony:FocusLocation`, read with ExifTool), the person nearest that
point is marked `<- AF target`. This handles several people and mirrors: in a mirror shot the sharp, focused
person is the one that matters.

## Constants (all population averages, all assumptions)

IPD 63 mm; visible iris diameter 11.7 mm; outer eye-corner width 88 mm; body height 1680 mm. The hand-length cue
(185 mm) exists in code but is **off**: on the test photos it came out 2-3x too short (curled or foreshortened
hands).

## What did not work

- **Skin texture scale** (`python/texture_experiment.py`): spectra of skin patches do not line up in physical
  units. They reflect sharpening, noise and the camera, not the skin; the images (0.2-0.4 mm/px) barely resolve
  pores.
- **Focus distance from EXIF**: would give true mm/px, but this camera/lens (Sony A7 IV + Tamron 28-75) writes
  `FocusDistance2 = inf`. The RAW would not help (same maker notes). Exported/edited JPEGs have no EXIF at all.
- **Hand length** cue, see above.
- **Depth-based yaw** from the landmark model's z values read ~0 degrees everywhere; replaced by the nose-position proxy.

## Limits

- Sigmas, the 25% outlier rule and the yaw penalty are my choices, tuned by eye on 5 face photos of one subject.
  There is no ground-truth test set yet.
- Body-only results (no face) are always `low` confidence. Very tight crops can give wrong values: SHP06304 returns
  S = 304 px, which is probably far too small for such a close crop. Keypoints for body parts outside the frame
  are guesses by the model.
- Perspective: near limbs look bigger than far ones, which biases the body cue on close, wide shots.
- Two test files (`SHP04049-Edit_small`, `SHP04683_Smoll`) are exported JPEGs without EXIF, so no AF point.

## Dodge & burn prototype (whole picture, no masks yet)

`python/dodge_burn.py` builds a gray layer for the **Soft Light** blend mode. 50% gray = no change, lighter =
dodge, darker = burn. It works on luminance only (colours are untouched) and is applied to the entire picture;
the command-line tool has no skin mask (the web UI does, see above), so hair, background and clothing are
processed too there.

**Band mode (default, `--mode band --bands LO:HI:GAIN[:TAPER] ...`):** you give one or more bands. Feature sizes
(width of the structure, in mm) inside a band are corrected with that band's own gain; sizes in no band are
left alone. Example: `--bands 1.5:40:1.0 40:80:0.3` ignores everything below 1.5 mm, corrects 1.5-40 mm fully,
corrects 40-80 mm at 30%, and ignores everything above 80 mm. Default: `1.5:50:1.0:0.5`.
- **Gain** multiplies `--strength` for that band (0 = nothing, 1 = full, above 1 = push harder).
- **Taper** (octaves, default 0.5) is the edge softness. A free edge (not shared with another band) fades inside
  the band: 0 at the edge, full at edge x 2^taper (at the top: down from HI / 2^taper to 0 at HI). Where one band's
  HI equals the next band's LO, the two cross-fade smoothly, centred on that size, using the lower band's taper.
- Bands with a gap between them leave the gap alone.
The image is split into half-octave detail levels with an edge-aware filter, each level gets the summed gain of
the bands, and the sum of gained levels is the correction. The script prints each level's size, energy and
gain. `--fine-mm` is ignored in band mode (the smallest LO sets the working resolution). `--mode single` keeps the
older "flatten against one local mean" method described below.

How the single mode works (sizes are in mm, converted to pixels with S):
1. Luminance in linear light, then log, on a downscaled copy (the correction is smooth, so this is fast).
2. Reference = edge-aware (guided filter) local mean at `--scale-mm` (default 12; 40 gave more useful results on faces).
3. Deviation = fine-smoothed luminance - reference. Detail below `--fine-mm` (1.5 mm) is left alone.
4. Change = -strength x deviation, faded out for large deviations (`--tau`, real features like eyes) and capped at
   `--max-ev` stops. Upsampled with a guided filter on the full-res image so it does not leak across edges.
5. **Tone weighting:** the change is multiplied by a weight that is 1 in the midtones and fades to 0 in the
   darkest and lightest parts. Three settings (brightness is sRGB-encoded, 0 black to 1 white): `--tone-mid`
   (centre of the corrected range, default 0.52), `--tone-range` (its total width; nothing outside changes, default
   0.87) and `--tone-taper` (how softly both edges fade, measured inside the range, default 0.16). The weight is
   1 between mid - range/2 + taper and mid + range/2 - taper, 0 outside mid +- range/2. `--no-tone` turns it off.
   This keeps lashes, brows, dark hair and blown highlights out of the correction and reduces halos at strong
   settings. `*_db_weight.jpg` shows the weight (white = full effect).
6. The Soft Light formula (W3C version) is inverted to get the gray value that produces that change. Photoshop's
   and PhotoCraft's soft light differ slightly, so results in those apps may differ a little.

```
uv run python python/dodge_burn.py testpictures/SHP04378.JPG --auto --scale-mm 40 --strength 0.8 --out out7/
uv run python python/dodge_burn.py IMAGE --S 297 ...        # give S by hand instead of --auto
```

Presets tried on SHP04378 (largest change in the face crop, 0-255 scale):
- default (40 mm, strength 0.8): about 3 levels, very subtle.
- strong tone-weighted (`--scale-mm 120 --strength 1.0 --tau 1.2 --max-ev 1.0 --edge 0.3`): about 7-10 levels.
- wide midrange, very strong (`--scale-mm 200 --strength 1.0 --tau 2.0 --max-ev 1.5 --edge 0.4 --shadows 0.08 0.25
  --highlights 0.80 0.95`): about 17 levels, p95 12. Visible, but it lifts the midtones broadly, flattens facial
  volume somewhat (reference is larger than the face) and brings edge halos back. Strength 1.4 reaches 24 levels.

Band-mode example (SHP04378, from before LO/HI had inside-edge semantics, so numbers differ slightly): `--mode band --band-mm 3 30 --strength 1.0 --tau 1.0 --max-ev 1.0 --edge 0.12
--shadows 0.08 0.25 --highlights 0.80 0.95` gives about 11 levels p95 / 26 max in the face crop, with the correction
concentrated beside the nose, under the eyes, around the mouth and chin instead of a broad lift. Without masks it
also corrects mid-size structures that are not skin (tattoo, hair strands, cushion edges).

Outputs: `*_db_layer16.png` (16-bit gray, use this), `*_db_layer8.png`, `*_db_preview.jpg` (before | after | change
map: red = brighten, blue = darken) and `*_db_crop.jpg` (full-res before/after around the subject).

Observed so far (9 test photos): the default settings change luminance by only about +-3-4% (95th percentile
0.04 stops) and leave fine detail unchanged (-0.1%). The "deviation removed" figure the script prints is mostly
a function of `--strength`, so it says little about quality; judge by eye. Not yet tuned or compared against
real retouching.

## Skin mask (wired into the dodge & burn and the UI)

`python/skin_mask.py` builds a soft skin mask in three steps:
1. **Segmenter:** MediaPipe selfie multiclass (ONNX, 16.5 MB, 256x256, Apache-2.0 per Google's model card; see
   `MODELS.md`), skin = body-skin + face-skin, run on a crop around the person. The crop grows when the mask touches
   its edge, and the mask is snapped to image edges with a guided filter. It covers face and body, and leaves out
   hair and clothing, but it also paints over eyes, brows and lips.
2. **Face features:** eyes, brows and lips from the face mesh (needs a detected face), grown by 2 mm and feathered
   1.2 mm, are subtracted, and so are the nostrils: the mesh only gives the lower-nose region, and inside it the (at
   most two) darkest blobs that are clearly darker than the surrounding skin are grown to about halfway to the skin
   brightness (`python/nose_check.py` writes `out17/nose_check.jpg` to check this; on the test photos it found both
   nostrils on 4 of 5 frontal faces, one nostril on the fifth, and one extra false blob on a turned face). Works well on frontal faces, partly on turned ones, not at all in profile or without a face.
3. **Colour check:** a robust Lab colour model is fitted on the confident skin pixels; pixels darker than average
   skin and far from it in chroma (default 3.5-6.0 sigma) are removed. This hardly helps with tattoos (the segmenter
   already leaves most dense tattoos out) and at lower thresholds it removes shaded, warm-lit skin, so it is
   conservative by default.

`uv run python python/skin_mask_demo.py` writes `out16/skin_final.jpg` (green = kept, red = features, yellow =
colour) and `skin_final_zoom.jpg`; `python/skin_mask_compare.py` is the earlier segmenter-vs-colour comparison
(`out15/`). Timing: about 1-2 s per 14 MP photo.

**How the mask is used** (`mask=` in `dodge_burn.compute_change_band` / `process`): (1) before the band split, pixels
outside the mask are replaced by a smooth local skin average (normalised convolution, about 8 mm), so hair, lashes,
clothes and background neither pull the local means nor create edges at the skin boundary; (2) the correction is
multiplied by the mask (at the working resolution and again at full resolution, so the boundary stays sharp).
The UI computes the mask once at Load and has checkboxes: only correct skin, leave eyes/brows/lips/nostrils alone,
skip tattoos/off-colour pixels, an edge feather slider (0-10 mm, default 1 mm; Gaussian) with a
"feather inward only" checkbox (keeps the outer boundary and fades the mask inside it, so the correction does not
spill onto hair or background; it also slightly enlarges the eye/lip/nostril holes), show the mask on the original (third panel can show the mask too). Save also writes
`*_skinmask.png`. Without a detected subject there is no mask and the whole picture is corrected. No brush editing
yet. On the command line `dodge_burn.py` does not use the mask yet.

## UI (local web page)

```
uv run python python/ui_server.py            # then open http://127.0.0.1:8765
uv run python python/ui_server.py --dir ~/Pictures other/folder    # list photos from other folders
```

The server listens on 127.0.0.1 only. The settings sidebar has independently collapsible groups (open/closed state is remembered):
Detail level (with the gain-by-size plot), Skin mask, Structure protection, Light range, Color range and Image analysis.
The four mask sections keep their enable checkboxes in the title row even when collapsed; Light range and Color range also
keep their picker buttons there. Using these controls does not expand/collapse the section; collapsing does not change processing.
**Presets** (the dropdown at the right of the top bar) store the bands, global options, mask and tone settings and the view choices (not the
photo or S) in `presets.json` next to the code: pick one from the list to load it (the first entry restores the built-in
settings), type a name and press *Save as new preset* (an existing name is overwritten), *Save as default* makes the
current settings the ones loaded when the page opens, and *Remove preset* (click twice) deletes the selected one. In the page:
1. Pick a photo (or type a path) and press **Load**: it estimates S (the number can be overridden).
2. Set the **bands** (it starts with one band, 1.5-50 mm; add more with + Add band; from/to in mm, gain, taper; a band can
   start where the previous one ends, with a cross-fade), the **global** settings and the **tone range** (midpoint,
   range, taper, with a small plot of the weight by brightness). The plot shows the total gain per feature size.
3. Press **Apply**: four panels are shown side by side with synchronised zoom (mouse wheel): Original | Result | the
   soft-light layer without the mask | the soft-light layer with the mask applied (the 3rd and 4th panel each have a
   dropdown: layer stretched x8 or raw, with or without the mask, change x8, or the mask),
   and pan (drag; double-click resets). The third panel can show the layer stretched x8 (otherwise it looks flat gray),
   the raw layer, or the change x8. "auto-apply" re-renders after each change.
4. **Save** writes `results/<name>_<time>/` with the three files for a layer-based workflow (Photoshop, PhotoCraft):
   - `<name>_1_result.jpg`: the picture with the settings applied, for reference;
   - `<name>_2_skinmask.png`: ONE mask = skin mask x picked color range x tone range x structure protection (8-bit gray,
     with the chosen feather; all white if none are used). These ranges are part of the mask in the UI/Save pipeline,
     not baked into the layer;
   - `<name>_3_softlight_layer.png`: 16-bit layer for the Soft Light blend mode (an RGB PNG with three identical gray
     channels, about 57 MB for 14 MP; a Gray image would be converted with a gray profile on import, which shifted
     50% gray and brightened the picture by about 8 levels in a PhotoCraft test), 50% gray = no change, with NO
     mask of any kind applied (no skin mask, no tone window). Inside the skin and up to 6 mm outside it, the layer
     comes from the mask-aware analysis (hair/background do not disturb it); farther away from the skin it comes from
     the plain whole-picture analysis, and the two are cross-faded smoothly, so the layer is valid everywhere and has
     no edge at the skin boundary. Put it above the original, set it to Soft Light, use file 2 as its layer mask; the
     result then matches file 1 (SHP04378: mean difference 0.5 of 255, 99th percentile 2, largest 7).
   - `settings.json`: every setting used. PhotoCraft's Soft Light blend matches the W3C formula used here (measured on gradients: within 0.5 of 255 for 8-bit
     and 16-bit documents); Photoshop's may differ slightly, which I have not tested.

**Colour.** The picture's embedded ICC profile is read at Load. sRGB or no profile: used as is. Adobe RGB (1998):
kept as is, analysed with its own gamma (2.2) and luminance weights, and the same profile is embedded in the preview
JPEGs and in `*_1_result.jpg`, so the browser, Photoshop and PhotoCraft show the same colours as the original (without
the tag, Adobe RGB values were shown as sRGB, which looks desaturated; this was the mismatch seen with files exported
from PhotoCraft). Any other profile: converted to sRGB and tagged sRGB. The layer and mask are gray and carry no
profile. The camera JPEGs in the test set are untagged sRGB.

Previews are computed on a downscaled copy (at most about 1600 px) of the subject crop (6 x S) or the whole image
(about 1400 px), so Apply takes well under a second; Save recomputes at full resolution on the whole image, so the
numbers can differ a little from the preview. The page (`python/ui/index.html`) needs no frontend build step.

### Shared SHP styling

The dependency-free `shp-ui` package lives in `shared/shp-ui`. Its
`shp_ui/common.css` is the single source for the common colors, typography, sidebar,
buttons, sliders, 74px row labels, value fields, tabs, status indicators and group
headers used by d&b and SHP crop. App-specific layouts and overrides stay in each
app's HTML, after the shared stylesheet link. Both local HTTP servers serve the
packaged CSS at `/static/shp-ui.css`; no CDN or separate asset server is required.
The shared `.pane-header` class owns pane-header alignment, wrapping, spacing and
the 2px top inset: d&b applies it to its `.ptitle` wrappers, while crop applies it
directly to its `.tabs` container. Tab-button styling remains separate.

`uv sync` installs this repo's shared package as editable. For live cross-app
development, install **the same package directory** into crop's environment too:

```sh
# Supply the location of the d&b checkout you want both tools to use.
uv pip install --python /path/to/crop/.venv/bin/python -e /path/to/dnb/shared/shp-ui
```

Edits to `common.css` then reach both apps on refresh, without copying files.
If you run `uv run` in crop after overriding its pinned dependency, use
`uv run --no-sync shpcrop` so it does not replace the editable install.
Normal crop installations instead use a pinned Git dependency on this existing
repository's `shared/shp-ui` subdirectory. Those installations are self-contained
and work offline at runtime, but need their dependency pin updated and synced
to pick up later shared style changes.

## Handoff to PhotoCraft (built; tested headless only)

The web UI has a PhotoCraft group: Get from PhotoCraft (active document, merged), Send to PhotoCraft (adds the
"dnb soft light" layer with its mask as one step), Launch PhotoCraft, Check connection. Code: `python/photocraft_bridge.py`.
Defaults match the OpenFill launcher (port 7878, `~/.config/openfill-photocraft/`); override with `DNB_PC_PORT`,
`DNB_PC_TOKEN_FILE`, `DNB_PC_EXCHANGE`. Tested end to end against `photocraft-cli serve`; not yet against the desktop app.


With `photocraft-cli serve` (the headless engine; same commands as the desktop control channel) the OpenFill-style
round trip works: open the original, open our layer and mask as documents, `layer.copyToDocument` the layer into the
original, `layer.setProps {blend: "Soft Light"}`, `select.loadSelection {channel: "composite", document: <mask doc>}`
and `layer.layerMask.revealSelection`. Measured on SHP04378: the mask arrives exactly (0 levels of difference from the
gradient, gray or RGB mask), and the composite matches our own result within 0.5 of 255 (mean), 2 (99th percentile),
7.7 (largest). `doc.save` to `.psd` gives a layered file. Not done yet: attaching to the running desktop app's control
channel (`--control`, token file, read/write roots), fetching the merged picture (`image.duplicate` + `app.save`),
a menu/trigger, and the send-back as one undo step.

## Layout

```
python/
  dnb_gpu.py              preloads CUDA 13 / cuDNN so onnxruntime uses the GPU
  estimate_scale.py       face: YuNet detect + MediaPipe landmarks, iris/IPD measures, yaw proxy, overlay drawing
  estimate_body_scale.py  body: RTMW whole-body pose, segment -> height -> S, extra cues, de-duplication
  combine_scale.py        combines cues, AF-point target selection, overlays (S estimation entry point)
  dodge_burn.py           dodge & burn gray layer: pipeline, multi-band, command line
  compare_variants.py     renders several band settings side by side in one image
  ui_server.py + ui/      local web UI (see above)
  skin_mask.py            skin mask: segmenter minus face features minus off-colour pixels
  skin_mask_demo.py       visual check of the skin mask on all test photos
  nose_check.py           visual check of the nostril detection
  skin_mask_compare.py    segmenter vs face-mesh+colour comparison
  exif_af.py              reads Sony AF point via local ExifTool
  texture_experiment.py   negative-result experiment (skin texture)
models/                   ONNX files (git-ignored); see MODELS.md for sources and licenses
tools/                    local ExifTool (git-ignored)
testpictures/             test images (personal photos, not for redistribution)
out*/                     generated overlays and plots
```

## Usage

```
uv sync
uv run python python/combine_scale.py testpictures/* --draw --out out/
```

Overlay colours: red box = YuNet face; yellow rings = YuNet's 5 points; green circles = irises; magenta dots/line =
iris centres and IPD; green skeleton = body keypoints (red outlines = low confidence, ignored); orange cross = AF point.

`--draw` writes `<name>_combined.jpg`. `estimate_scale.py` (face only) and `estimate_body_scale.py` (body only)
can also be run on their own.

## Setup notes

- Python 3.13 via `uv`. `pyproject.toml` overrides `onnxruntime` (rtmlib depends on the CPU build, which would
  replace `onnxruntime-gpu`). CUDA libraries come from the `nvidia-*` pip packages and are loaded by `dnb_gpu.py`.
- Face model files go in `models/` (`yunet.onnx`, `face_landmarks.onnx`); the RTMW models are downloaded by
  `rtmlib` to `~/.cache/rtmlib`.
- ExifTool (Perl) is unpacked in `tools/Image-ExifTool-13.60/`; needs `perl`.

## Licenses

See `MODELS.md`. The model weights are Apache-2.0 or MIT, but some training datasets (HumanArt, COCO-WholeBody)
carry their own terms. Review before shipping commercially.
