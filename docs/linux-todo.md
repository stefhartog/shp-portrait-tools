# Linux: closing the gap after the Windows-first desktop changes

Written 2026-11-21 after the shell/proxy removal (`main` at the commit that added this file). Windows is
current and verified live; **Linux is knowingly broken** until the items below are done. Work in the fresh
clone set up by "Start here" below. Tests that need Linux (GTK, JavaScriptCore, Firefox, `/proc`, pidfds,
bubblewrap) could not run on Windows; their status is noted per item.

## Start here: a fresh install on the Linux machine

For the AI session on the Linux machine. Read `CLAUDE.md` first (it is written for Windows; on Linux use
plain `uv`, not `python -m uv`). Ask the user before stopping a running Tools instance, before moving or
replacing any existing file, and before anything that needs `sudo`. Run each step from a normal terminal,
not from a sandbox that redirects `$HOME`.

**Goal: ignore the existing install and set up a fresh clone with fresh settings.** The old checkout
(`~/shp-photocraft-tools`) and the old data/config/state folders (`shp-photocraft-tools` under
`~/.local/share`, `~/.config`, `~/.local/state`) are left untouched; nothing is migrated from them except
copying the model files. The user deletes the old install later. The watch folder no longer exists
(removed 2026-11-21), so the old checkout's `watchfolder` is not used.

1. **Quit the old Tools.** Ask the user to quit it from the tray and save any work first. Check nothing is
   left: `pgrep -af 'shp_photocraft_tools|shp_portrait_tools'`, `ss -ltnp | grep -E ':(18760|18765|18768)\b'`.
   If something remains, show it to the user; don't kill it without their OK.
2. **Clone a fresh copy.**
   ```sh
   test ! -e ~/shp-portrait-tools || echo "exists: ask the user"
   git clone https://github.com/stefhartog/shp-portrait-tools.git ~/shp-portrait-tools
   cd ~/shp-portrait-tools
   git log --oneline -1        # compare with the newest commit on GitHub
   ```
   If `~/shp-portrait-tools` already exists, stop and ask the user what to do with it.
3. **Install the environment** (from inside the new clone):
   ```sh
   uv python install 3.13
   uv sync --locked --extra gpu
   uv run --no-sync python -c "import shp_portrait_tools, sys; print(shp_portrait_tools.__file__, sys.version)"
   ```
   The printed path must be inside `~/shp-portrait-tools`.
