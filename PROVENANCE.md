# Source provenance

This is a new repository, not a move, rename or history rewrite of either original project.

| Source | Snapshot |
|---|---|
| [stefhartog/shpcrop](https://github.com/stefhartog/shpcrop) | `7e07a3fbea337af4f590ff3e258156cce4383901` |
| [stefhartog/shpdnb](https://github.com/stefhartog/shpdnb) (local folder `/home/stef/dnb`) | `643aea67b3a8378b2e3b5abe02b823bd8f4772ef` plus the current uncommitted `python/ui/index.html` and `HANDOFF.md` edits |
| Shared CSS/resource loader | D&B snapshot's `shared/shp-ui/shp_ui/{common.css,__init__.py}` |

The D&B working-copy UI changes included its window title and standalone app-window resize
to 1347x994. The monorepo then consistently renames both active window titles.
The source HTML, processing engines, tests and relevant technical documentation were copied
from the working files, so the latest UI work was not lost by copying only committed blobs.

`docs/source/` contains frozen README/HANDOFF technical context from both snapshots.
Its old commands, paths, launcher instructions and validation statements describe the
originals only; use the root README/HANDOFF for this repository. Those archived documents
are intentionally not renamed or rewritten.

Only tracked source, the Crop tests, shared CSS/resource loader and technical/model notices
were selected. Git history, environments, personal photos, presets/settings, credentials,
runtime caches/results/logs, model binaries and locally unpacked tools were not copied.
No source root had a tracked project LICENSE file to transfer; no new license grant is
asserted here. Model/dependency licensing and model-source notices remain in MODELS.md.

## Subsequent Tools development

The consolidated service now exposes Skin Mask, FS + Skintone, D&B and Combine beside
the independent Crop module. Shared masks/settings/transport are local package resources;
Combine calls the existing FS and D&B engines rather than importing an external project.
The original repositories and frozen notes describe migration inputs, not current UI,
output topology, activation or deployment. Root README/HANDOFF are the current references.

Release checkpoints intentionally exclude unrelated local Blemish/ClearFrame experiments,
datasets, generated artifacts and credentials. Those experiments are not shipped runtime
dependencies or implemented release features. Updating documentation or publishing Git
does not update installed launchers or restart live PhotoCraft/Tools/ComfyUI processes.
