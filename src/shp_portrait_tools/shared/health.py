"""Loopback server identity for the desktop launcher; no external service calls."""

import os
import sys
from pathlib import Path


def identity(module, data, models=None):
    result = {
        "application": "shp-photocraft-tools",
        "module": module,
        "package": str(Path(__file__).resolve().parent.parent),
        "python": str(Path(sys.executable).absolute()),
        "data": str(Path(data).resolve()),
        "pid": os.getpid(),
        "instance": os.environ.get("SHP_SERVER_INSTANCE"),
        "supervisor": os.environ.get("SHP_SUPERVISOR_INSTANCE"),
        "owner_protocol": 1 if os.environ.get("SHP_SUPERVISOR_TOKEN") else 0,
    }
    if models is not None:
        result["models"] = str(Path(models).resolve())
    return result