4. **Create fresh, empty Tools folders so nothing is migrated.** The launcher and installer move the old
   `shp-photocraft-tools` folders only when the new `shp-portrait-tools` folder does not exist yet; creating
   the new ones first leaves the old ones alone:
   ```sh
   mkdir -p ~/.local/share/shp-portrait-tools/skin/models ~/.config/shp-portrait-tools ~/.local/state/shp-portrait-tools
   ```
   Copy (don't move) the three ONNX models from the old install: look in
   `~/.local/share/shp-photocraft-tools/dnb/models` (or wherever the old
   `~/.config/shp-photocraft-tools/launcher.json` `models` points):
   ```sh
   cp -n <old models folder>/{yunet.onnx,face_landmarks.onnx,selfie_multiclass_256x256.onnx} \
     ~/.local/share/shp-portrait-tools/skin/models/
   ```
   Check them against the hashes in `MODELS.md`. The PhotoCraft control folders under
   `~/.config/shp-photocraft-tools/<module>` (token, exchange) keep being used by design; don't move them.
5. **Install the desktop launcher.**
   ```sh
   uv run --no-sync python scripts/install_desktop.py \
     --models "$HOME/.local/share/shp-portrait-tools/skin/models" [--exiftool /path/to/exiftool]
   ```
   The launcher files keep their old names (`~/.local/bin/shp-photocraft-tools`,
   `~/.local/share/applications/shp-photocraft-tools.desktop`, the Desktop shortcut and icons), so the old
   install's copies are in the way: the installer stops with "Refusing to overwrite an existing different
   file: …". With the user's OK, rename exactly the file it names to `<file>.bak-YYYYMMDD` and run it again,
   one file at a time. Afterwards `~/.local/bin/shp-photocraft-tools` must exec
   `~/shp-portrait-tools/.venv/bin/python -m shp_portrait_tools.launcher`, and
   `~/.config/shp-portrait-tools/launcher.json` must name the new models folder and `ports.skin` 18765.
   (The ExifTool path, if used, is in the old `launcher.json`.)
6. **Start it without the native window** (the GTK window is broken until item 1 of "To do" is done):
   ```sh
   uv run --no-sync shp-portrait-tools --no-browser
   ```
   Open `http://127.0.0.1:18760/` in a normal browser: the status page moves on to Skin Tools
   (`http://127.0.0.1:18765/`). Check the five tabs, Settings & Info (connection: choose PhotoCraft), Get
   Image from PhotoCraft and Send Layers. Settings and presets start at their defaults. In a plain browser
   Standalone's Get Image has no file picker; use the Image analysis list or path box.
   Logs: `~/.local/state/shp-portrait-tools/` and `~/.local/share/shp-portrait-tools/state/`.
7. **Run the tests** and compare with "Run and fix the Linux-only tests" below:
   `uv run --no-sync python tests/run.py`.
8. Then work through **To do**, starting with the Qt host, in the new clone. AI Fill is the separate
   `github.com/stefhartog/shp-ai-fill` repository; clone it into its own folder only if the user wants it on
   this machine.

## What changed under Linux's feet

1. **The shell and its `/skin/` proxy are gone.** The supervisor (port 18760) now serves only health,
   status, module controls, the native quit, UI leases and a small status page at `/`
   (`supervisor.STATUS_PAGE`). That page moves on to the Skin Tools page served **directly by the worker**
   (port 18765, a different origin) once Skin Tools is running. Tabs and Settings & Info now live in the
   Skin Tools page itself.
2. **Desktop host core.** `desktop/host.py` `DesktopHost` holds everything platform-neutral (geometry,
   status polling, tray model, module controls, quit/restart/reload, the 90 s drain,
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
- has its own copy of the status/tray/quit logic that `DesktopHost` now owns (its watch-folder wake was
  removed with the feature; `test_desktop.py` lost the matching test).

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

- The fresh install in "Start here" skips the folder migration on purpose (the new folders exist first).
  `migrate_legacy_roots()` therefore stays unexercised on Linux; it moves `~/.local/share/shp-photocraft-tools`
  → `shp-portrait-tools` (`dnb`, `state`, `webview`, `watchfolder`; AI Fill's `crop` stays), `~/.config/...`
  (launcher.json*, settings.json, photoshop, Chromium profile; the per-module PhotoCraft folders stay),
  `~/.local/state/...` (all), then renames `data/dnb` → `data/skin` and rewrites launcher.json paths, only
  when the new folder is missing.
- PhotoCraft control folders stay at `~/.config/shp-photocraft-tools/<module>` by design (`detect_config`),
  as do the `photocraft-dnb` LightCraft wrapper and `DNB_PC_*`.
- The watch folder and its hard-coded Linux inbox (`LINUX_INBOX_ROOT`) are removed; nothing on Linux
  depends on the checkout path any more.
- App identity strings (`.desktop` name, `StartupWMClass`, icon names `shp-photocraft-tools*`) are
  unchanged; rename them together with the Qt host if wanted.

## Done when

- The desktop window on Linux opens the status page, moves on to the Skin Tools page, shows the tabs and
  Settings & Info, and Open/Batch pickers work through the JS bridge.
- Tray: PhotoCraft line, Skin Tools submenu, Show/Hide/Reload/Restart/Force Restart/Quit, with the same
  confirmations as Windows.
- `tests/run.py` on Linux has no failures beyond ones documented as pre-existing.
