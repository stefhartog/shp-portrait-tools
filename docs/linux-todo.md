# Linux: closing the gap after the Windows-first desktop changes

Written 2026-11-21 after the shell/proxy removal (`main` at the commit that added this file). Windows is
current and verified live; **Linux is knowingly broken** until the items below are done. Work on a fresh
pull on the Linux machine. Tests that need Linux (GTK, JavaScriptCore, Firefox, `/proc`, pidfds,
bubblewrap) could not run on Windows; their status is noted per item.

## Start here: update the Linux machine and get Tools running

For the AI session on the Linux machine. Read `CLAUDE.md` first (it is written for Windows; on Linux use
plain `uv`, not `python -m uv`). Ask the user before stopping a running Tools instance, before moving or
replacing any existing file, and before anything that needs `sudo`. Run each step from a normal terminal
in the checkout, not from a sandbox that redirects `$HOME`.

The checkout lives at **`~/shp-photocraft-tools`** and must stay there: the Linux watch folder is
`/home/stef/shp-photocraft-tools/watchfolder` (`platform_paths.LINUX_INBOX_ROOT`, ignored by git), and the
installed launcher points into this folder's `.venv`. So update it **in place**; do not clone into a new
folder or move the old one aside.

1. **Quit Tools.** Ask the user to quit from the tray (or close the Tools window and stop the services) and
   to save any work first. Check nothing is left: `pgrep -af shp_photocraft_tools`,
   `ss -ltnp | grep -E ':(18760|18765|18768)\b'`. If something remains, show it to the user; don't kill it
   without their OK.
2. **Pull the current `main` from GitHub.**
   ```sh
   cd ~/shp-photocraft-tools
   git status --short          # must be clean (ignored inbox/models/settings are fine)
   git remote -v               # expect github.com/stefhartog/shp-portrait-tools
   git fetch origin
   git switch main
   git pull --ff-only origin main
   git log --oneline -1        # compare with the newest commit on GitHub
   ```
   If `git status` shows local changes, or the pull is not a fast-forward, stop and ask the user (offer
   `git stash` or a backup branch); never reset or discard their work. If the remote still has the old
   repository name, GitHub redirects; `git remote set-url origin https://github.com/stefhartog/shp-portrait-tools.git`
   only with the user's OK.
3. **Rebuild the environment** (the package was renamed to `shp_portrait_tools`, so this must be a real
   sync, not `--no-sync`):
   ```sh
   uv sync --locked --extra gpu
   uv run --no-sync python -c "import shp_portrait_tools, sys; print(shp_portrait_tools.__file__, sys.version)"
   ```
