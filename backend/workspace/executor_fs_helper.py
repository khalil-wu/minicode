"""Filesystem helper run inside the captured platform sandbox.

This file deliberately imports only the standard library and its native
primitive companion. Requests contain native paths already admitted by the
receiving host; the helper never receives a second permission profile.
"""
from __future__ import annotations

import base64
from collections import deque
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).parent))
    from executor_fs_native import nofollow_mkdir, nofollow_remove, nofollow_stat, open_regular, remove_file
    from executor_paths import ExecutorPath
else:
    from .executor_fs_native import nofollow_mkdir, nofollow_remove, nofollow_stat, open_regular, remove_file
    from .executor_paths import ExecutorPath


FILE_CHUNK_SIZE = 1024 * 1024
MAX_READ_FILE_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = MAX_READ_FILE_BYTES * 4 // 3 + 128 * 1024


def native_uri(path: Path) -> str:
    convention = "windows" if os.name == "nt" else "posix"
    native = str(path) if os.name == "nt" else os.fsencode(path)
    return ExecutorPath.from_native(native, convention=convention, platform_os=sys.platform).uri


def _is_link(value) -> bool:
    return stat.S_ISLNK(value.st_mode) or (os.name == "nt"
        and value.st_reparse_tag in {0xA0000003, 0xA000000C})


def _cancelled(cancelled) -> None:
    if cancelled is not None and cancelled.is_set():
        raise OSError(errno.ECANCELED, "filesystem operation cancelled")


