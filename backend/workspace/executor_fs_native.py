"""Source no-follow primitives; path admission and OS isolation happen outside.

POSIX operations stay anchored to directory descriptors. Windows uses the
same NtCreateFile OBJ_DONT_REPARSE boundary as Source no_follow/windows.rs.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat


@contextmanager
def parent_fd(path: Path):
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise OSError(errno.EINVAL, "no-follow filesystem operations require a normalized absolute path", str(path))
    if len(path.parts) == 1:
        raise OSError(errno.EINVAL, "path must name an entry", str(path))
    flags = getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY)) | os.O_DIRECTORY | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, flags | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def _nt_open(path: Path, access: int, disposition: int, options: int = 0) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ctypes.POINTER(UnicodeString)), ("Attributes", wintypes.ULONG),
                    ("SecurityDescriptor", wintypes.LPVOID), ("SecurityQualityOfService", wintypes.LPVOID)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class SecurityQualityOfService(ctypes.Structure):
        _fields_ = [("Length", wintypes.DWORD), ("ImpersonationLevel", ctypes.c_int),
                    ("ContextTrackingMode", ctypes.c_ubyte), ("EffectiveOnly", ctypes.c_ubyte)]

    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\??\\UNC\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = "\\??\\" + value[4:]
    elif value.startswith("\\\\"):
        value = "\\??\\UNC\\" + value[2:]
    elif path.is_absolute() and len(path.drive) == 2:
        value = "\\??\\" + value
    else:
        raise OSError(errno.EINVAL, "no-follow filesystem operations require a local disk or UNC path", str(path))
    encoded_length = len(value.encode("utf-16-le", errors="surrogatepass"))
    if encoded_length + 2 > 65535:
        raise OSError(errno.ENAMETOOLONG, "filesystem path is too long", str(path))
    buffer = ctypes.create_unicode_buffer(value)
    name = UnicodeString(encoded_length, encoded_length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    qos = SecurityQualityOfService(ctypes.sizeof(SecurityQualityOfService), 1, 0, 1)
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), None, ctypes.pointer(name),
                                  0x40 | 0x1000, None, ctypes.cast(ctypes.pointer(qos), wintypes.LPVOID))
    status_block, handle = IoStatusBlock(), wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    create = ntdll.NtCreateFile
    create.restype = wintypes.LONG
    create.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, ctypes.POINTER(ObjectAttributes),
                       ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
                       wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    status_code = create(ctypes.byref(handle), access | 0x00100000, ctypes.byref(attributes),
                         ctypes.byref(status_block), None, 0x80, 7, disposition, options | 0x20, None, 0)
    if status_code < 0:
        if status_code & 0xFFFFFFFF == 0xC000050B:
            raise OSError(errno.EINVAL, "path contains a reparse point", str(path))
        convert = ntdll.RtlNtStatusToDosError
        convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
        raise ctypes.WinError(convert(status_code))
    return msvcrt.open_osfhandle(handle.value, os.O_BINARY | (os.O_WRONLY if access & 0x2 else os.O_RDONLY))


def open_regular(path: Path, mode: str, *, follow: bool = True) -> int:
    if mode not in {"read", "replace"}:
        raise OSError(errno.EINVAL, "file mode must be read or replace", str(path))
    if os.name == "nt":
        if not follow:
            access = 0x120089 if mode == "read" else 0x80 | 0x2
            descriptor = _nt_open(path, access, 1 if mode == "read" else 3, 0x40)
        else:
            import ctypes
            from ctypes import wintypes
            import msvcrt

            create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
            create.restype = wintypes.HANDLE
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            handle = create(str(path), 0x80000000 if mode == "read" else 0x40000000, 7, None,
                            3 if mode == "read" else 2, 0x00100000 | 0x00010000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == "read" else os.O_WRONLY))
    else:
        flags = (os.O_RDONLY if mode == "read" else os.O_WRONLY | os.O_CREAT) | os.O_NONBLOCK | os.O_CLOEXEC
        if follow:
            descriptor = os.open(path, flags | (os.O_TRUNC if mode == "replace" else 0), 0o666)
        else:
            with parent_fd(path) as (parent, leaf):
                descriptor = os.open(leaf, flags | os.O_NOFOLLOW, 0o666, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "path is not a regular file", str(path))
        if mode == "replace" and not follow:
            os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def nofollow_stat(path: Path):
    if os.name == "nt":
        descriptor = _nt_open(path, 0x80, 1)
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)
    if path == Path(path.anchor):
        result = path.stat()
    else:
        with parent_fd(path) as (parent, leaf):
            result = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(result.st_mode):
        raise OSError(errno.EINVAL, "path contains a symbolic link", str(path))
    return result


def nofollow_mkdir(path: Path, recursive: bool) -> None:
    if os.name == "nt":
        targets = list(reversed(path.parents))[1:] + [path] if recursive else [path]
        for target in targets:
            descriptor = _nt_open(target, 0x80, 3 if recursive else 2, 1)
            os.close(descriptor)
        return
    if path == Path(path.anchor):
        if not recursive:
            raise FileExistsError(errno.EEXIST, "directory already exists", str(path))
        return
    if not recursive:
        with parent_fd(path) as (parent, leaf):
            os.mkdir(leaf, dir_fd=parent)
        return
    flags = getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY)) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
    finally:
        os.close(descriptor)


def nofollow_remove(path: Path, *, recursive: bool, force: bool) -> None:
    if recursive:
        raise OSError(errno.ENOTSUP, "recursive no-follow removal is unsupported", str(path))
    try:
        metadata = nofollow_stat(path)
        if os.name == "nt":
            import ctypes
            import msvcrt

            descriptor = _nt_open(path, 0x10000, 1, 1 if stat.S_ISDIR(metadata.st_mode) else 0x40)
            try:
                disposition = ctypes.c_ubyte(1)
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
                if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(descriptor), 4, ctypes.byref(disposition), 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                os.close(descriptor)
        else:
            with parent_fd(path) as (parent, leaf):
                (os.rmdir if stat.S_ISDIR(metadata.st_mode) else os.unlink)(leaf, dir_fd=parent)
    except FileNotFoundError:
        if not force:
            raise


def remove_file(path: Path) -> None:
    if os.name != "nt":
        path.unlink()
        return
    import ctypes
    from ctypes import wintypes

    # Source DirectFileSystem calls std::fs::remove_file / DeleteFileW even
    # for directory reparse points. Python unlink instead removes junctions
    # through RemoveDirectoryW, which would turn Source's real error into a
    # successful mutation.
    function = ctypes.WinDLL("kernel32", use_last_error=True).DeleteFileW
    function.argtypes, function.restype = [wintypes.LPCWSTR], wintypes.BOOL
    if not function(str(path)):
        raise ctypes.WinError(ctypes.get_last_error())
