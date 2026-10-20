"""Count physical .zst bytes and existing native-compressed file allocations."""
from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_compressed_file_size = _kernel32.GetCompressedFileSizeW
    _get_compressed_file_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _get_compressed_file_size.restype = wintypes.DWORD


def file_disk_bytes(path: Path) -> int:
    if os.name != "nt":
        return path.stat().st_size
    # The W API accepts Unicode and extended paths, including UNC worktrees.
    native_path = os.path.abspath(path)
    if not native_path.startswith("\\\\?\\"):
        native_path = "\\\\?\\UNC\\" + native_path[2:] if native_path.startswith("\\\\") else "\\\\?\\" + native_path
    high = wintypes.DWORD()
    ctypes.set_last_error(0)
    low = _get_compressed_file_size(native_path, ctypes.byref(high))
    error_code = ctypes.get_last_error()
    if low == 0xFFFFFFFF and error_code:
        error = ctypes.WinError(error_code)
        error.filename = str(path)
        raise error
    return high.value << 32 | low