def metadata(value, *, is_symlink: bool = False) -> dict:
    created_ms = (value.st_ctime_ns // 1_000_000 if os.name == "nt"
                  else int(getattr(value, "st_birthtime", 0) * 1000))
    return {"isDirectory": stat.S_ISDIR(value.st_mode), "isFile": stat.S_ISREG(value.st_mode),
            "isSymlink": is_symlink, "size": value.st_size,
            "createdAtMs": max(0, created_ms), "modifiedAtMs": max(0, value.st_mtime_ns // 1_000_000)}


def _read_file(path: Path, follow: bool, cancelled) -> bytes:
    descriptor = open_regular(path, "read", follow=follow)
    try:
        if os.fstat(descriptor).st_size > MAX_READ_FILE_BYTES:
            raise OSError(errno.EINVAL, f"file is too large to read: limit is {MAX_READ_FILE_BYTES} bytes", str(path))
        result = bytearray()
        while True:
            _cancelled(cancelled)
            block = os.read(descriptor, min(FILE_CHUNK_SIZE, MAX_READ_FILE_BYTES + 1 - len(result)))
            if not block:
                return bytes(result)
            result.extend(block)
            if len(result) > MAX_READ_FILE_BYTES:
                raise OSError(errno.EINVAL, f"file is too large to read: limit is {MAX_READ_FILE_BYTES} bytes", str(path))
    finally:
        os.close(descriptor)


def _copy(source: Path, destination: Path, recursive: bool, cancelled) -> None:
    _cancelled(cancelled)
    value = source.lstat()
    if _is_link(value):
        directory_link = bool(value.st_file_attributes & 0x10) if os.name == "nt" else False
        os.symlink(os.readlink(source), destination, target_is_directory=directory_link)
    elif stat.S_ISDIR(value.st_mode):
        if not recursive:
            raise OSError(errno.EINVAL, "fs/copy requires recursive: true when sourcePath is a directory")
        if destination.resolve().is_relative_to(source.resolve(strict=True)):
            raise OSError(errno.EINVAL, "fs/copy cannot copy a directory to itself or one of its descendants")
        destination.mkdir(parents=True, exist_ok=True)
        for entry in source.iterdir():
            _copy(entry, destination / entry.name, True, cancelled)
    elif stat.S_ISREG(value.st_mode):
        shutil.copyfile(source, destination)
        shutil.copymode(source, destination)
    else:
        raise OSError(errno.EINVAL, "fs/copy only supports regular files, directories, and symlinks")


def _directory(path: Path, cancelled) -> list[dict]:
    result = []
    with os.scandir(path) as entries:
        for entry in entries:
            _cancelled(cancelled)
            try:
                link = entry.is_symlink() or os.name == "nt" and _is_link(entry.stat(follow_symlinks=False))
                if link:
                    value = entry.stat(follow_symlinks=True)
                    is_directory, is_file = stat.S_ISDIR(value.st_mode), stat.S_ISREG(value.st_mode)
                else:
                    is_directory, is_file = entry.is_dir(follow_symlinks=False), entry.is_file(follow_symlinks=False)
            except OSError:
                # Source omits broken/inaccessible directory entries.
                continue
            name = (entry.name.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
                    if os.name == "nt" else os.fsencode(entry.name).decode("utf-8", errors="replace"))
            result.append({"fileName": name, "isDirectory": is_directory, "isFile": is_file})
    return result


def _walk(root: Path, options: dict, cancelled) -> dict:
    depth_limit, directory_limit, entry_limit = (options[key] for key in ("maxDepth", "maxDirectories", "maxEntries"))
    if directory_limit <= 0 or entry_limit <= 0 or depth_limit < 0:
        raise OSError(errno.EINVAL, "filesystem walk limits must be greater than zero")
    if depth_limit > 64 or directory_limit > 10_000 or entry_limit > 50_000:
        raise OSError(errno.EINVAL, "filesystem walk limits exceed maximums: depth=64, directories=10000, entries=50000")
    follow = options["followDirectorySymlinks"]
    hidden = options.get("pruneHiddenDirectories", False)
    outcome = {"entries": [], "errors": [], "truncated": False}
    _cancelled(cancelled)
    value, symlink = root.stat(), _is_link(root.lstat())
    if not stat.S_ISDIR(value.st_mode) or symlink and not follow:
        return outcome
    queue = deque([(root, 0)])
    visited = {root.resolve(strict=True) if follow else root}
    directory_count, entry_count, response_bytes = 1, 0, 0

    def append(collection: str, path: Path, **extra) -> bool:
        nonlocal response_bytes
        uri = native_uri(path)
        item_bytes = len(uri.encode()) + len(extra.get("message", "").encode()) + 64
        if response_bytes + item_bytes > 4 * 1024 * 1024:
            outcome["truncated"] = True
            return False
        response_bytes += item_bytes
        outcome[collection].append({"path": uri, **extra})
        return True

    while queue:
        _cancelled(cancelled)
        directory, depth = queue.popleft()
        try:
            names = sorted(entry["fileName"] for entry in _directory(directory, cancelled))
        except OSError as error:
            if not append("errors", directory, message=str(error)):
                return outcome
            continue
        for name in names:
            _cancelled(cancelled)
            if entry_count == entry_limit:
                outcome["truncated"] = True
                return outcome
            entry_count += 1
            path = directory / name
            try:
                value, symlink = path.stat(), _is_link(path.lstat())
            except OSError as error:
                if not append("errors", path, message=str(error)):
                    return outcome
                continue
            is_directory = stat.S_ISDIR(value.st_mode)
            if symlink and (not follow or not is_directory):
                continue
            if not is_directory and not stat.S_ISREG(value.st_mode):
                continue
            if not append("entries", path, kind="directory" if is_directory else "file",
                    size=value.st_size, modifiedAtMs=max(0, value.st_mtime_ns // 1_000_000)):
                return outcome
            if is_directory and depth < depth_limit and not (hidden and name.startswith(".")):
                try:
                    identity = path.resolve(strict=True) if follow else path
                except OSError as error:
                    if not append("errors", path, message=str(error)):
                        return outcome
                    continue
                if identity in visited:
                    continue
                visited.add(identity)
                if directory_count == directory_limit:
                    outcome["truncated"] = True
                else:
                    directory_count += 1
                    queue.append((path, depth + 1))
    return outcome


def execute(request: dict, handles: dict, cancelled=None):
    operation = request["operation"]
    _cancelled(cancelled)
    path = Path(request["path"]) if "path" in request else None
    follow = request.get("followSymlinks", True)
    if operation == "readFile":
        return {"dataBase64": base64.b64encode(_read_file(path, follow, cancelled)).decode("ascii")}
    if operation == "writeFile":
        contents = base64.b64decode(request["dataBase64"], validate=True)
        if follow:
            path.write_bytes(contents)
        else:
            with os.fdopen(open_regular(path, "replace", follow=False), "wb") as target:
                target.write(contents)
        return {}
    if operation == "createDirectory":
        if follow:
            path.mkdir(parents=request["recursive"], exist_ok=request["recursive"])
        else:
            nofollow_mkdir(path, request["recursive"])
        return {}
    if operation == "getMetadata":
        value = path.lstat() if follow else nofollow_stat(path)
        is_symlink = _is_link(value)
        return metadata(path.stat() if is_symlink else value, is_symlink=is_symlink)
    if operation == "canonicalize":
        return {"path": native_uri(path.resolve(strict=True))}
    if operation == "readDirectory":
        return {"entries": _directory(path, cancelled)}
    if operation == "walk":
        return _walk(path, request["options"], cancelled)
    if operation == "remove":
        if not follow:
            nofollow_remove(path, recursive=request["recursive"], force=request["force"])
        else:
            try:
                value = path.lstat()
                if stat.S_ISDIR(value.st_mode) and not _is_link(value):
                    shutil.rmtree(path) if request["recursive"] else path.rmdir()
                else:
                    remove_file(path)
            except FileNotFoundError:
                if not request["force"]:
                    raise
        return {}
    if operation == "copy":
        _copy(Path(request["sourcePath"]), Path(request["destinationPath"]), request["recursive"], cancelled)
        return {}
    if operation == "open":
        handles[request["handleId"]] = (open_regular(path, request["mode"]), request["mode"])
        return {"handleId": request["handleId"]}
    if operation == "close":
        entry = handles.pop(request["handleId"], None)
        if entry is not None:
            os.close(entry[0])
        return {}
    if operation in {"readBlock", "writeBlock"}:
        handle_id = request["handleId"]
        offset = request["offset"]
        if not 0 <= offset <= (1 << 64) - 1:
            raise OSError(errno.EINVAL, "file block offset must be an unsigned 64-bit integer")
        if operation == "readBlock":
            length = request["len"]
            if not 0 < length <= FILE_CHUNK_SIZE:
                raise OSError(errno.EINVAL, "file read block length must be between 1 and 1048576")
            if handle_id not in handles:
                raise OSError(errno.ENOENT, f"unknown file handle `{handle_id}`")
            descriptor, _ = handles[handle_id]
            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                chunk = bytearray()
                while len(chunk) < length:
                    _cancelled(cancelled)
                    block = os.read(descriptor, length - len(chunk))
                    if not block:
                        break
                    chunk.extend(block)
                return {"chunk": base64.b64encode(chunk).decode("ascii"), "eof": len(chunk) < length}
            except OSError:
                handles.pop(handle_id)
                os.close(descriptor)
                raise
            except OverflowError as error:
                handles.pop(handle_id)
                os.close(descriptor)
                raise OSError(errno.EINVAL, str(error)) from error
        chunk = base64.b64decode(request["chunk"], validate=True)
        if not 0 < len(chunk) <= FILE_CHUNK_SIZE:
            raise OSError(errno.EINVAL, "file write block length must be between 1 and 1048576")
        if offset + len(chunk) > (1 << 63) - 1:
            raise OSError(errno.EINVAL, "file write range exceeds the signed 64-bit file offset limit")
        if handle_id not in handles:
            raise OSError(errno.ENOENT, f"unknown file handle `{handle_id}`")
        descriptor, _ = handles[handle_id]
        os.lseek(descriptor, offset, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "wb", closefd=True) as target:
            target.write(chunk)
        return {}
    raise ValueError(f"unknown filesystem operation: {operation}")


def main() -> None:
    handles = {}
    try:
        while header := sys.stdin.buffer.read(4):
            size, = struct.unpack("!I", header)
            request = json.loads(sys.stdin.buffer.read(size))
            try:
                response = {"result": execute(request, handles)}
            except OSError as error:
                response = {"error": {"errno": error.errno, "message": str(error), "filename": error.filename,
                                      "winerror": getattr(error, "winerror", None)}}
            except ValueError as error:
                response = {"error": {"errno": errno.EINVAL, "message": str(error), "filename": None, "winerror": None}}
            except OverflowError as error:
                response = {"error": {"errno": errno.EINVAL, "message": str(error), "filename": None, "winerror": None}}
            data = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
            sys.stdout.buffer.write(struct.pack("!I", len(data)) + data)
            sys.stdout.buffer.flush()
    finally:
        for descriptor, _ in handles.values():
            os.close(descriptor)


if __name__ == "__main__":
    main()
