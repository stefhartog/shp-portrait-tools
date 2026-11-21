"""Per-user data, config and inbox defaults; Linux values are unchanged, Windows uses %LOCALAPPDATA%/%APPDATA%."""

import os
import sys
from pathlib import Path

APP = "shp-photocraft-tools"
LINUX_INBOX_ROOT = Path("/home/stef/shp-photocraft-tools")


def _windows_dir(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / "AppData" / fallback


def data_root():
    if sys.platform == "win32":
        return _windows_dir("LOCALAPPDATA", "Local") / APP
    return Path.home() / ".local" / "share" / APP


def global_config_dir():
    if sys.platform == "win32":
        return _windows_dir("APPDATA", "Roaming") / APP
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


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
