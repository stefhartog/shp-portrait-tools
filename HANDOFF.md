# SHP PhotoCraft Tools handoff

## Scope and boundaries

This private monorepo was created as a new, side-by-side project. No history rewrite,
automatic cutover, desktop entry modification, old-server restart or PhotoCraft/ComfyUI
configuration change is part of the migration. Existing launchers remain on the old projects.
Do all new work here, not in the source folders listed in PROVENANCE.md.

The public names are **SHP PhotoCraft Crop** and **SHP PhotoCraft D&B**. Each has an independent
stdlib `ThreadingHTTPServer`, its original endpoint shapes and default port (8768/8765),
and its own browser UI. The root distribution installs both plus `shpcrop`/`shpdnb` aliases.
There is deliberately no module registry, plugin interface or combined server.

## Shared code

- `shared/ui/__init__.py`: `stylesheet() -> bytes` uses `importlib.resources`.
  `common.css` is the only shared stylesheet, copied from the original D&B `shp-ui` package.
  Both servers serve the same resource at `/static/shp-ui.css`. The old Git dependency and
  nested package manifest are gone.
- `shared/photocraft/bridge.py`: the original clients' common JSON-lines `Client`, auth,
  reconnect, errors, config detection, session/headless negotiation, document lookup,
  scratch open/save/close helpers, merged-document `fetch()` and explicit `launch()`.
  Module env overrides win over `SHP_PC_*`. Existing OpenFill credentials remain readable;
  fallback config/log paths are new and module-separated.

The original clients were compared before extraction: transport/session/fetch/launch were
identical except env prefixes, own config paths and labels. These differences are represented
by the two small subclasses' attributes, not duplicated clients.

Do not move result application into the common bridge:

- `modules/crop/photocraft_bridge.py`: signed canvas crop/resize, RGBA generated area, layer
  copy and best-effort undo snapshot grouping. Crop is the only module that calls `image.crop`.
- `modules/dnb/photocraft_bridge.py`: RGBA16 scratch layer plus separate mask document,
  composite selection/revealSelection retry, Soft Light mode, one layer copy. It never
  changes canvas size or groups undo by deleting history states.

Original best-effort cleanup, reconnect/retry and undo-grouping behavior is retained;
this migration is not a redesign of PhotoCraft transactions.

## Crop

`modules/crop/expand.py` is unchanged. This includes signed margins, Free height, model
validation, WebSocket progress/polling fallback, ComfyUI graph, seam correction, variations
and cancellation. No real ComfyUI graph was queued during migration validation.

`server.py` retains its endpoint routes, lock-free job/cancel polling, JSON settings/presets,
explicit Start ComfyUI and `--comfy`/`--dir`. HTML is the live source snapshot with only the
window title renamed; this includes Free mode, Generate/Cancel, result zoom/pan, pane headers
and compact shared controls. The data default moves to the new module data directory.

## D&B

Python scripts are now proper package modules with relative imports; run retained analysis
CLIs using `python -m shp_photocraft_tools.modules.dnb.<module>`. `research` installs the
optional matplotlib/notebook dependencies for experiments.

The engine math, color management, mask composition, scale cues, output precision,
picker, preview crop handling and full-resolution processing are retained. The original
HANDOFF's latest uncommitted changes are archived in `docs/source/dnb-handoff.md`.
The copied HTML includes the four independently collapsible mask-related sections and
the uncommitted standalone-window resize behavior (1347x994); only its title changes.

Writable files no longer live beside installed code: `paths.py` owns `DNB_DATA` and
`DNB_MODELS`; the server's `ROOT` refers to its data directory. New preset directories are
created on first write. The original `{presets,default}` schema is unchanged.

The import-time segmenter in `skin_mask_compare.py` is now lazy so an installed UI and
`--help` work without shipping model binaries or allocating an inference session.
Face/skin model loaders resolve external files through `model_path()` and report how to
configure missing files. Inference and missing-file failures are not disguised as successful
analysis. rtmlib's pose loader remains lazy as in the source.

Sony AF lookup now uses ExifTool on PATH or `DNB_EXIFTOOL` rather than an untracked unpacked
tool relative to the old checkout. No third-party tool tree was copied.

## Data and safe validation

Production defaults, env precedence and deliberate later JSON reuse are documented in README.
This migration copies no private presets, settings, images, model weights, credentials,
venvs, results or caches. Browser localStorage keys remain compatible but ports scope them.
Default ports are unchanged; use alternate or ephemeral ports beside the original apps.

`tests/run.py` sets temporary data/model/token/exchange paths before importing either server,
then runs the migrated Crop checks and unittest tests. `tests/preview.py` supplies synthetic
images, fake Get/Send, stub ComfyUI and fake D&B detection/masks on ephemeral ports for browser
checks; it never contacts the real services. Its real D&B correction is CPU-only.

Validation performed for the migration:

- Eight migrated Crop checks and seventeen new unittest checks pass with isolated paths.
- Wheel and sdist contain both UIs and shared CSS, without model/token files.
- The test suite also passes using a clean wheel installation outside the checkout.
- Both named commands and both compatible aliases serve HTML and identical packaged CSS
  from the installed wheel on ephemeral ports.
- Browser interaction checks use the synthetic fixture, not the live apps. No GPU inference,
  real PhotoCraft Get/Send or ComfyUI launch is used. Both layouts were checked at 1347x994
  and 760px width without horizontal overflow. Crop Free mode, Generate/Cancel, result zoom/reset,
  D&B synchronized zoom/reset, persisted section collapse, summary checkboxes/pickers and a
  browser-created preset were exercised.
- Original tracked-file hashes and the existing dirty D&B diff are checked unchanged.

Real GPU/PhotoCraft integration was intentionally not repeated. The original technical
documents record historical live checks; those are not claims of new migration validation.
Known algorithmic limitations (scale heuristics, tattoos/nostrils, nonidentical Photoshop Soft
Light) remain; see the archived notes rather than treating this packaging work as new tuning.
At very small widths (observed at 344px), the inherited D&B preset toolbar can overflow.
The original HTML/CSS was preserved rather than introducing a responsive-layout redesign.
