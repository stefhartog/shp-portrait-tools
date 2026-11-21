"""Run all migration checks with isolated data/config roots and no live services."""

import os
import runpy
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory(prefix="shp-photocraft-tests-") as tmp:
    os.environ.update(DNB_DATA=tmp + "/dnb", DNB_MODELS=tmp + "/models",
                      SHP_PC_PORT="1", SHP_PC_TOKEN_FILE=tmp + "/token", SHP_PC_EXCHANGE=tmp + "/exchange",
                      SHP_PC_READ_ROOT=tmp)
    for key in ("PORT", "TOKEN_FILE", "EXCHANGE", "READ_ROOT"):
        os.environ.pop(f"DNB_PC_{key}", None)
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
