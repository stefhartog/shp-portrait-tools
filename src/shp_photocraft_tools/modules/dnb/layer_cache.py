"""Bounded in-process cache for exact full-resolution Send components."""

from collections import OrderedDict
import ctypes
import os
import threading

import numpy as np


LIMIT = 2 * 1024**3
RESERVE = 4 * 1024**3


def available_memory():
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended_virtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.available_physical
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


class LayerCache:
    def __init__(self, limit=LIMIT, memory=available_memory):
        self.limit, self.memory = limit, memory
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.bytes = 0

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.bytes = 0

    def _room(self, reserve):
        available = self.memory()
        if available is None or available < reserve:
            self.entries.clear()
            self.bytes = 0
            return False
        return True

    def get(self, key, reserve):
        with self.lock:
            if not self._room(reserve):
                return None
            entry = self.entries.get(key)
            if entry is None:
                return None
            self.entries.move_to_end(key)
            return entry[0]

    def contains_all(self, keys):
        with self.lock:
            return bool(keys) and all(key in self.entries for key in keys)

    def put(self, key, arrays, reserve):
        if (not arrays or any(not isinstance(a, np.ndarray) or not a.flags.owndata for a in arrays)
                or len({id(a) for a in arrays}) != len(arrays)):
            return False
        size = sum(a.nbytes for a in arrays)
        with self.lock:
            if size > self.limit or not self._room(reserve):
                return False
            old = self.entries.pop(key, None)
            if old:
                self.bytes -= old[1]
            while self.bytes + size > self.limit:
                _, (_, removed) = self.entries.popitem(last=False)
                self.bytes -= removed
            for array in arrays:
                array.flags.writeable = False
            self.entries[key] = (tuple(arrays), size)
            self.bytes += size
            return True