4. **Reinstall the desktop launcher.** The old `~/.local/bin/shp-photocraft-tools` wrapper still runs
   `shp_photocraft_tools.launcher`, which no longer exists. The installer first migrates the folders
   (`~/.local/share|~/.config|~/.local/state/shp-photocraft-tools` → `shp-portrait-tools`, `data/dnb` →
   `data/skin`, `launcher.json` paths rewritten; see item 5 under "What changed" below), then writes the
   wrapper, `launcher.json`, the `.desktop` files and icons.
   - Note the old settings first: `cat ~/.config/shp-photocraft-tools/launcher.json` (or the
     `shp-portrait-tools` one if already migrated) for `models` and `exiftool`.
   - Models end up in `~/.local/share/shp-portrait-tools/skin/models` after the migration.
   ```sh
   uv run --no-sync python scripts/install_desktop.py \
     --models "$HOME/.local/share/shp-portrait-tools/skin/models" [--exiftool /path/to/exiftool]
   ```
   It refuses to overwrite a file whose content differs ("Refusing to overwrite an existing different
   file: …"); the old wrapper and `launcher.json` will differ. With the user's OK, rename exactly the file it
   names to `<file>.bak-YYYYMMDD` and run it again, one file at a time. Afterwards check
   `~/.config/shp-portrait-tools/launcher.json` (`ports.skin` 18765; old `dnb`/`crop`/`comfy` keys are gone or
   ignored) and that `~/.local/share/shp-portrait-tools/skin` holds the models, presets and settings.
5. **Start it without the native window** (the GTK window is broken until item 1 of "To do" is done):
   ```sh
   uv run --no-sync shp-portrait-tools --no-browser
   ```
   Open `http://127.0.0.1:18760/` in a normal browser: the status page moves on to Skin Tools
   (`http://127.0.0.1:18765/`). Check the five tabs, Settings & Info (connection Photoshop/PhotoCraft/
   Standalone; PhotoCraft is the one that matters on Linux), Get Image from PhotoCraft and Send Layers.
   In a plain browser Standalone's Get Image has no file picker; use the Image analysis list or path box.
   Logs: `~/.local/state/shp-portrait-tools/` and `~/.local/share/shp-portrait-tools/state/`.
6. **Run the tests** and compare with "Run and fix the Linux-only tests" below:
   `uv run --no-sync python tests/run.py`.
7. Then work through **To do**, starting with the Qt host. AI Fill is now the separate
   `github.com/stefhartog/shp-ai-fill` repository; clone it into its own folder only if the user wants it
   on this machine (its data stays in `~/.local/share/shp-photocraft-tools/crop`).

## What changed under Linux's feet

1. **The shell and its `/skin/` proxy are gone.** The supervisor (port 18760) now serves only health,
   status, module controls, the native quit, UI leases and a small status page at `/`
   (`supervisor.STATUS_PAGE`). That page moves on to the Skin Tools page served **directly by the worker**
   (port 18765, a different origin) once Skin Tools is running. Tabs and Settings & Info now live in the
   Skin Tools page itself.
2. **Desktop host core.** `desktop/host.py` `DesktopHost` holds everything platform-neutral (geometry,
   status polling, watch-folder wake, tray model, module controls, quit/restart/reload, the 90 s drain,
   following the worker to the status page after stop/restart, the page address via `/api/health`).
   `windows_controller.Host` is the only subclass so far.
3. **File pickers use pywebview's JS API.** The page calls `window.top.pywebview.api.pick_image()` /
   `pick_images()` / `pick_folder()` (`host.PickerApi`) and asks Folder-or-Images in its own
   `#batchSourceDialog`. Without the bridge it falls back to `prompt('SHP_OPEN_*')`, which the GTK
   controller answers — but see item 1 below.
4. **Connection choice** (page and server only, nothing Linux-specific): Settings & Info's connection is
   Photoshop / PhotoCraft / Standalone. Editors get Get Image + Send Layers; Standalone gets Get Image (local
   file through the picker bridge or the `prompt` fallback) + Send Result, and is the only place Batch
   Processing shows. The PhotoCraft launch button is gone. On Linux PhotoCraft already starts in its own
   session (`start_new_session`), so the Windows Job Object fix for tray Quit/Restart has no Linux counterpart.
5. **Renames and folder migration** (already in place, never exercised on Linux): package
   `shp_portrait_tools`, distribution/commands `shp-portrait-tools` / `shp-skin-tools`, module ID `skin`
   (route `/skin/` is now unused, env `SKIN_*` with `DNB_*` fallbacks), folders
   `~/.local/share|~/.config|~/.local/state/shp-portrait-tools` with `migrate_legacy_roots()` on first
   start (moves only app-owned entries, then renames `data/dnb` to `data/skin`).

## Why the GTK controller no longer works

`desktop/controller.py` (system Python, GTK3/WebKit2) still:

- loads `self.base + "/"` (now the status page) and its navigation policy only allows `self.base`, so the
  status page's redirect to the worker origin (port 18765) is **blocked**;
- injects `__shpNativeFileDialog` only for `self.base`, so the Skin Tools page (worker origin) would not
  offer local Open even if navigation were allowed;
- answers `prompt('SHP_OPEN_*')` only for `self.base`;
- has its own copy of the status/tray/quit logic that `DesktopHost` now owns, including the watch-folder poll
  to the supervisor's removed `/skin/api/watch-folder` route (a 404 that shows "Tools connection lost" and
  empties the tray status; `DesktopHost` asks the worker origin instead).

Until the Qt host lands, use Tools on Linux without the native window: `shp-portrait-tools --no-browser`, then
open `http://127.0.0.1:18760/` in a normal browser (the status page moves on to Skin Tools). File pickers are not
available there; use the Image analysis list or path box and press Load.

Patching it is possible (allow and flag the worker origin from `/api/health` `ports.skin`), but the plan is
to replace it.

## To do

### 1. Qt host on `DesktopHost` (replaces the GTK controller)

- `desktop/qt_controller.py`: `class Host(DesktopHost)` with pywebview `gui="qt"`, run from the project's
  uv environment (not `/usr/bin/python3`).
  - **Engine hooks** (`QWebEnginePage`): navigation allowed only for `self.trusted_origin(origin)`
    (supervisor + worker), `about:blank` and `blob:` from those origins; deny permission requests; block
    new windows; inject `__shpOwnedDesktop` (top frame) and `__shpNativeFileDialog` (worker origin) at
    document creation (`QWebEngineScript`, `DocumentCreation`).
  - **Dialogs**: `confirm(title, text, action)`, `error(title, text)` — `QMessageBox`, or pywebview
    `window.create_confirmation_dialog`.
  - **Pickers**: nothing to do; `host.Picker` + `PickerApi` are already used through `create_window(js_api=…)`.
  - **Tray**: `QSystemTrayIcon` created on the Qt GUI thread (pywebview's Qt backend owns the
    `QApplication`), menu built from the shared model: `external_lines[0]`, `module_title("skin")` with
    Load on startup / Start / Stop / Restart (`module_action_enabled`, `startup_checked`, `module_idle`),
    then `window_actions()`. Implement `notify`, `refresh_tray`, `stop_tray`. GNOME still needs the
    AppIndicator extension.
  - **Single instance**: `QLocalServer`/`QLocalSocket`, or a lock file plus a loopback ping, with
    `listen_for_second_launch()` semantics (`single.wait_for_show`).
  - **Process helpers** for restart: `own_identity()` (pid + start ticks from `/proc`), `process_alive()`,
    `spawn_detached()` — reuse `shared/owned_process.py`.
