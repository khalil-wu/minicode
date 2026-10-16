"""Language servers for explicit editor operations in a trusted workspace.

The bundled Pyright, YAML and clangd entry points are launched here. Agent tool
language servers keep their separate permission-bound manager.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

from backend.config import PROJECT_ROOT
from backend.runtime_env import sanitized_subprocess_env
from backend.lsp.client import LSPClient
from backend.subprocesses import communicate, spawn_exec, terminate_process_tree


async def cpp_compiler_configuration(language: str) -> tuple[list[str], list[str]]:
    compiler = shutil.which("g++" if language == "cpp" else "gcc") or shutil.which("clang++" if language == "cpp" else "clang")
    flags = ["-x", "c++" if language == "cpp" else "c", "-std=c++17" if language == "cpp" else "-std=c17"]
    if compiler is None:
        return flags, []
    process = await spawn_exec(compiler, "-E", "-x", "c++" if language == "cpp" else "c", "-", "-v",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=sanitized_subprocess_env())
    _, stderr = await communicate(process, input_data=b"", timeout=15)
    output = stderr.decode("utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(f"C/C++ 编译器无法读取头文件路径：{output.strip()}")
    in_search_paths = False
    for line in output.splitlines():
        if line.strip() == "#include <...> search starts here:":
            in_search_paths = True
        elif line.strip() == "End of search list.":
            in_search_paths = False
        elif in_search_paths:
            flags.extend(["-isystem", str(Path(line.strip()).resolve())])
        elif line.startswith("Target: "):
            flags.append("--target=" + line.removeprefix("Target: ").strip())
    return flags, ["--query-driver=" + Path(compiler).as_posix()]


class EditorLanguageProcess:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    async def spawn_interactive(self, argv, *, container_argv=None, **kwargs):
        env = sanitized_subprocess_env({"ELECTRON_RUN_AS_NODE": "1"})
        self.process = await spawn_exec(*argv, env=env, **kwargs)
        return self.process

    async def terminate(self, process):
        return await terminate_process_tree(process)

    async def cleanup(self):
        return self.process is None or await self.terminate(self.process)

    def map_path_to_sandbox(self, path):
        return path

    def map_path_from_sandbox(self, path):
        return path


class EditorLanguageStateError(RuntimeError):
    """The renderer retired or superseded this document snapshot."""


@dataclass
class EditorDocumentState:
    document_id: str
    snapshot_revision: int
    model_version: int
    path: str


class EditorLanguageServers:
    def __init__(self):
        self.clients: dict[tuple[str, str, str, str], LSPClient] = {}
        self.locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self.documents: dict[tuple[str, str, str, str], dict[str, EditorDocumentState]] = {}
        self.closed_clients: set[tuple[str, str]] = set()
        self.closed_documents: set[tuple[str, str, str]] = set()
        self.closed_windows: set[str] = set()
        self.window_bindings: dict[str, tuple[str, str]] = {}
        self.operations: dict[tuple[str, str, str, str], tuple[asyncio.Task[Any], set[str]]] = {}

    def bind_browser_window(self, instance_id: str, owner_id: str, binding_token: str) -> bool:
        bound = self.window_bindings.get(instance_id)
        if bound is None:
            self.window_bindings[instance_id] = (owner_id, binding_token)
            return True
        return bound[0] == owner_id and hmac.compare_digest(bound[1], binding_token)

    @staticmethod
    def _path_key(path):
        if path.startswith("file:"):
            from backend.workspace.executor_paths import ExecutorPath
            return ExecutorPath.parse(path)._identity()
        return os.path.normcase(path)

    def _check_snapshot(self, key, documents, snapshot_revision):
        if key[0] in self.closed_windows:
            raise EditorLanguageStateError("The editor window was closed.")
        if key[:2] in self.closed_clients:
            raise EditorLanguageStateError("The editor language client was released.")
        owned = self.documents.get(key, {})
        for document in documents:
            if (*key[:2], document["document_id"]) in self.closed_documents:
                raise EditorLanguageStateError("The editor document was closed.")
            previous = owned.get(self._path_key(document["path"]))
            if previous is not None and (snapshot_revision < previous.snapshot_revision
                    or previous.document_id == document["document_id"] and document["model_version"] < previous.model_version):
                raise EditorLanguageStateError("The editor document snapshot was superseded.")

    async def _await_document_operation(self, key, documents, snapshot_revision, operation: Callable[[], Coroutine[Any, Any, Any]]):
        self._check_snapshot(key, documents, snapshot_revision)
        task = asyncio.create_task(operation())
        self.operations[key] = (task, {document["document_id"] for document in documents})
        try:
            return await task
        except asyncio.CancelledError:
            # Lifecycle cancellation has a concrete 409 receipt. External
            # request cancellation keeps its original cancellation semantics.
            self._check_snapshot(key, documents, snapshot_revision)
            raise
        finally:
            self.operations.pop(key)

    def _cancel_document_operations(self, owner, document_ids: set[str] | None = None):
        for key, (task, dependencies) in self.operations.items():
            if key[:2] == owner and (document_ids is None or dependencies & document_ids):
                task.cancel()

    async def request(
        self,
        root: Path,
        language: str,
        method: str,
        documents: list[dict],
        params: dict,
        *,
        window_instance_id: str,
        client_id: str,
        snapshot_revision: int,
        executor=None,
        server_spec=None,
    ) -> dict:
        root_key = str(root)
        if executor is not None:
            from backend.workspace.executor_service import executor_registration_id
            root_key = f"executor:{executor_registration_id(executor.context)}:{executor.context.conversation_id}:{root}"
        key = (window_instance_id, client_id, root_key, language)
        self._check_snapshot(key, documents, snapshot_revision)
        async with self.locks.setdefault(key, asyncio.Lock()):
            self._check_snapshot(key, documents, snapshot_revision)
            client = self.clients.get(key)
            changed_launch = (executor is not None and client is not None
                and (client.server_spec != server_spec or client.execution_sandbox != executor.scope.process_sandbox_context()))
            if client is None or not client.is_running() or changed_launch:
                if client is not None:
                    await client.stop()
                self._check_snapshot(key, documents, snapshot_revision)
                if executor is None:
                    resources = os.environ.get("MINICODE_APP_RESOURCES_DIR")
                    editor_resources = os.environ.get("MINICODE_EDITOR_LANGUAGE_SERVICES_DIR")
                    directory = (
                        Path(editor_resources)
                        if editor_resources else Path(resources) / "language-services"
                        if resources
                        else PROJECT_ROOT / "desktop/language-services"
                    )
                    if language in {"c", "cpp"}:
                        entry = directory / "clangd/clangd_23.1.0/bin/clangd.exe"
                        command = str(entry) if entry.is_file() else shutil.which("clangd")
                        if not command:
                            raise RuntimeError("C/C++ 语言服务尚未准备，请运行 desktop 的 npm run clangd:prepare。")
                        flags, driver_args = await self._await_document_operation(key, documents, snapshot_revision, lambda: cpp_compiler_configuration(language))
                        # Requests are serialized by this client. Let clangd finish
                        # its current document parse before handling completion;
                        # otherwise a new include can return an empty symbol list.
                        args = ["--sync", "--use-dirty-headers", "--background-index", "--clang-tidy", "--completion-parse=always", "--completion-style=detailed", "--header-insertion=never", *driver_args]
                        initialization_options = {"fallbackFlags": flags}
                    else:
                        command = os.environ.get("MINICODE_EDITOR_NODE") or shutil.which("node")
                        entry = (
                            directory
                            / "node_modules"
                            / (
                                "pyright/langserver.index.js"
                                if language == "python"
                                else "yaml-language-server/bin/yaml-language-server"
                            )
                        )
                        if not command or not entry.is_file():
                            raise RuntimeError(
                                "内置语言服务尚未安装，请在 desktop/language-services 运行 npm ci 后重启。"
                            )
                        args = [str(entry), "--stdio"]
                        initialization_options = None
                    self._check_snapshot(key, documents, snapshot_revision)
                    client = LSPClient(
                        command,
                        args,
                        str(root),
                        sandbox_runner=EditorLanguageProcess(),
                        initialization_options=initialization_options,
                    )
                    settings = (
                        {
                            "python": {
                                "analysis": {
                                    "diagnosticMode": "openFilesOnly",
                                    "typeCheckingMode": "basic",
                                }
                            }
                        }
                        if language == "python"
                        else {
                            "yaml": {
                                "validate": True,
                                "hover": True,
                                "completion": True,
                                "format": {"enable": True},
                                "schemaStore": {"enable": False},
                            }
                        }
                        if language == "yaml" else {}
                    )
                else:
                    from backend.lsp.executor_editor import ExecutorEditorLSPClient
                    client = ExecutorEditorLSPClient(executor, server_spec)
                    settings = server_spec.settings if server_spec.settings is not None else (
                        {"python": {"analysis": {"diagnosticMode": "openFilesOnly", "typeCheckingMode": "basic"}}}
                        if language == "python" else
                        {"yaml": {"validate": True, "hover": True, "completion": True, "format": {"enable": True}, "schemaStore": {"enable": False}}}
                        if language == "yaml" else {})
                self.clients[key] = client
                client.settings = settings
                await self._await_document_operation(key, documents, snapshot_revision, client.start)
                await client._send_notification("workspace/didChangeConfiguration", {"settings": settings})
            # A close can arrive while startup is awaiting the server. Retire
            # that snapshot before publishing any didOpen/didChange message.
            self._check_snapshot(key, documents, snapshot_revision)
            owned = self.documents.setdefault(key, {})
            # The requested document is first. Publish its dirty dependencies
            # before asking a synchronous server to parse that document.
            for document in [*documents[1:], documents[0]]:
                self._check_snapshot(key, documents, snapshot_revision)
                path_key = self._path_key(document["path"])
                previous = owned.get(path_key)
                if previous is not None and previous.document_id != document["document_id"]:
                    self.closed_documents.add((*key[:2], previous.document_id))
                    await client.close_file(previous.path)
                owned[path_key] = EditorDocumentState(
                    document["document_id"], snapshot_revision, document["model_version"], document["path"],
                )
                await self._await_document_operation(key, documents, snapshot_revision,
                    lambda: client._sync_file(document["path"], document["content"], document.get("language"),
                        **({"model_version": document["model_version"]} if executor is not None else {})))
            self._check_snapshot(key, documents, snapshot_revision)
            if method == "diagnostics":
                path = documents[0]["path"]
                expected = client._opened_files[path][0]

                async def current_diagnostics():
                    while True:
                        client.diagnostics_changed.clear()
                        snapshot = client.diagnostics.get(self._path_key(path))
                        # LSP versions are optional; YAML publishes unversioned
                        # results. didChange clears its previous document result.
                        if snapshot and snapshot.get("version", expected) == expected:
                            return snapshot["diagnostics"]
                        await client.diagnostics_changed.wait()

                result = await self._await_document_operation(key, documents, snapshot_revision, lambda: asyncio.wait_for(current_diagnostics(), 15))
            elif method == "formatting" and language == "python" and executor is None:
                async def format_python():
                    process = await spawn_exec(
                        sys.executable, "-m", "ruff", "format", "--stdin-filename", documents[0]["path"], "-",
                        cwd=root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await communicate(process, input_data=documents[0]["content"].encode("utf-8"), timeout=15)
                    if process.returncode:
                        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())
                    return {"text": stdout.decode("utf-8")}
                result = await self._await_document_operation(key, documents, snapshot_revision, format_python)
            else:
                result = await self._await_document_operation(key, documents, snapshot_revision, lambda: client._send_request(f"textDocument/{method}", params))
            self._check_snapshot(key, documents, snapshot_revision)
            return {"result": result, "capabilities": client.capabilities, "snapshot_revision": snapshot_revision}

    async def _stop_context(self, key):
        client = self.clients.get(key)
        if client is not None:
            await client.stop()
            self.clients.pop(key)
        self.documents.pop(key, None)

    async def close_documents(self, *, window_instance_id: str, client_id: str, document_ids: list[str]):
        owner = (window_instance_id, client_id)
        closed = set(document_ids)
        # This fence precedes the lock: queued requests and startup cannot
        # reopen a model whose onWillDispose has already reached this endpoint.
        self.closed_documents.update((*owner, document_id) for document_id in closed)
        self._cancel_document_operations(owner, closed)
        for key, lock in [(key, lock) for key, lock in self.locks.items() if key[:2] == owner]:
            async with lock:
                owned = self.documents.get(key, {})
                client = self.clients.get(key)
                for path, document in list(owned.items()):
                    if document.document_id in closed:
                        if client is not None and client.is_running():
                            await client.close_file(document.path)
                        owned.pop(path)
                if not owned:
                    await self._stop_context(key)

    async def release_client(self, *, window_instance_id: str, client_id: str):
        owner = (window_instance_id, client_id)
        self.closed_clients.add(owner)
        self._cancel_document_operations(owner)
        failures = []
        for key, lock in [(key, lock) for key, lock in self.locks.items() if key[:2] == owner]:
            async with lock:
                try:
                    await self._stop_context(key)
                except (RuntimeError, OSError) as exc:
                    failures.append(str(exc))
                else:
                    self.locks.pop(key, None)
        if failures:
            raise RuntimeError("Editor language processes remain owned: " + "; ".join(failures))
        self.closed_documents = {entry for entry in self.closed_documents if entry[:2] != owner}

    async def release_window(self, window_instance_id: str):
        self.closed_windows.add(window_instance_id)
        owners = {key[:2] for key in self.locks if key[0] == window_instance_id}
        results = await asyncio.gather(*(self.release_client(window_instance_id=window, client_id=client) for window, client in owners), return_exceptions=True)
        failures = [str(result) for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError("Editor language window cleanup failed: " + "; ".join(failures))

    async def shutdown(self):
        owners = {key[:2] for key in self.locks}
        results = await asyncio.gather(*(self.release_client(window_instance_id=window, client_id=client) for window, client in owners), return_exceptions=True)
        failures = [str(result) for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError("Editor language shutdown failed: " + "; ".join(failures))
        self.closed_clients.clear()
        self.closed_documents.clear()
        self.closed_windows.clear()


editor_language_servers = EditorLanguageServers()
