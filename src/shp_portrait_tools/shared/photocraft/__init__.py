"""Shared PhotoCraft connection, authentication and merged-document fetch."""

from .bridge import Bridge, Client, ControlError, detect_config

__all__ = ["Bridge", "Client", "ControlError", "detect_config"]
