"""Windows filesystem primitives for the no-overwrite, replacement-safe output code.

Windows cannot open directories as descriptors or address children with ``dir_fd``. A directory is
instead pinned with a handle that omits FILE_SHARE_DELETE, so it cannot be renamed, replaced or
removed (nor, by NTFS rules, can its ancestors be renamed) while the handle is open.
"""

import ctypes
import os
import re
import stat
import sys

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXTENDED_FROM = 240


def validate_name(name):
    """Reject names Windows would alias, truncate or interpret (devices, streams, trailing dot/space)."""
    if not name or _INVALID.search(name) or name[-1] in " .":
        raise ValueError(f"not a safe Windows file name: {name!r}")
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        raise ValueError(f"reserved Windows device name: {name!r}")
    return name


def extended(path):
    """Return a path usable beyond MAX_PATH; short paths are returned unchanged."""
    path = os.fspath(path)
    if len(path) < _EXTENDED_FROM or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def is_reparse_point(info):
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def is_sharing_violation(error):
    """True for ERROR_SHARING_VIOLATION/ERROR_LOCK_VIOLATION: another process holds the file."""
    return getattr(error, "winerror", None) in (32, 33)


def lstat_identity(path):
    info = os.lstat(extended(path))
    return info.st_dev, info.st_ino


if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    # Sharing modes are ignored for handles that only request attribute access, so also ask to list.
    _ACCESS = 0x1 | 0x80
    _SHARE_READ_WRITE = 0x1 | 0x2
    _OPEN_EXISTING = 3
    _BACKUP_SEMANTICS = 0x02000000
    _OPEN_REPARSE_POINT = 0x00200000

    def pin_directory(path, *, follow_links=True):
        """Open a directory so it cannot be renamed or deleted; with follow_links=False pin a link itself."""
        flags = _BACKUP_SEMANTICS | (0 if follow_links else _OPEN_REPARSE_POINT)
        handle = _kernel32.CreateFileW(extended(os.path.abspath(path)), _ACCESS,
                                       _SHARE_READ_WRITE, None, _OPEN_EXISTING, flags, None)
        if handle == _INVALID_HANDLE or handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def close_handle(handle):
        _kernel32.CloseHandle(handle)

    def open_readonly_no_follow(path):
        """Open a file for reading without following a link (a link is opened itself) and without
        blocking an exporter that renames or deletes it; returns a CRT descriptor."""
        import msvcrt
        handle = _kernel32.CreateFileW(extended(os.path.abspath(path)), 0x80000000, 0x1 | 0x2 | 0x4,
                                       None, _OPEN_EXISTING, 0x80 | _OPEN_REPARSE_POINT, None)
        if handle == _INVALID_HANDLE or handle is None:
            code = ctypes.get_last_error()
            raise ctypes.WinError(code)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
else:
    def pin_directory(path, *, follow_links=True):
        raise OSError("directory pinning is Windows-only")

    def close_handle(handle):
        raise OSError("directory pinning is Windows-only")

    def open_readonly_no_follow(path):
        raise OSError("Windows-only")
