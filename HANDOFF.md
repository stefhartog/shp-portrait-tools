# SHP PhotoCraft Tools handoff

This is the consolidated current-state handoff for the migration, launcher/supervisor,
native desktop, managed ComfyUI and subsequent UI refinements. It replaces the incremental
conversation chronology. See [README.md](README.md) for setup/commands, [PROVENANCE.md](PROVENANCE.md)
for exact sources and `docs/source/` for frozen original algorithm/graph notes.

## Scope and accumulated decisions

The new private repository is `/home/stef/shp-photocraft-tools`,
`stefhartog/shp-photocraft-tools`, on `main`. Migration was published as
`3adb22f98ee88550f21de55b23d9b4157b66fd17`; later work adds the Tools desktop experience.
The user requested consolidation, documentation updates, commit and push on 2026-11-05.

Original `/home/stef/shpcrop` and `/home/stef/dnb` source files/repos remain untouched.
No personal presets, settings, photos, credentials, environments, models or generated files
were copied. User-approved legacy Crop/D&B shortcut cleanup happened separately; PhotoCraft/
OpenFill shortcuts were retained, including the misleadingly named PhotoCraft launcher.
The distinct Tools installer does not remove or overwrite those launchers.

The launcher evolved from independent browser tabs to a thin combined shell, then to an
owned GTK/WebKit window and tray. Ungoogled Chromium remains an explicit fallback, not the
default. The web Services/details panel was removed in favor of native tray controls.
PhotoCraft remains external/informational; the user explicitly requested a dedicated
Tools-owned ComfyUI process, superseding the earlier reachability-only design.

Real service/window activations and the later termination of all then-current ComfyUI
instances were separately user-approved. Historical process IDs/status are not current-state
claims. Publication does not authorize reopening/reloading live views, restarting services,
running inference, changing PhotoCraft documents or reinstalling desktop assets.

## Architecture and preserved behavior

Each module retains an independent `ThreadingHTTPServer`, processing engine, UI and settings.
Standalone defaults remain Crop **8768**, D&B **8765**, with compatible `shpcrop`/`shpdnb`
aliases. No module registry, plugin framework or combined processing engine was introduced.

| Surface | Responsibility / invariant |
|---|---|
| `shared/ui/` | One `importlib.resources` CSS resource served identically by both modules; shared switches, controls and lifecycle script. No Git CSS dependency. |
| `shared/photocraft/bridge.py` | Common JSON-lines transport, auth/config/env precedence, reconnect/errors, session/headless negotiation, document lookup and merged-document fetch. |
| `modules/crop/photocraft_bridge.py` | Signed canvas crop/resize, generated RGBA layer and best-effort undo grouping. Crop alone calls `image.crop`. |
| `modules/dnb/photocraft_bridge.py` | RGBA16 scratch layer, separate mask document, revealSelection retry, Soft Light mode and one layer copy. Never resizes the canvas. |
| `launcher.py` / `supervisor.py` | Verified startup/reuse, fixed proxy, module controls, external status and owned lifecycle. |
| `shared/health.py`, `supervision.py`, `lifecycle.py` | Identity, private work/stop gate, revision guards and desktop/browser leases. |
| `desktop/controller.py` | Standalone system-Python GTK/WebKit window, tray, dialogs, geometry and guarded Quit/reload. |
| `comfy.py` | Dedicated process watchdog, actual listener ownership and queue-aware shutdown. |

Original PhotoCraft clients were compared before extraction; compatible transport/session/
fetch/launch differed only by prefixes, config paths and labels, now subclass attributes.
Do not combine the incompatible result-application semantics. Original best-effort cleanup,
reconnect/retry and undo behavior remain; this is not a transaction redesign.

Crop's `expand.py` is unchanged: signed margins, Free height, validation, ComfyUI graph,
WebSocket progress/polling fallback, seam correction, variations and cancellation.
D&B's correction math, mask composition, ICC handling, output precision, scale cues, picker,
preview crop and full-resolution processing remain. Analysis scripts use package-relative
imports. Segmenter/model sessions are lazy; external missing files/inference failures
produce actionable errors rather than successful-looking results. ExifTool comes from
`PATH` / `DNB_EXIFTOOL`, never a copied tool tree.

## Supervisor, proxy and lifecycle

Tools shell **18760** proxies `/crop/` and `/dnb/` to owned backends **18768 / 18765**.
Both module documents use relative assets/API/image URLs, working standalone or prefixed
without response rewriting. D&B's inherited standalone resize request is suppressed inside
iframes. Same-sized mounted frames switch through visibility/inert/ARIA, not `display:none`,
navigation or recreation; image/preview/zoom/prompt state survives switching.

