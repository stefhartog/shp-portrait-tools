# Linux: status and remaining follow-ups

The Windows-first desktop changes (shell/proxy removal, `DesktopHost`, pywebview pickers) left Linux without
a working native window. That gap is closed as of 2026-11-21: the Linux desktop host is
`desktop/qt_controller.py` (pywebview on Qt WebEngine with a `QSystemTrayIcon`), and the GTK controller and its
tests are gone. This page keeps the fresh-install steps and lists what is still open.

## Fresh install on a Linux machine

For people and AI sessions alike. `CLAUDE.md` uses `python -m uv` (the Windows machine); on Linux use plain
`uv`. Ask the user before stopping a running Tools instance, before moving or replacing any existing file, and
before anything that needs `sudo`; `sudo` needs a real terminal, not the `!` prefix of an AI session.

1. **Quit any old Tools** from its tray after saving work. Check nothing is left:
   `pgrep -af 'shp_photocraft_tools|shp_portrait_tools'`, `ss -ltnp | grep -E ':(18760|18765)\b'`.
2. **Clone.**
   ```sh
   test ! -e ~/shp-portrait-tools || echo "exists: decide what to do with it first"
   git clone https://github.com/stefhartog/shp-portrait-tools.git ~/shp-portrait-tools
   cd ~/shp-portrait-tools
   ```
3. **Install the environment.** It includes the desktop window (pywebview, PySide6/Qt WebEngine); no system
   GUI packages are needed.
   ```sh
   uv python install 3.13
   uv sync --locked --extra gpu
   ```
4. **Models.** Put `yunet.onnx`, `face_landmarks.onnx` and `selfie_multiclass_256x256.onnx` in
   `~/.local/share/shp-portrait-tools/skin/models` and check them against `MODELS.md`. To keep an older
   `shp-photocraft-tools` install's folders untouched, create the new `shp-portrait-tools` folders under
   `~/.local/share`, `~/.config` and `~/.local/state` first and copy the models across; the launcher and
   installer migrate the old folders only when the new ones do not exist.
5. **Desktop launcher.**
   ```sh
   uv run --no-sync python scripts/install_desktop.py \
     --models "$HOME/.local/share/shp-portrait-tools/skin/models" [--exiftool /path/to/exiftool]
   ```
   The launcher files keep their old names (`~/.local/bin/shp-photocraft-tools`, the
   `shp-photocraft-tools.desktop` entries and icons), so an older install's copies stop it with "Refusing to
   overwrite an existing different file". With the user's OK, rename the named file to
   `<file>.bak-YYYYMMDD` and run it again. The installer ends with a desktop-runtime check and warns if the
   window cannot start.
6. **PhotoCraft (optional).** See README → [PhotoCraft connection](../README.md#photocraft-connection): Tools
   needs the same control port, token file and exchange folder as the running PhotoCraft, saved in the
   `photocraft` section of `~/.config/shp-portrait-tools/launcher.json`. A native PhotoCraft (RPM/deb from
   the PhotoCraft releases page) is simplest: the Flatpak's sandbox cannot see the token or exchange folders
   without extra `--filesystem` grants.
7. **Start** from the app menu or `~/.local/bin/shp-photocraft-tools`. GNOME needs the AppIndicator extension
   for the tray icon; without a tray, closing the window asks to quit instead of hiding.
   `shp-portrait-tools --no-browser` still runs the services alone (open `http://127.0.0.1:18760/`).
   Logs: `~/.local/state/shp-portrait-tools/` (`desktop.log` is the window's).
8. **Tests:** `uv run --no-sync python tests/run.py`; compare with "Tests" below.

## Done (2026-11-21)

- **Qt host** (`desktop/qt_controller.py`, `tests/test_qt_host.py`): navigation limited to the supervisor
  and worker origins, `about:blank` and their `blob:` URLs; popups blocked; every permission request denied
  (pywebview's own page grants camera/microphone, so its page class is replaced); `__shpOwnedDesktop` and
  `__shpNativeFileDialog` injected at document creation; pickers through `PickerApi`; Qt confirmations;
  tray over the shared model; `QLocalServer` single instance; `/proc` identity for restart. The portal
  platform theme gives GNOME's own (dark-aware) file chooser.
- **Launcher** starts it from the project environment; pywebview, PySide6 and qtpy are Linux dependencies.
- **GTK controller removed**, with `tests/desktop_smoke.py` and the tests that exec'd GTK-only code.
- `shared/filelock.py` honours its timeout on Linux (it blocked forever); `tests/run.py` isolates XDG
  config/state so the user's `settings.json` cannot change results.
- **Verified live** by the user on Fedora/GNOME Wayland: window and tabs, tray menu, hide to tray and Show,
  native pickers (Get Image, Batch) in Standalone, Get Image from a native PhotoCraft 0.23.1.

## Still open

1. **Live check of the tray's Reload, Force Restart and Quit** on Linux (covered by `test_desktop_host`
   with a fake platform). **Restart** was verified live on 2026-11-21: drain, launcher relaunch from the
   installed wrapper, and a fresh window back on Skin Tools.
2. **Real-window smoke test** for the Qt host, replacing the removed GTK one (opt-in, needs a session).
3. **`--browser` mode** (Ungoogled Chromium app window): check it still works now that the status page
   redirects to the worker origin, or drop it.
4. **Plain-browser pickers.** Without the pywebview bridge the page falls back to `prompt('SHP_OPEN_*')`,
   which no host answers any more, so a plain browser shows a raw prompt. Decide whether to replace it with
   a path box or a hint.
5. **Tests** (`tests/run.py` on Linux with `photocraft-cli` installed: 492 tests, 114 failures, 8 errors).
   These look stale rather than Linux-specific: most are in tests that skip without `photocraft-cli`, so
   they never ran on Windows.
   - `test_dnb_transfer`, `test_transfer_options`, `test_skintone_transfer`, `test_directional_blur`
     (`NativeTests`): expect the old `Hue`/`Blur`/`Directional Blur` layers; the stack has a `Corrections`
     group since `7c06cc1` (2026-11-17).
   - `test_dnb`, `test_second_band`: old page title and `/img/<key>` routes (404).
   - `test_skintone`: predates the connection choice ("Selected editor differs from the fetched source").
   - `test_pose_backend` (`prepare_loaded_image` arguments), `test_fs_toggle`, `test_fs_pipeline` (fails the
     same way on `main`; the overlap test is timing-sensitive).
   - `test_dnb_progress` (JavaScriptCore harness) and `test_status_layout` (`renderTabs is not defined`;
     re-add a layout check for the in-page tab bar and Settings & Info).
6. **Migration on a real install.** `migrate_legacy_roots()` has not run on Linux (fresh installs create the
   new folders first). It moves `~/.local/share|~/.config|~/.local/state/shp-photocraft-tools` app entries to
   `shp-portrait-tools`, renames `data/dnb` to `data/skin` and rewrites `launcher.json` paths; the per-module
   PhotoCraft control folders and AI Fill's `crop` stay by design.
7. **App identity strings** (`.desktop` name, `StartupWMClass`, icon names `shp-photocraft-tools*`, the Qt
   host's `APP_ID`) are unchanged; rename them together if wanted.
8. **LightCraft hand-off** (`scripts/install_editor_handoff.py`, `photocraft-dnb`/`photocraft-lightcraft`)
   assumes `~/.local/bin/photocraft` and LightCraft's settings; not set up or checked against the Qt host.
