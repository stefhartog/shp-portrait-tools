"""Writable D&B data and external model locations, independent of the installed package."""

import os
from pathlib import Path

from shp_portrait_tools.shared.platform_paths import env, module_data

DATA = module_data("SKIN_DATA", "skin", legacy_env="DNB_DATA")
MODELS = Path(env("SKIN_MODELS", "DNB_MODELS", DATA / "models")).expanduser()


def model_path(name):
    path = MODELS / name
    if not path.is_file():
        raise FileNotFoundError(f"Skin Tools needs {path}; see MODELS.md or set SKIN_MODELS")
    return path
