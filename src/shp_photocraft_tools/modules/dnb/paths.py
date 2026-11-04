"""Writable D&B data and external model locations, independent of the installed package."""

import os
from pathlib import Path

DATA = Path(os.environ.get("DNB_DATA", Path.home() / ".local" / "share" / "shp-photocraft-tools" / "dnb")).expanduser()
MODELS = Path(os.environ.get("DNB_MODELS", DATA / "models")).expanduser()


def model_path(name):
    path = MODELS / name
    if not path.is_file():
        raise FileNotFoundError(f"SHP PhotoCraft D&B needs {path}; see MODELS.md or set DNB_MODELS")
    return path