Startup locks serialize launcher clicks. Reuse checks package/interpreter/data/models/config,
PID and random instance, not simply an open port. The supervisor manages only children it
created with matching supervisor identity and private environment-only capabilities.
Standalone/unrelated servers are never adopted or stopped; port collisions fail visibly.
One unavailable module leaves the shell/other module accessible.

Module controls are `/api/modules/{crop|dnb}/{start|stop|restart}`. The exact loopback Host,
same-origin JSON, non-simple `X-SHP-Control: 1` header, fixed allowlist, bounded bodies and
confirmation types are checked. Owner routes/capabilities are not proxied or exposed.
Stop/Restart cannot override active API work or Crop generation. Loaded work needs confirmation;
the original instance and server-provided revision must still match. Other windows share
backend work, so stale/changed-work confirmations are refused. Restart replaces only its frame.
Only a never-ready owned startup child is eligible for forced startup cleanup.

The top-level shell owns browser leases: 20-second long polls, 90-second expiry,
15-second final-close grace and two-minute startup reservation. The desktop owns an independent
`desktop-<uuid>` lease from a Python thread, retained while hidden. The injected
`__shpOwnedDesktop` marker disables only that webview's redundant JS lease; it adds no native
privilege bridge. Other browser sessions keep independent leases. Direct backend pages do not
extend supervised lifetime. Manual module commands persist unless given `--close-with-ui`.

Last-session shutdown drains API/generation work before closing owned modules and ComfyUI.
Module children detect parent loss and finish work before exit. Suspension/crash/lease expiry
can shut down otherwise unused services; reopen the launcher. Only saved JSON/results persist.
Refresh/reload clears browser-only state; module stop/restart clears in-memory work for every
window using it. No automatic snapshot/recovery was introduced.

## Native desktop and tray

The native helper uses `/usr/bin/python3` and GI GTK3/WebKit2-4.1/AppIndicator3;
project uv Python stays separate. Bindings/display preflight opens no window or data.
It needs a graphical display and tray host; GNOME uses AppIndicator support.
The deterministic per-origin GApplication D-Bus ID guarantees single-instance activation.
GLib program name, GDK program class, icon name and desktop `StartupWMClass` are all
`shp-photocraft-tools`, matching the pinned desktop basename.

GTK3's actual Wayland `app_id` followed its program class, not the D-Bus ID: merely changing
`StartupWMClass` to the D-Bus name was insufficient. Actual protocol assertions now cover
the correct identity; the user confirmed one GNOME dash icon without repinning/favorites edits.
The intentional AppIndicator tray icon is separate.

X hides to tray; relaunch/Show presents the same mounted webview. No tray host/lost tray host
keeps or restores an accessible window. Show/Hide/Quit and Crop/D&B Start/Stop/Restart use
the existing supervisor API. HTTP/control workers run off GTK's main thread; UI/dialog updates
use the main loop. Epoch/pending-action guards prevent stale polls from overwriting controls.
Disconnected/transitional/busy actions are disabled; failures surface rather than force-retry.

Quit warns about unsent/view-state loss and rechecks busy state and work signature after
confirmation. Cancel, stale or busy work keeps the window. Other browser leases retain services.
If the supervisor is unavailable, closing only the desktop is explicitly confirmed without
force-stopping processes. Guarded Reload UI recovers renderer/load errors without service restart.
Native notifications overlay rather than resize the viewport.

Same-origin navigation/downloads only; popups, external navigation and browser device/
notification permissions are refused. Native prompt/confirm dialogs and Save chooser preserve
cancel/overwrite handling. Failed/canceled downloads are never reported as completed.

WebKit uses `<XDG_DATA_HOME>/shp-photocraft-tools/webview/{data,cache}` without moving old
browser profiles. Debounced atomic `window.json` stores normal sizes, not maximized/fullscreen.
Default GTK size **1399x949** includes the titlebar; WebView minimum is **363x626**.
X11 coordinates restore; Wayland placement remains compositor-controlled.

## External services and managed ComfyUI

One PhotoCraft tray line reports **Reachable / Unreachable (TCP)** without auth/document access.
Equal module endpoints share one probe, including failure results. Different endpoints show
an actionable mismatch; module auth/env compatibility remains independent.
Tools never starts/stops PhotoCraft for status.

Optional `comfy` launcher config and installer `--comfy-dir` select an existing runtime and
dedicated port **18788**; 8188 is disallowed. The watchdog launches that checkout's
`venv`/`.venv` Python, existing models/custom nodes, with isolated
`comfy-{user,input,output,temp}` state directories. No installation/download/adoption.
Occupied dedicated ports fail. Supervised Crop receives the managed URL; its retained startup
API is intercepted by the supervisor, and direct supervised calls cannot spawn a duplicate.