- `launcher.desktop_command()` (Linux branch): start `-m shp_portrait_tools.desktop.qt_controller` with
  `background_python()`; keep `--check`.
- `pyproject.toml`: make `pywebview` (and a Qt binding: PySide6 or PyQt6 + QtWebEngine) a Linux
  dependency too; relock with `uv lock`.
- Prototype first, as planned: the `QSystemTrayIcon` on the GUI thread, and whether pywebview's Qt backend
  exposes the native page/profile objects the hooks need.

### 2. Remove the GTK controller and its tests

- Delete `desktop/controller.py`.
- Tests that exec or import it: `test_desktop.py` (GTK command/runtime and the tray/wake tests),
  `test_module_isolation.py` (`test_native_notifications_…`, `test_notice_timers_…`),
  `test_native_lifecycle.py` (GTK pieces), `test_local_results.py` (`NativeChooserTests` GTK prompt
  answering). Port the behaviours that still matter to Qt-host tests or to `test_desktop_host.py`.
- `tests/desktop_smoke.py`: rewrite for the Qt host; it still drives the removed AI Fill module and the
  removed shell.

### 3. Browser mode and installer

- `--browser` (Ungoogled Chromium app window) opens `http://127.0.0.1:18760/`: the status page now redirects
  to the worker origin. Check it still works as an app window, or drop the mode.
- `scripts/install_desktop.py`: drop GTK/WebKit/AppIndicator system-package requirements once the Qt host
  ships; the `.desktop` wrapper already runs `-m shp_portrait_tools.launcher`.
- `docs/portrait-tools-fresh-linux-setup.md` and `README.md` still describe the GTK setup.

### 4. Run and fix the Linux-only tests

On Windows these were skipped or run through a Node stand-in; results there were unchanged from before the
desktop changes, i.e. these **pre-existing** failures remain:

- `test_dnb_progress.test_feedback_summary_log_and_inline_syntax_without_browser` (JavaScriptCore):
  stale expectations — the workflow-button and local-transfer harnesses predate the connection choice
  (`pcConnect` is gone; Send Result/Send Layers visibility now follows `editorSelect`), the batch harness
  expects old calls, the feedback harness fails at its first check, and an
  "Always exaggerated diagnostic" check. The shell/AI Fill parts were removed from it.
- `test_status_layout` (Firefox/Marionette): AI Fill and shell checks removed; **re-add a layout check for
  the in-page tab bar and Settings & Info view** (the old shell-based `check_general_settings` was removed).
- `test_native_lifecycle` (pidfds), `test_photocraft_flatpak_peer`, `test_editor_handoff`,
  `test_pose_backend` Google-worker tests, `test_launcher` Linux paths, `test_supervisor` on Linux.
- `tests/run.py` full suite; compare against a run of the previous commit.

### 5. Migration and paths on the real Linux install

- First start of the new launcher migrates `~/.local/share/shp-photocraft-tools` → `shp-portrait-tools`
  (`dnb`, `state`, `webview`, `watchfolder`; AI Fill's `crop` stays), `~/.config/...` (launcher.json*,
  settings.json, photoshop, Chromium profile; the per-module PhotoCraft folders stay), `~/.local/state/...`
  (all), then renames `data/dnb` → `data/skin` and rewrites launcher.json paths. Run it from a normal
  terminal or the desktop launcher and check the folders and `launcher.json` afterwards.
- PhotoCraft control folders stay at `~/.config/shp-photocraft-tools/<module>` by design (`detect_config`),
  as do the `photocraft-dnb` LightCraft wrapper and `DNB_PC_*`.
- `shared/platform_paths.LINUX_INBOX_ROOT` is still hard-coded to `/home/stef/shp-photocraft-tools`
  (the checkout path); make it portable before the Phase 4 installer.
- App identity strings (`.desktop` name, `StartupWMClass`, icon names `shp-photocraft-tools*`) are
  unchanged; rename them together with the Qt host if wanted.

## Done when

- The desktop window on Linux opens the status page, moves on to the Skin Tools page, shows the tabs and
  Settings & Info, and Open/Batch pickers work through the JS bridge.
- Tray: PhotoCraft line, Skin Tools submenu, Show/Hide/Reload/Restart/Force Restart/Quit, with the same
  confirmations as Windows.
- `tests/run.py` on Linux has no failures beyond ones documented as pre-existing.
