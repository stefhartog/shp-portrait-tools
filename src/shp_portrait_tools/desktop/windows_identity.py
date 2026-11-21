"""One taskbar identity for the Windows desktop host and its launcher shortcuts."""

import ctypes
from pathlib import Path
from uuid import UUID

APP_ID = "SHP.PortraitTools"


def set_process_app_id():
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
    shell.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    _check(shell.SetCurrentProcessExplicitAppUserModelID(APP_ID))


def _check(result):
    if result < 0:
        raise OSError(f"Windows app identity operation failed (HRESULT 0x{result & 0xffffffff:08x})")


def shortcut_app_id(path, app_id=None):
    """Read, or set only System.AppUserModel.ID on a .lnk, preserving its other properties."""
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_byte * 16)]

        def __init__(self, value):
            super().__init__()
            self.data[:] = UUID(value).bytes_le

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class Value(ctypes.Union):
        _fields_ = [("pointer", ctypes.c_void_p), ("storage", ctypes.c_void_p * 2)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wintypes.WORD), ("reserved", wintypes.WORD * 3), ("value", Value)]

    def call(pointer, index, arguments, *values):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(table[index])
        result = method(pointer, *values)
        _check(result)
        return result

    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE: the caller already initialized COM as MTA.
        _check(initialized)
    store, persist = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole.CoCreateInstance.restype = ctypes.c_long
        _check(ole.CoCreateInstance(
            ctypes.byref(GUID("00021401-0000-0000-c000-000000000046")), None, 1,
            ctypes.byref(GUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")), ctypes.byref(store)))
        call(store, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
             ctypes.byref(GUID("0000010b-0000-0000-c000-000000000046")), ctypes.byref(persist))
        call(persist, 5, [ctypes.c_wchar_p, wintypes.DWORD], str(Path(path).resolve()), 2 if app_id is not None else 0)
        key = PROPERTYKEY(GUID("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)
        value = PROPVARIANT()
        if app_id is None:
            call(store, 5, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
                 ctypes.byref(key), ctypes.byref(value))
            try:
                if value.vt == 0:
                    return ""
                if value.vt != 31:  # VT_LPWSTR
                    raise ValueError(f"Unexpected shortcut AppUserModelID type: {value.vt}")
                return ctypes.wstring_at(value.value.pointer)
            finally:
                ole.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
                ole.PropVariantClear.restype = ctypes.c_long
                _check(ole.PropVariantClear(ctypes.byref(value)))
        text = ctypes.create_unicode_buffer(app_id)
        value.vt, value.value.pointer = 31, ctypes.cast(text, ctypes.c_void_p).value
        call(store, 6, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
             ctypes.byref(key), ctypes.byref(value))
        call(store, 7, [])
        call(persist, 6, [ctypes.c_wchar_p, wintypes.BOOL], None, True)
        return app_id
    finally:
        if persist:
            call(persist, 2, [])
        if store:
            call(store, 2, [])
        if initialized >= 0:
            ole.CoUninitialize()