Linux `/proc/net/tcp` listener inode plus child `/proc/<pid>/fd` verifies actual ownership;
the real `/queue` supplies busy/readiness. Atomic instance-scoped status reports phases/errors
through the tray. Native Quit/lifetime protection includes owned queue work. Modules close first;
the watchdog drains the queue before stopping only its child, including after parent loss.
If an established queue becomes unreadable, shutdown conservatively waits and reports it.
Startup failures are visible and do not trigger a hidden retry loop.

Standalone Crop retains its original ComfyUI URL/start API behavior. Crop's web AI panel no
longer has a Start button/warning block; readiness retries remain, Generate is gated and genuine
model/node errors go to the main status bar. Independent ComfyUI processes remain unowned.

## Current UI and runtime files

Both modules show **Get Image / Send Result**, with original semantics/tooltips. Shared CSS
provides dark select/option styling, 74px labels, 42px readouts, 11px toolbar vertical padding
and preset buttons 1px shorter than their dropdown. Global real-checkbox switches are
**32x18**, grey off/blue on, with disabled dimming, keyboard focus and reduced-motion support.
IDs, label associations, checked state and settings/preset handlers remain.

Crop's empty instruction is removed; loaded dimension/margin text remains. Lowercase
`turbo lora` / `seam colour correction` rows precede model/upscaler selection, aligned right
with **zero inset**, matching D&B. Toggle rows have 2px vertical padding and compensated 6px
inter-row margin; model rows have 4px vertical padding.
D&B's four mask switches have no static "on" text; all six collapsible headers have no arrows.
Native header click/keyboard collapse and independent switch/picker behavior remain.
Shell tabs are centered, up to 260px each, shrink equally, and have no blue top stripe or
repeated web title. The native window retains its title.

Installer sources are `scripts/install_desktop.py` and `desktop/`; identical reinstalls are
safe, differing Tools files are refused. Desktop trust failures explain manual Allow Launching.
The installed wrapper uses this repo's `.venv` without network sync.
Current external resource references are `/home/stef/dnb/models`,
`/home/stef/dnb/tools/Image-ExifTool-13.60/exiftool` and `/home/stef/ComfyUI`.
Config is `~/.config/shp-photocraft-tools/launcher.json`, with no credentials.
Module data remains `~/.local/share/shp-photocraft-tools/{crop,dnb}`.
Logs/lock and managed ComfyUI directories are under
`<XDG_STATE_HOME>/shp-photocraft-tools/`.

`SHPC_DATA` / `DNB_DATA` isolate standalone writes; `DNB_MODELS` / `DNB_EXIFTOOL` select
external resources. Supervised modules use launcher-owned data directories. Original Crop
`{presets,last}` / settings and D&B `{presets,default}` JSON schemas remain.
Module `SHPC_PC_*` / `DNB_PC_*` overrides take precedence over `SHP_PC_*`; existing OpenFill
token/exchange are read at connection time when configured. Browser keys stay module-prefixed
but origin/profile scoped. Do not import secrets or reuse original writable data for checks.

## Validation and limitations

`tests/run.py` sets temporary data/model/token/exchange paths before imports, then runs eight
migrated Crop checks and the complete unittest suite. `tests/preview.py` and
`tests/shell_preview.py` use synthetic images, fake PhotoCraft, stub ComfyUI and fake model
results with real CPU D&B correction, on free ports. Tests neither infer on GPU nor mutate
PhotoCraft or start real ComfyUI.

Opt-in `SHP_DESKTOP_SMOKE=1` exercises actual GTK/WebKit against synthetic children: real canvas
readback/pointer capture/wheel zoom, generation/previews, retained iframe state, native dialogs/
downloads, geometry/localStorage across restart, hide/relaunch, tray controls, stale/busy/Cancel
guards, independent leases and final shutdown. It deliberately terminates only its own test
renderer to verify guarded recovery. Computed styles cover dark controls, all ten switches,
Crop zero-inset alignment and D&B arrowless header toggling/switch isolation.

Migration checks additionally verified wheel installation outside the checkout, both command
aliases, identical packaged CSS, usual/narrow layouts and unchanged source hashes/dirty D&B diff.
Real GPU/PhotoCraft integration was not repeated. Separately approved managed ComfyUI activation
verified runtime/custom-node startup and ownership with an idle queue, without inference.
Source notes retain algorithmic limitations: scale heuristics, tattoos/nostrils and nonidentical
Photoshop Soft Light. The inherited D&B preset toolbar can overflow around 344px width;
no unrelated responsive redesign was included.

Publication verification on **2026-11-05** passed **54 unittest checks with native smoke
enabled, plus eight migrated Crop checks**. Wheel/sdist builds contain all module/shared/
shell/native resources and the installer/docs. A local wheel installation outside the checkout
passed all five command help checks, four standalone command/alias HTTP and identical-asset
checks, and a full regression rerun (native smoke skipped only in that installed-wheel rerun).
The installed native helper's runtime preflight created no UI data.
Validation used only temporary services; no live app/window was reloaded or restarted for publication.
