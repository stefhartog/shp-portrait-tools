"""Shared styles served by each SHP tool's local HTTP server."""

from importlib.resources import files


def stylesheet() -> bytes:
    """Return the packaged common stylesheet."""
    return files(__package__).joinpath("common.css").read_bytes()


def theme_script() -> bytes:
    """Return shared theme preference and frame synchronization."""
    return files(__package__).joinpath("theme.js").read_bytes()
