"""Local executor filesystem using the captured Source sandbox context.

Restricted operations execute inside a real SandboxRunner helper. Streamed
files keep that original helper and descriptor until close; no controller
reopen or registry lookup occurs between blocks.
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import errno
import json
from pathlib import Path
import struct
import sys
import sysconfig
import threading
from uuid import uuid4

from backend.environments.errors import ExecutorClosed, ExecutorProtocolError
from backend.environments.filesystem import FILE_CHUNK_SIZE
from backend.sandbox import SandboxRunner
from backend.sandbox.policy import (
    FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, NetworkSandboxPolicy,
)
from backend.workspace.executor_local_policy import local_path, local_sandbox_policy, requires_filesystem_sandbox

from .executor_fs_helper import MAX_RESPONSE_BYTES, execute


_HELPER = Path(__file__).with_name("executor_fs_helper.py")
_NATIVE_HELPER = Path(__file__).with_name("executor_fs_native.py")
_URI_HELPER = Path(__file__).with_name("executor_paths.py")
_HELPER_ENV = frozenset({"PATH", "TMPDIR", "TMP", "TEMP"})


def _helper_policy(sandbox: dict):
    policy = local_sandbox_policy(sandbox)
    profile = policy.permission_profile
    paths = {Path(sys.executable), Path(sys.prefix), Path(sysconfig.get_path("stdlib")), _HELPER, _NATIVE_HELPER, _URI_HELPER}
    filesystem = profile.file_system
    runtime_entries = tuple(FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.READ) for path in paths)
    profile = replace(profile, network=NetworkSandboxPolicy.RESTRICTED,
        file_system=FileSystemSandboxPolicy.restricted((*filesystem.entries, *runtime_entries),
            glob_scan_max_depth=filesystem.glob_scan_max_depth))
    return replace(policy, permission_profile=profile)


class _SandboxHelper:
    def __init__(self, owner, sandbox: dict):
        self.owner = owner
        self.sandbox = deepcopy(sandbox)
        self.policy = _helper_policy(sandbox)
        self.runner = SandboxRunner(self.policy, env_filter=lambda env: {
            key: value for key, value in env.items() if key.upper() in _HELPER_ENV})
        self.process = None
        self.lock = asyncio.Lock()
        self.stderr_task = None
        self.stderr_tail = bytearray()
        self.close_task = None
        self.aborted = False
        self.owner.helpers.add(self)

    async def start(self) -> None:
        cwd = self.policy.policy_cwd
        # Source helpers start at the drive/share/filesystem root so a removed
        # policy cwd does not prevent a request with absolute paths.
        self.process = await self.runner.spawn_interactive(
            [sys.executable, "-I", "-S", str(_HELPER)], cwd=Path(cwd.anchor))
        self.stderr_task = asyncio.create_task(self._stderr(), name="executor-fs-helper-stderr")

    async def _stderr(self) -> None:
        while chunk := await self.process.stderr.read(4096):
            self.stderr_tail.extend(chunk)
            del self.stderr_tail[:-4096]

    async def request(self, request: dict):
        if self.close_task is not None:
            raise ExecutorClosed("local filesystem helper is closed")
        async with self.lock:
            encoded = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
            try:
                self.process.stdin.write(struct.pack("!I", len(encoded)) + encoded)
                await self.process.stdin.drain()
                size, = struct.unpack("!I", await self.process.stdout.readexactly(4))
                if size > MAX_RESPONSE_BYTES:
                    raise ExecutorProtocolError("local filesystem helper exceeded the Source read response ceiling")
                response = json.loads(await self.process.stdout.readexactly(size))
            except asyncio.IncompleteReadError as error:
                await self.process.wait()
                await self.stderr_task
                raise ExecutorProtocolError(f"local filesystem helper exited with code {self.process.returncode}: "
                    f"{self.stderr_tail.decode('utf-8', errors='replace')}") from error
            except asyncio.CancelledError:
                await self.aclose(abort=True)
                raise
            if "error" in response:
                error = response["error"]
                raise OSError(error["errno"], error["message"], error["filename"], error["winerror"])
            return response["result"]

    async def _close(self):
        try:
            if self.process is not None:
                self.process.stdin.close()
                if self.aborted:
                    if not await self.runner.terminate(self.process):
                        raise RuntimeError("local filesystem helper process tree did not exit")
                else:
                    try:
                        async with asyncio.timeout(2):
                            await self.process.wait()
                    except TimeoutError:
                        if not await self.runner.terminate(self.process):
                            raise RuntimeError("local filesystem helper process tree did not exit")
                if self.stderr_task is not None:
                    await self.stderr_task
                # Cancellation may abandon a frame while the child still has
                # buffered stdout. Drain that exact pipe before its event loop
                # is released, including after an observed process exit.
                while await self.process.stdout.read(64 * 1024):
                    pass
                try:
                    await self.process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    if not self.aborted:
                        raise
                if self.process.returncode != 0 and not self.aborted:
                    raise ExecutorProtocolError(f"local filesystem helper exited with code {self.process.returncode}: "
                        f"{self.stderr_tail.decode('utf-8', errors='replace')}")
        finally:
            cleaned = await self.runner.cleanup()
            if not cleaned:
                raise RuntimeError("local filesystem sandbox cleanup was not confirmed")
            self.owner.helpers.discard(self)

    async def aclose(self, *, abort: bool = False):
        self.aborted = self.aborted or abort
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="local-fs-helper-close")
        return await asyncio.shield(self.close_task)


class LocalExecutorFile:
    def __init__(self, owner, handle_id: str, sandbox: dict | None, helper: _SandboxHelper | None):
        self.owner, self.handle_id = owner, handle_id
        self.sandbox, self.helper = deepcopy(sandbox), helper
        self.handles = {}
        self.lock = asyncio.Lock()
        self.open_task = None
        self.opened = False
        self.closed = False
        self.close_task = None

    async def _request(self, operation: str, **options):
        if self.closed:
            raise ExecutorClosed("local executor file is closed")
        request = {"operation": operation, "handleId": self.handle_id, **options}
        async with self.lock:
            return (await self.helper.request(request) if self.helper is not None
                    else await self.owner._direct(request, self.handles))

    async def read_block(self, offset: int, length: int = FILE_CHUNK_SIZE) -> tuple[bytes, bool]:
        response = await self._request("readBlock", offset=offset, len=length)
        return base64.b64decode(response["chunk"], validate=True), response["eof"]

    async def write_block(self, offset: int, chunk: bytes) -> None:
        await self._request("writeBlock", offset=offset, chunk=base64.b64encode(chunk).decode("ascii"))

    async def _close(self):
        try:
            if self.open_task is not None:
                await asyncio.gather(self.open_task, return_exceptions=True)
            if self.helper is None:
                if self.handle_id in self.handles:
                    await self._request("close")
            elif self.opened and not self.helper.aborted:
                await self._request("close")
            if self.helper is not None:
                await self.helper.aclose()
            self.closed = True
        finally:
            if self.closed:
                self.owner.files.discard(self)
        return {"handle_id": self.handle_id, "closed": self.closed}

    async def aclose(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="local-executor-file-close")
        return await asyncio.shield(self.close_task)

    async def chunks(self):
        offset = 0
        try:
            while True:
                chunk, eof = await self.read_block(offset)
                if chunk:
                    yield chunk
                if eof:
                    return
                offset += len(chunk)
        finally:
            await self.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()


class LocalExecutorFileSystem:
    def __init__(self):
        self.helpers: set[_SandboxHelper] = set()
        self.files: set[LocalExecutorFile] = set()
        self.operations: dict[asyncio.Task, threading.Event] = {}
        self.closed = False
        self.close_task = None

    async def _direct(self, request: dict, handles: dict | None = None):
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(execute, request, handles if handles is not None else {}, cancelled))
        self.operations[task] = cancelled
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self.operations.pop(task, None)

    def _prepare(self, operation: str, sandbox: dict | None, **options) -> tuple[dict, bool]:
        if self.closed:
            raise ExecutorClosed("local executor filesystem is closed")
        request = {"operation": operation, **deepcopy(options)}
        for key in ("path", "sourcePath", "destinationPath"):
            if key in request:
                request[key] = str(local_path(request[key]))
        if sandbox is not None:
            local_sandbox_policy(sandbox)
        write = operation in {"writeFile", "createDirectory", "remove", "copy"} or operation == "open" and options["mode"] == "replace"
        return request, requires_filesystem_sandbox(sandbox, write=write)

    async def _request(self, operation: str, sandbox: dict | None, **options):
        request, sandboxed = self._prepare(operation, sandbox, **options)
        if not sandboxed:
            return await self._direct(request)
        helper = _SandboxHelper(self, sandbox)
        try:
            await helper.start()
            return await helper.request(request)
        finally:
            await helper.aclose()

    async def read_file(self, path: str, *, sandbox: dict | None, follow_symlinks: bool = True) -> bytes:
        result = await self._request("readFile", sandbox, path=path, followSymlinks=follow_symlinks)
        return base64.b64decode(result["dataBase64"], validate=True)

    async def write_file(self, path: str, contents: bytes, *, sandbox: dict | None, follow_symlinks: bool = True) -> None:
        await self._request("writeFile", sandbox, path=path, dataBase64=base64.b64encode(contents).decode("ascii"), followSymlinks=follow_symlinks)

    async def create_directory(self, path: str, *, sandbox: dict | None, recursive: bool = False, follow_symlinks: bool = True) -> None:
        await self._request("createDirectory", sandbox, path=path, recursive=recursive, followSymlinks=follow_symlinks)

    async def get_metadata(self, path: str, *, sandbox: dict | None, follow_symlinks: bool = True) -> dict:
        return await self._request("getMetadata", sandbox, path=path, followSymlinks=follow_symlinks)

    async def canonicalize(self, path: str, *, sandbox: dict | None) -> str:
        return (await self._request("canonicalize", sandbox, path=path))["path"]

    async def read_directory(self, path: str, *, sandbox: dict | None) -> list[dict]:
        return (await self._request("readDirectory", sandbox, path=path))["entries"]

    async def walk(self, path: str, options: dict, *, sandbox: dict | None) -> dict:
        return await self._request("walk", sandbox, path=path, options=options)

    async def remove(self, path: str, *, sandbox: dict | None, recursive: bool = False, force: bool = False, follow_symlinks: bool = True) -> None:
        await self._request("remove", sandbox, path=path, recursive=recursive, force=force, followSymlinks=follow_symlinks)

    async def copy(self, source: str, destination: str, *, sandbox: dict | None, recursive: bool = False) -> None:
        await self._request("copy", sandbox, sourcePath=source, destinationPath=destination, recursive=recursive)

    async def open(self, path: str, *, sandbox: dict | None, mode: str = "read") -> LocalExecutorFile:
        if len(self.files) >= 128:
            raise OSError(errno.EINVAL, "at most 128 file handles may be open per connection")
        handle_id = uuid4().hex
        request, sandboxed = self._prepare("open", sandbox, path=path, handleId=handle_id, mode=mode)
        helper = _SandboxHelper(self, sandbox) if sandboxed else None
        handle = LocalExecutorFile(self, handle_id, sandbox, helper)
        self.files.add(handle)

        async def open_file():
            if helper is not None:
                await helper.start()
            await handle._request("open", path=request["path"], mode=mode)
            handle.opened = True

        handle.open_task = asyncio.create_task(open_file(), name="local-executor-file-open")
        try:
            await asyncio.shield(handle.open_task)
            return handle
        except BaseException:
            handle.open_task.cancel()
            await handle.aclose()
            raise

    async def _close(self):
        self.closed = True
        for cancelled in self.operations.values():
            cancelled.set()
        for file in self.files:
            if file.open_task is not None and not file.open_task.done():
                file.open_task.cancel()
        await asyncio.gather(*tuple(self.operations), return_exceptions=True)
        results = await asyncio.gather(*(file.aclose() for file in tuple(self.files)), return_exceptions=True)
        results += await asyncio.gather(*(helper.aclose() for helper in tuple(self.helpers)), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("local executor filesystem close was not confirmed", failures)

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="local-executor-filesystem-close")
        return await asyncio.shield(self.close_task)
