"""Executor-native file URIs and Source filesystem RPCs."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from urllib.parse import urlsplit
from uuid import uuid4

from .errors import ExecutorClosed, ExecutorDisconnected, ExecutorProtocolError

FILE_CHUNK_SIZE = 1024 * 1024


def decode_bytes(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ExecutorProtocolError(f"exec-server returned invalid base64 {field}: {error}") from error


def file_uri(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme != "file" or not parsed.path.startswith("/") or parsed.query or parsed.fragment:
        raise ExecutorProtocolError("executor filesystem paths and cwd must be absolute file URIs")
    return path


class ExecutorFile:
    def __init__(self, client, handle_id: str, sandbox: dict | None):
        self.client = client
        self.handle_id = handle_id
        self.sandbox = sandbox
        self.open_task: asyncio.Task | None = None
        self.opened = False
        self.closed = False
        self.close_task: asyncio.Task | None = None

    async def read_block(self, offset: int, length: int = FILE_CHUNK_SIZE) -> tuple[bytes, bool]:
        response = await self.client.request("fs/readBlock", {"handleId": self.handle_id, "offset": offset, "len": length})
        chunk = decode_bytes(response["chunk"], "chunk")
        if len(chunk) > length or (not chunk and not response["eof"]):
            raise ExecutorProtocolError("fs/readBlock returned an invalid non-terminal block")
        return chunk, response["eof"]

    async def write_block(self, offset: int, chunk: bytes) -> None:
        if not 0 < len(chunk) <= FILE_CHUNK_SIZE:
            raise ExecutorProtocolError("fs/writeBlock requires 1–1048576 decoded bytes")
        await self.client.request("fs/writeBlock", {"handleId": self.handle_id, "offset": offset,
                                                   "chunk": base64.b64encode(chunk).decode("ascii")})

    async def _close(self) -> dict:
        try:
            unconfirmed = False
            if self.open_task is not None:
                try:
                    await asyncio.shield(self.open_task)
                except ExecutorClosed:
                    unconfirmed = True
            if self.opened or unconfirmed:
                await self.client.request("fs/close", {"handleId": self.handle_id}, cleanup=True)
                self.closed = True
            return {"handle_id": self.handle_id, "closed": self.closed}
        finally:
            if self.client.files.get(self.handle_id) is self:
                self.client.files.pop(self.handle_id)
            self.client.notify_idle()

    async def aclose(self) -> dict:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="executor-file-close")
        return await asyncio.shield(self.close_task)

    async def __aenter__(self) -> ExecutorFile:
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()

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


class ExecutorFileSystem:
    def __init__(self, environment):
        self.environment = environment

    async def _path_call(self, method: str, path: str, sandbox: dict | None, **options) -> dict:
        params = {"path": file_uri(path), "sandbox": deepcopy(sandbox), **options}
        client = await self.environment.get_client()
        return await client.request(method, params)

    @staticmethod
    def _follow(follow_symlinks: bool) -> dict:
        return {} if follow_symlinks else {"followSymlinks": False}

    async def read_file(self, path: str, *, sandbox: dict | None, follow_symlinks: bool = True) -> bytes:
        result = await self._path_call("fs/readFile", path, sandbox, **self._follow(follow_symlinks))
        return decode_bytes(result["dataBase64"], "dataBase64")

    async def write_file(self, path: str, contents: bytes, *, sandbox: dict | None, follow_symlinks: bool = True) -> None:
        await self._path_call("fs/writeFile", path, sandbox, dataBase64=base64.b64encode(contents).decode("ascii"),
                              **self._follow(follow_symlinks))

    async def create_directory(self, path: str, *, sandbox: dict | None, recursive: bool = False,
                               follow_symlinks: bool = True) -> None:
        await self._path_call("fs/createDirectory", path, sandbox, recursive=recursive, **self._follow(follow_symlinks))

    async def get_metadata(self, path: str, *, sandbox: dict | None, follow_symlinks: bool = True) -> dict:
        return await self._path_call("fs/getMetadata", path, sandbox, **self._follow(follow_symlinks))

    async def canonicalize(self, path: str, *, sandbox: dict | None) -> str:
        result = await self._path_call("fs/canonicalize", path, sandbox)
        return file_uri(result["path"])

    async def read_directory(self, path: str, *, sandbox: dict | None) -> list[dict]:
        result = await self._path_call("fs/readDirectory", path, sandbox)
        return result["entries"]

    async def walk(self, path: str, options: dict, *, sandbox: dict | None) -> dict:
        return await self._path_call("fs/walk", path, sandbox, options=deepcopy(options))

    async def remove(self, path: str, *, sandbox: dict | None, recursive: bool = False, force: bool = False,
                     follow_symlinks: bool = True) -> None:
        await self._path_call("fs/remove", path, sandbox, recursive=recursive, force=force, **self._follow(follow_symlinks))

    async def copy(self, source: str, destination: str, *, sandbox: dict | None, recursive: bool = False) -> None:
        params = {"sourcePath": file_uri(source), "destinationPath": file_uri(destination),
                  "recursive": recursive, "sandbox": deepcopy(sandbox)}
        client = await self.environment.get_client()
        await client.request("fs/copy", params)

    async def open(self, path: str, *, sandbox: dict | None, mode: str = "read") -> ExecutorFile:
        path = file_uri(path)
        sandbox = deepcopy(sandbox)
        client = await self.environment.get_client()
        capabilities = (client.environment_info or {}).get("capabilities", {})
        if mode == "replace" and not capabilities.get("fileWriteStreaming", False):
            raise ExecutorProtocolError("executor does not support file write streaming")
        handle = ExecutorFile(client, uuid4().hex, sandbox)
        client.files[handle.handle_id] = handle

        async def open_file() -> None:
            response = await client.request("fs/open", {"handleId": handle.handle_id, "path": path,
                                                       "mode": mode, "sandbox": handle.sandbox})
            if response["handleId"] != handle.handle_id:
                raise ExecutorProtocolError("exec-server returned another file handle")
            handle.opened = True

        handle.open_task = asyncio.create_task(open_file(), name="executor-file-open")
        try:
            await asyncio.shield(handle.open_task)
            return handle
        except asyncio.CancelledError:
            client.retain_cleanup(handle.aclose())
            raise
        except ExecutorDisconnected:
            async def cleanup_unconfirmed_open() -> None:
                try:
                    await client.request("fs/close", {"handleId": handle.handle_id}, cleanup=True)
                finally:
                    if client.files.get(handle.handle_id) is handle:
                        client.files.pop(handle.handle_id)
            client.retain_cleanup(cleanup_unconfirmed_open())
            raise
        except Exception:
            client.files.pop(handle.handle_id, None)
            client.notify_idle()
            raise
