"""Editor LSP over the original executor's process and file-URI domain."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.environments.errors import ExecutorProtocolError
from backend.lsp.client import LSPClient, LSPTransportClosed, _LANGUAGE_SERVERS, _SERVER_ARGS
from backend.workspace.executor_paths import ExecutorPath


class ExecutorLanguageServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    initialization_options: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class ExecutorLanguageConfiguration(BaseModel):
    """MiniCode editor configuration, not part of Source environments.toml."""
    model_config = ConfigDict(extra="forbid")
    language_servers: dict[Literal["python", "yaml", "c", "cpp"], ExecutorLanguageServer]


async def target_language_server(service, language: str) -> ExecutorLanguageServer:
    # This is an explicit editor configuration read, never an editor/agent
    # write to the directory defining workspace behavior.
    path = await service.allowed(".minicode/language-servers.json", inventory=True)
    if await service.metadata(path) is not None:
        try:
            config = ExecutorLanguageConfiguration.model_validate_json(await service.read_text(path))
        except ValidationError as error:
            raise HTTPException(422, {"code": "editor.executor_language_server_configuration",
                "path_uri": path.uri, "errors": error.errors(include_input=False, include_context=False)}) from error
        if language in config.language_servers:
            return config.language_servers[language]
    extension = {"python": "py", "yaml": "yaml", "c": "c", "cpp": "cpp"}[language]
    command = _LANGUAGE_SERVERS[extension]
    options = {"fallbackFlags": ["-x", "c++" if language == "cpp" else "c",
        "-std=c++17" if language == "cpp" else "-std=c17"]} if language in {"c", "cpp"} else None
    # The executor resolves this name using its own PATH. No controller
    # executable discovery or bundled host path enters this launch.
    return ExecutorLanguageServer(argv=[command, *_SERVER_ARGS[command]], initialization_options=options)


class _ExecutorInput:
    def __init__(self, handle):
        self.handle = handle
        self.pending = bytearray()
        self.lock = asyncio.Lock()

    def write(self, chunk):
        self.pending.extend(chunk)

    async def drain(self):
        async with self.lock:
            chunk = bytes(self.pending)
            self.pending.clear()
            if await self.handle.write(chunk) != "accepted":
                raise LSPTransportClosed("The original executor language-server stdin is closed")


class _ExecutorStdio:
    def __init__(self, handle):
        self.handle = handle
        self.stdin = _ExecutorInput(handle)
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.reader = asyncio.create_task(self._read(), name=f"editor-lsp-stdio-{handle.process_id}")

    @property
    def returncode(self):
        return self.handle.exit_code if self.handle.exited else None

    async def _read(self):
        cursor = 0
        try:
            while True:
                result = await self.handle.read(after_seq=cursor, wait_ms=1000)
                for chunk in result.chunks:
                    {"stdout": self.stdout, "stderr": self.stderr}[chunk.stream].feed_data(chunk.chunk)
                cursor = result.next_seq - 1
                if result.failure:
                    raise ExecutorProtocolError(result.failure)
                if result.closed:
                    self.handle.closed = True
                    self.handle.exited, self.handle.exit_code = result.exited, result.exit_code
                    break
        except BaseException as error:
            self.stdout.set_exception(error)
            self.stderr.set_exception(error)
            raise
        finally:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self):
        await asyncio.shield(self.reader)
        return self.returncode


class ExecutorLanguageProcess:
    """The LSP runner owns a clone of the accepted original registration."""
    def __init__(self, service, spec: ExecutorLanguageServer):
        self.lease = service.lease.clone()
        self.environment = self.lease.environment
        self.cwd = service.cwd
        self.sandbox = deepcopy(service.scope.process_sandbox_context())
        policy = service.config.shell_environment_policy
        self.env_policy = {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
            "exclude": list(policy.exclude), "set": dict(policy.set_values), "includeOnly": list(policy.include_only)}
        self.spec = spec
        self.handle = None
        self.process = None

    async def spawn_interactive(self, argv, **_kwargs):
        def retain(handle):
            self.handle = handle
        handle = await self.environment.process.start(argv, self.cwd.uri, sandbox=self.sandbox,
            env=self.spec.env, env_policy=self.env_policy, pipe_stdin=True, process_observer=retain)
        self.process = _ExecutorStdio(handle)
        return self.process

    async def terminate(self, _process):
        await self.handle.aclose()
        return self.handle.released

    async def cleanup(self):
        if self.handle is not None:
            receipt = await self.handle.aclose()
            if not self.handle.released:
                raise RuntimeError(f"Original executor language-server cleanup is unconfirmed: {receipt}")
        try:
            if self.process is not None:
                await self.process.wait()
        finally:
            await self.lease.aclose()
        return True


class ExecutorEditorLSPClient(LSPClient):
    def __init__(self, service, spec: ExecutorLanguageServer):
        self.service = service
        self.server_spec = spec
        runner = ExecutorLanguageProcess(service, spec)
        self.execution_sandbox = runner.sandbox
        super().__init__(spec.argv[0], spec.argv[1:], service.cwd.uri,
            server_name="executor-editor-language", sandbox_runner=runner,
            initialization_options=spec.initialization_options)

    def _normalize_file_path(self, path):
        return ExecutorPath.parse(path, platform_os=self.service.scope.platform_os).uri

    def _file_key(self, path):
        return ExecutorPath.parse(path, platform_os=self.service.scope.platform_os)._identity()

    def _path_to_uri(self, path):
        return self._normalize_file_path(path)

    def _file_path_from_uri(self, uri):
        return self._normalize_file_path(uri)

    async def _read_source_file(self, path):
        return await self.service.read_text(path)

    def _workspace_name(self):
        return self.service.cwd.name

    def _client_process_id(self):
        # The controller's PID has no meaning to a server on another machine.
        return None
