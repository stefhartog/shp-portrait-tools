"""Writable D&B data and external model locations, independent of the installed package."""

import os
from pathlib import Path

from shp_photocraft_tools.shared.platform_paths import module_data

DATA = module_data("DNB_DATA", "dnb")
MODELS = Path(os.environ.get("DNB_MODELS", DATA / "models")).expanduser()


def model_path(name):
    path = MODELS / name
    if not path.is_file():
        raise FileNotFoundError(f"Skin Tools needs {path}; see MODELS.md or set DNB_MODELS")
    return path
