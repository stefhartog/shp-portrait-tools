"""Per-user data, config and inbox locations, and the one-time move from the pre-rename folder names.

Folders are named `shp-portrait-tools`. Installs from before the rename used `shp-photocraft-tools`;
every lookup prefers the new folder and falls back to the old one, so an install that has not been
migrated (or whose migration was refused because files were in use) keeps working unchanged.
`migrate_legacy_roots()` moves only app-owned entries and is called explicitly by the entry points,
never at import time.
"""

import json
import os
import sys
from pathlib import Path

APP = "shp-portrait-tools"
LEGACY_APP = "shp-photocraft-tools"
# Linux inbox under the developer's checkout; unrelated to the data-folder rename.
LINUX_INBOX_ROOT = Path("/home/stef/shp-photocraft-tools")

# App-owned entries moved by the migration. Anything else stays: AI Fill's `crop` data (shp-ai-fill
# still uses the old folder), the per-module PhotoCraft control folders under config (PhotoCraft is
# configured against them), and files the app did not create.
DATA_ENTRIES = frozenset({"dnb", "state", "webview", "watchfolder"})
CONFIG_ENTRIES = frozenset({"launcher.json", "launcher.json.lock", "settings.json", "photoshop",
                            "shp-photocraft-tools.ico", "ungoogled-chromium-profile"})
MIGRATION_LOCK = ".migration.lock"
MIGRATION_NOTE = "MOVED-TO-shp-portrait-tools.txt"


def _windows_dir(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / "AppData" / fallback


def _data_base():
    if sys.platform == "win32":
        return _windows_dir("LOCALAPPDATA", "Local")
    return Path.home() / ".local" / "share"


def _config_base():
    if sys.platform == "win32":
        return _windows_dir("APPDATA", "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _state_base():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def _choose(base):
    new, old = base / APP, base / LEGACY_APP
    return old if old.is_dir() and not new.exists() else new


def data_root():
    return _choose(_data_base())


def global_config_dir():
    return _choose(_config_base())


def state_root():
    """Logs and launcher lock: under the data folder on Windows, XDG state on Linux."""
    if sys.platform == "win32":
        return data_root() / "state"
    return _choose(_state_base())


def photocraft_config_dir():
    """Per-module PhotoCraft token/exchange folders keep the old name: PhotoCraft is set up against them."""
    return _config_base() / LEGACY_APP


def module_data(env_name, module):
    return Path(os.environ.get(env_name, data_root() / module)).expanduser()


def watch_directory(module_env, linux_name, windows_name):
    """Inbox for one service: its own variable, then the generic WATCH_DIRECTORY, then a platform default."""
    value = os.environ.get(module_env) or os.environ.get("WATCH_DIRECTORY")
    if value:
        return Path(value).expanduser()
    if sys.platform == "win32":
        return data_root() / windows_name
    return LINUX_INBOX_ROOT / linux_name


def legacy_config_path(path):
    """Map a path inside the old config folder to the same path in the new one."""
    old = _config_base() / LEGACY_APP
    try:
        return _config_base() / APP / Path(path).relative_to(old)
    except ValueError:
        return Path(path)


def _config_entry(name):
    return name in CONFIG_ENTRIES or name.startswith("launcher.json.")


def _migrate_root(base, wanted, log):
    """Move wanted entries from base/LEGACY_APP to base/APP. All or nothing; returns the moved names."""
    from .filelock import file_lock

    old, new = base / LEGACY_APP, base / APP
    if new.exists() or not old.is_dir():
        return []
    moved = []
    with file_lock(old / MIGRATION_LOCK):
        entries = [] if new.exists() else sorted(  # another launcher may have finished first
            child.name for child in old.iterdir() if wanted(child.name))
        if entries:
            new.mkdir(parents=True)
            try:
                for name in entries:
                    os.rename(old / name, new / name)
                    moved.append(name)
            except OSError as error:
                failed = entries[len(moved)]
                for name in reversed(moved):
                    os.rename(new / name, old / name)
                new.rmdir()
                moved = []
                log(f"Kept using {old}: could not move {failed} ({error}). "
                    "Close every SHP Portrait Tools window and start it again.")
            else:
                (old / MIGRATION_NOTE).write_text(
                    f"SHP Portrait Tools moved {', '.join(moved)} to {new}.\n"
                    "What is left here was not created by SHP Portrait Tools, belongs to SHP AI Fill (crop),\n"
                    "or is a PhotoCraft control folder that PhotoCraft is configured to use.\n", encoding="utf-8")
    try:
        (old / MIGRATION_LOCK).unlink()
    except OSError:
        pass  # another launcher may hold it; harmless
    if moved:
        log(f"Moved {', '.join(moved)} from {old} to {new}")
    return moved


def _rewrite_launcher_paths(old_data, new_data, moved, log):
    """Point launcher.json values that named a moved data entry (e.g. the models folder) at its new place."""
    config = global_config_dir() / "launcher.json"
    if not moved or not config.is_file():
        return
    try:
        values = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    prefixes = [(str(old_data / name), str(new_data / name)) for name in moved]

    def fix(value):
        if isinstance(value, dict):
            return {key: fix(item) for key, item in value.items()}
        if isinstance(value, str):
            for before, after in prefixes:
                if value == before or value.startswith(before + os.sep) or value.startswith(before + "/"):
                    return after + value[len(before):]
        return value

    updated = fix(values)
    if updated != values:
        temporary = config.with_name(config.name + ".migrating")
        temporary.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, config)
        log(f"Updated folder paths in {config}")


def migrate_legacy_roots(log=lambda message: print(message, file=sys.stderr, flush=True)):
    """One-time move of app-owned folders from `shp-photocraft-tools` to `shp-portrait-tools`."""
    moved_data = _migrate_root(_data_base(), DATA_ENTRIES.__contains__, log)
    _migrate_root(_config_base(), _config_entry, log)
    if sys.platform != "win32":
        _migrate_root(_state_base(), lambda name: name not in (MIGRATION_LOCK, MIGRATION_NOTE), log)
    _rewrite_launcher_paths(_data_base() / LEGACY_APP, _data_base() / APP, moved_data, log)
