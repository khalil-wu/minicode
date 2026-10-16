"""Workspace I/O on an accepted, captured execution attachment.

The registry is intentionally absent. A consumer keeps the original lease,
URI domain and sandbox for its entire operation, including cleanup.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import errno
import hashlib
from itertools import count
import subprocess
from weakref import WeakKeyDictionary, WeakValueDictionary
from uuid import uuid4

from backend.atomic_io import normalize_text_newlines
from backend.environments.errors import ExecutorProtocolError, ExecutorRpcError
from backend.permissions.context import ToolExecutionContext
from backend.workspace.executor_paths import ExecutorPath


_registration_numbers = WeakKeyDictionary()
_next_registration = count(1)
_registration_epoch = uuid4().hex
_mutation_locks = WeakKeyDictionary()


def uses_executor_io(context) -> bool:
    return isinstance(context, ToolExecutionContext) and (context.turn_environment is not None
        or context.run_context is not None and (context.run_context.thread_environments is not None
            or context.run_context.turn_environments is not None))


def _attachment(context: ToolExecutionContext):
    if context is None or context.turn_environment is None:
        raise ExecutorProtocolError("No usable execution environment is attached to this tool call")
    if context.permission.environment_scope is None:
        raise ExecutorProtocolError("The accepted execution attachment has no permission scope")
    return context.turn_environment, context.permission.environment_scope


def resolve_executor_path(context: ToolExecutionContext, value: str) -> ExecutorPath:
    _environment, scope = _attachment(context)
    return ExecutorPath.resolve(value, cwd_uri=scope.cwd_uri, platform_os=scope.platform_os,
                                user_home_uri=scope.user_home_uri)


def _read_key(context: ToolExecutionContext, value: str) -> str:
    environment, _scope = _attachment(context)
    registration = environment.environment
    if registration not in _registration_numbers:
        _registration_numbers[registration] = next(_next_registration)
    # A registry ID can be replaced. This number names the actual original
    # registration object, never a replacement found under the same ID.
    return f"{_registration_epoch}:{_registration_numbers[registration]}:{resolve_executor_path(context, value)._identity()!r}"


def executor_registration_id(context: ToolExecutionContext) -> str:
    attachment, _scope = _attachment(context)
    registration = attachment.environment
    if registration not in _registration_numbers:
        _registration_numbers[registration] = next(_next_registration)
    return f"{_registration_epoch}:{_registration_numbers[registration]}"


def executor_read_hash(context: ToolExecutionContext, uri: str) -> str | None:
    return context.metadata.get("_executor_read_file_hashes", {}).get(_read_key(context, uri))


def record_executor_file_hash(context: ToolExecutionContext, uri: str, value: str | None) -> None:
    hashes = context.metadata.setdefault("_executor_read_file_hashes", {})
    observed = context.metadata.setdefault("_executor_observed_files", {})
    key = _read_key(context, uri)
    if value is None:
        hashes.pop(key, None)
        observed.pop(key, None)
    else:
        hashes[key] = value
        record_executor_file_observation(context, uri)


def record_executor_file_observation(context: ToolExecutionContext, uri: str) -> None:
    observed = context.metadata.setdefault("_executor_observed_files", {})
    key = _read_key(context, uri)
    observed.pop(key, None)
    observed[key] = {"environment_id": context.environment_id, "path_uri": resolve_executor_path(context, uri).uri,
                     "registration_id": executor_registration_id(context)}


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text_newlines(text).encode("utf-8")).hexdigest()


def validate_text_hash(previous: str | None, expected, *, require: bool = True) -> None:
    expected = str(expected or "").strip().lower()
    if previous is None:
        if expected:
            raise ValueError("expected_hash must be empty when creating a new file")
        return
    if not expected:
        if require:
            raise ValueError("expected_hash is required for existing files. Re-read the file with read_file and retry with its content_hash.")
        return
    actual = text_hash(previous)
    if actual != expected:
        raise ValueError(f"File changed on disk; expected_hash={expected}, actual_hash={actual}. Re-read before editing.")


def not_found(error: Exception) -> bool:
    # Source server/file_system_handler.rs maps NotFound to -32004, without
    # an OS-specific message or error.data. Other failures remain failures.
    return isinstance(error, FileNotFoundError) or isinstance(error, ExecutorRpcError) and error.code == -32004


class ExecutorWorkspaceService:
    def __init__(self, context: ToolExecutionContext):
        attachment, self.scope = _attachment(context)
        self.context = context
        self.config = attachment.config
        self.lease = attachment.lease.clone()
        self.environment = self.lease.environment
        self.filesystem = self.environment.filesystem
        self.sandbox = deepcopy(self.scope.sandbox_context())
        self.cwd = ExecutorPath.parse(self.scope.cwd_uri, platform_os=self.scope.platform_os)
        self.workspace_roots = tuple(ExecutorPath.parse(uri, platform_os=self.scope.platform_os)
                                     for uri in self.scope.workspace_root_uris)

    def resolve(self, value: str) -> ExecutorPath:
        return ExecutorPath.resolve(value, cwd_uri=self.scope.cwd_uri, platform_os=self.scope.platform_os,
                                    user_home_uri=self.scope.user_home_uri)

    def display(self, value: str | ExecutorPath) -> str:
        path = self.resolve(value) if isinstance(value, str) else value
        if path.is_relative_to(self.cwd):
            return path.relative_to(self.cwd).as_posix()
        return path.native

    async def metadata(self, value: str | ExecutorPath) -> dict | None:
        path = self.resolve(value) if isinstance(value, str) else value
        try:
            return await self.filesystem.get_metadata(path.uri, sandbox=self.sandbox)
        except (OSError, ExecutorRpcError) as error:
            if not_found(error):
                return None
            raise

    async def read_bytes(self, value: str | ExecutorPath, *, limit: int = 10 * 1024 * 1024) -> bytes:
        path = self.resolve(value) if isinstance(value, str) else value
        result = bytearray()
        async with await self.filesystem.open(path.uri, sandbox=self.sandbox) as handle:
            while True:
                chunk, eof = await handle.read_block(len(result), min(1024 * 1024, limit + 1 - len(result)))
                result.extend(chunk)
                if len(result) > limit:
                    raise ValueError(f"File is too large; limit is {limit} bytes")
                if eof:
                    return bytes(result)

    async def read_text(self, value: str | ExecutorPath, *, limit: int = 10 * 1024 * 1024) -> str:
        return (await self.read_bytes(value, limit=limit)).decode("utf-8")

    async def write_bytes(self, value: str | ExecutorPath, contents: bytes) -> None:
        path = self.resolve(value) if isinstance(value, str) else value
        if await self.metadata(path.parent) is None:
            await self.filesystem.create_directory(path.parent.uri, sandbox=self.sandbox, recursive=True)
        await self.filesystem.write_file(path.uri, contents, sandbox=self.sandbox)

    async def remove(self, value: str | ExecutorPath) -> None:
        path = self.resolve(value) if isinstance(value, str) else value
        await self.filesystem.remove(path.uri, sandbox=self.sandbox)

    async def list_directory(self, value: str | ExecutorPath) -> list[dict]:
        path = self.resolve(value) if isinstance(value, str) else value
        return await self.filesystem.read_directory(path.uri, sandbox=self.sandbox)

    async def walk(self, value: str | ExecutorPath, *, limit: int = 50_000, max_depth: int = 64) -> dict:
        path = self.resolve(value) if isinstance(value, str) else value
        return await self.filesystem.walk(path.uri, {"maxDepth": max_depth, "maxDirectories": 10_000,
            "maxEntries": limit, "followDirectorySymlinks": False}, sandbox=self.sandbox)

    @asynccontextmanager
    async def mutate(self, values: list[str | ExecutorPath]):
        paths = sorted({self.resolve(value) if isinstance(value, str) else value for value in values}, key=lambda path: path.uri)
        locks = _mutation_locks.setdefault(self.environment, WeakValueDictionary())
        held = [locks.setdefault(path, asyncio.Lock()) for path in paths]
        acquired = []
        try:
            for lock in held:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    async def run(self, argv: list[str], *, cwd: str | ExecutorPath | None = None,
                  timeout: float | None = 30, output_limit: int = 20 * 1024 * 1024,
                  env: dict[str, str] | None = None, env_policy: dict | None = None,
                  permission_scope=None) -> subprocess.CompletedProcess:
        directory = self.cwd if cwd is None else self.resolve(cwd) if isinstance(cwd, str) else cwd
        scope = self.scope if permission_scope is None else permission_scope
        process = await self.environment.process.start(argv, directory.uri, sandbox=scope.process_sandbox_context(),
            env=env, env_policy=env_policy or {"inherit": self.config.shell_environment_policy.inherit,
                "ignoreDefaultExcludes": self.config.shell_environment_policy.ignore_default_excludes,
                "exclude": list(self.config.shell_environment_policy.exclude),
                "set": dict(self.config.shell_environment_policy.set_values),
                "includeOnly": list(self.config.shell_environment_policy.include_only)})
        stdout, stderr = bytearray(), bytearray()
        cursor = 0
        try:
            async with asyncio.timeout(timeout):
                while True:
                    result = await process.read(after_seq=cursor, max_bytes=64 * 1024, wait_ms=1000)
                    if result.failure:
                        raise ExecutorProtocolError(result.failure)
                    for chunk in result.chunks:
                        sink = stderr if chunk.stream == "stderr" else stdout
                        sink.extend(chunk.chunk)
                        if len(sink) > output_limit:
                            raise OSError(errno.EFBIG, f"executor command output exceeded {output_limit} bytes")
                    cursor = result.next_seq - 1
                    if result.closed:
                        return subprocess.CompletedProcess(argv, result.exit_code, bytes(stdout), bytes(stderr))
        finally:
            await process.aclose()

    async def aclose(self) -> None:
        await self.lease.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()
