# dnb handoff

Automatic dodge & burn for portraits. Python prototype, used as an add-on for PhotoCraft. A Rust port (Windows/macOS,
PhotoCraft plug-in) is planned but deferred: iterate on the Python version first.

Last updated: 2026-11-04.

## What it does

1. Reads a picture and estimates the scale **S** = pixels per 63 mm at the subject.
2. Builds a **skin mask**, optional picked **color range**, **tone-range** weight, and optional **structure protection**
   (all feed ONE mask).
3. Computes a log-luminance band-pass correction at feature sizes given in mm, and writes it as a **Soft Light gray layer**.

Three outputs, always:

| Output | Meaning |
|---|---|
| Result | the picture with the settings applied |
| Mask | ONE mask = skin mask x color range x tone range x structure protection |
| Soft Light layer | 16-bit RGB PNG, three identical gray channels, 50% gray = no change, NO mask applied |

The layer is RGB, not Gray, on purpose: PhotoCraft converts a Gray image with a gray profile on import, which shifted 50%
gray and brightened the face by about 8 levels.

## Running it

```
cd /home/stef/dnb
uv run python python/ui_server.py --port 8765      # UI at http://127.0.0.1:8765
```

- The PhotoCraft desktop icon runs `~/.local/bin/photocraft-dnb`, which starts this server (port 8765) and the shpexpand server (port 8767) if not running, opens each
  in its own Chrome app window (`flatpak run com.google.Chrome --app=...`), then runs OpenFill's launcher
  (`~/.local/bin/openfill-photocraft`). It stops the servers when PhotoCraft closes, but only those it started.
  The desktop entry is `~/.local/share/applications/ai.storyteller.photocraft.desktop` (Exec points at the launcher). If an
  OpenFill update rewrites it, point Exec back at `photocraft-dnb`. The original is saved as `*.openfill-backup`.
- Restart gotcha: kill the server in its own shell call, with the pattern `ui_serve[r].py`. Putting `pkill -f ui_server.py`
  in the same command as the restart kills the shell. Then start it in a separate call.
- Python is run via `uv run python ...`. No node here; to check the page for JS errors use headless Chrome:
  `flatpak run com.google.Chrome --headless=new --no-sandbox --enable-logging=stderr --virtual-time-budget=5000 --dump-dom URL`.
  The browser tool in the OpenCode app cannot reach this machine's localhost.

## Files

- `python/ui_server.py`: stdlib HTTP server and API (`/api/load`, `run`, `save`, `profile`, `tone`, `presets/*`, `pc/*`).
- `python/ui/index.html`: the whole UI (single file).
- `python/dodge_burn.py`: engine (`compute_change_band`, `process`, `tone_mask`, `layer_from_change`, colour modes).
- `python/skin_mask.py`, `skin_mask_compare.py`: skin mask, `pick_target`, `person_box`.
- `python/combine_scale.py`, `estimate_scale.py`, `estimate_body_scale.py`, `exif_af.py`, `dnb_gpu.py`: scale estimation, Sony AF point, CUDA preload.
- `python/photocraft_bridge.py`: PhotoCraft control-channel client (fetch / send / launch).
- `presets.json`: saved presets (gitignored). `results/<name>_<time>/`: saved outputs (gitignored). `inbox/`: pictures fetched from PhotoCraft (gitignored).
- `README.md`, `MODELS.md`: documentation and model licences. `crates/`: Rust skeleton, not developed.
- `testpictures/`, `out*/`, `tools/` (local ExifTool): gitignored.

## Scale (S) estimation

Cues: `face_ipd`, `iris`, `eye_corners`, `yunet_ipd`, `body` (RTMW). Face cues' sigma grows with head yaw. The AF point
(Sony FocusLocation via the local ExifTool) picks the target subject. A PNG fetched from PhotoCraft has no AF point, so
the target is the most trustworthy subject. Body-only estimates are low confidence. The user can override S in the
Image analysis section.

## Engine

- Log-luminance band-pass with guided filters. One band: LO/HI in mm (UI shows midpoint + range in octaves + taper).
  Other parameters: strength (0 to 2), edge (0 to 0.2), tau and max_ev (hidden, fixed from the preset), gain (fixed 1).
  With one band, gain and strength are the same multiplier, so only strength is exposed.
- Tone range (midpoint / range / taper) is part of the mask, not baked into the layer. `D.process` returns
  `(cs, after, change, diag, total_mask)`.
- Color range (`D.color_range_mask`) is a soft HSV window around a color: filled with the average skin-mask color on load
  (and switched on), or picked by clicking the small swatch next to the "Color range" title and then the Original (`/api/color-pick`). Sliders: strength, hue range, saturation range,
  feather mm (blur of the mask) and smoothing mm (blur of the image before the test, to avoid speckle). It multiplies into
  the same mask and is saved in presets. Light range and structure protection also have a strength slider (0..1); global
  strength goes to 4. Light range has the same swatch: its midpoint is the average skin-mask brightness on load
  (`skin_colour.light`) or picked from the Original (`light` in `/api/color-pick`, ~1 mm patch).
