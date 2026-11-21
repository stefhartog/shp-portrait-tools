"""Import this before creating any onnxruntime session: makes the pip-installed CUDA 13 / cuDNN libraries loadable."""
import os
import sys
from pathlib import Path

_dll_dirs = []


def add_windows_dll_directories():
    """Windows does not search PATH for dependent DLLs of onnxruntime's CUDA provider, so register the
    pip-installed nvidia-* wheel bin directories explicitly. Returns the directories added."""
    if sys.platform != "win32":
        return []
    added = []
    for entry in sys.path:
        nvidia = Path(entry) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bindir in sorted(nvidia.glob("*/bin")) + sorted(nvidia.glob("*/bin/x86_64")):
            if bindir.is_dir() and any(bindir.glob("*.dll")) and str(bindir) not in map(str, _dll_dirs):
                try:
                    _dll_dirs.append(os.add_dll_directory(str(bindir)))
                    added.append(bindir)
                except OSError:
                    pass
    return added


add_windows_dll_directories()

import onnxruntime as ort  # noqa: E402

try:
    ort.preload_dlls()
except Exception as e:  # CPU fallback still works
    print(f"[dnb_gpu] CUDA preload failed, running on CPU: {e}")

PROVIDERS = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]


def device():
    """rtmlib device name matching the providers that can actually be used."""
    return "cuda" if "CUDAExecutionProvider" in PROVIDERS else "cpu"