- Skin mask = MediaPipe selfie-multiclass ONNX segmenter, minus eyes/brows/lips (face mesh) and nostrils (dark-blob
  detection), times a conservative colour check, then feathered inward only. Features, colour check and inward feather are
  always on (their checkboxes were removed).
- Mask-aware averaging uses a multi-scale fill. The unmasked layer is cross-faded to the plain whole-picture analysis within
  6 mm outside the mask.
- Colour: ICC read at load. sRGB/untagged kept; Adobe RGB kept (gamma 2.2 + own luminance weights) and the profile embedded in
  previews and results; other profiles converted to sRGB.
- Previews are computed on a downscaled copy (max ~1600 px). Preview crop: face (2.5 x S), portrait (6 x S) or whole image; Shift-click zooms between them.
  Save and Send always run at full resolution.

## UI as it is now

- Slider value boxes are read-only small readouts (no spinner arrows); collapse arrows are on the right; thin dividers only between major groups.

- Top bar across the whole width: Get from PhotoCraft, Send to PhotoCraft, centered status text (spinner while working, green check when done; no filename); the preset dropdown and
  Save / Save New / Delete on the right.
- Left pane, in order: Detail level (midpoint/range/taper + strength + edge + gain plot), Skin mask, Structure
  protection, Light range (the old "tone range"), Color range, Image analysis (info + px/63 mm override).
  Each is independently collapsible, with state remembered. Each mask part's "on" checkbox stays in its title row
  when collapsed, as do the light/color picker buttons. These controls do not toggle collapse, and collapse does not
  change processing settings. Existing Mask / Light & color range collapse preferences seed the split sections.
- Ctrl+click on a preview cycles the tabs of that pane. Tab panes: left Original/Result; right Analysis/Mask/Layer/Layer+mask.
- 2x2 previews: Original, Result / Mask, Soft-light layer (stretched x8). Wheel zoom and drag pan, shared across panels,
  double-click resets.
- Always applies in real time (no Apply button, no auto-apply checkbox). Loading or getting a picture applies the current settings.
- Presets: list (Factory settings / Default / named), Save, Save New (popup for a name), Delete (confirm). A `*` after the
  selected name means the current settings differ from it. Presets ignore the bottom-panel choices, and force band gain to 1.
- Tooltips (title attributes) on every control instead of helper text.
- Manual file loading, Apply and Save still exist, hidden in `<div id="manual" hidden>` (plus hidden selects for crop and
  panels). Do not delete them; the code uses them.

## PhotoCraft handoff

- Get: `image.duplicate` (mergedOnly) then `app.save` as PNG into the exchange folder, then the normal load.
- Send: layer (RGBA16 PNG so it opens as a normal layer) and mask are opened as scratch documents. The mask is loaded via
  `select.loadSelection channel="composite" document=<maskdoc>` + `layer.layerMask.revealSelection`. Blend is set to "Soft Light",
  and one `layer.copyToDocument` puts it in the target document, so one Undo removes it. Scratch documents are closed.
- Connection defaults: port 7878, token `~/.config/openfill-photocraft/control.token`, exchange `~/.config/openfill-photocraft/exchange`
  (write root); read root = home. Override with `DNB_PC_PORT`, `DNB_PC_TOKEN_FILE`, `DNB_PC_EXCHANGE`.
- Tested live with desktop PhotoCraft 0.20.1 (layer, Soft Light, mask and one history step confirmed) and headless with
  `photocraft-cli serve`. `/api/pc/status` and `/api/pc/launch` still exist; their buttons were removed from the UI.
- Measured: PhotoCraft's Soft Light = W3C formula (within 0.5/255). Composite vs our result: mean 0.48, p99 2, max 7.7 (of 255).
  The mask arrives exactly. Photoshop's Soft Light has not been tested.

## Known issues and ideas

- Tattoo colour-exclusion is weak. Nostril detection is imperfect on turned faces.
- The mask is not used by the CLI `dodge_burn.py` (only the UI/server).
- Layer PNG is about 57 MB at 14 MP; an 8-bit option could be offered.
- Ideas not done: a Filter > Plug-ins menu trigger inside PhotoCraft, an AF point for PNGs from PhotoCraft, a Rust port,
  a trained model for blemish vs real-feature judgement (no ComfyUI model for dodge & burn was found in a quick search).
- Previous assistant mistakes to avoid: reading a file while a command is still writing it, killing the server in the same
  shell command that restarts it, and cat'ing a binary file.
