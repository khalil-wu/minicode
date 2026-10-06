"""Language servers for explicit editor operations in a trusted workspace.

The bundled Pyright, YAML and clangd entry points are launched here. Agent tool
language servers keep their separate permission-bound manager.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

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


class EditorLanguageServers:
    def __init__(self):
        self.clients: dict[tuple[str, str], LSPClient] = {}
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def request(
        self,
        root: Path,
        language: str,
        method: str,
        documents: list[dict],
        params: dict,
    ) -> dict:
        key = (str(root), language)
        async with self.locks.setdefault(key, asyncio.Lock()):
            client = self.clients.get(key)
            if client is None or not client.is_running():
                if client is not None:
                    await client.stop()
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
                    flags, driver_args = await cpp_compiler_configuration(language)
                    # Requests are serialized by this client. Let clangd finish
                    # its current document parse before handling completion;
                    # otherwise a new include can return an empty symbol list.
                    args = ["--sync", "--background-index", "--clang-tidy", "--completion-parse=always", "--completion-style=detailed", "--header-insertion=never", *driver_args]
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
                client = LSPClient(
                    command,
                    args,
                    str(root),
                    sandbox_runner=EditorLanguageProcess(),
                    initialization_options=initialization_options,
                )
                self.clients[key] = client
                await client.start()
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
                await client._send_notification(
                    "workspace/didChangeConfiguration", {"settings": settings}
                )
            for document in documents:
                await client._sync_file(document["path"], document["content"], document.get("language"))
            if method == "diagnostics":
                path = documents[0]["path"]
                expected = client._opened_files[path][0]

                async def current_diagnostics():
                    while True:
                        client.diagnostics_changed.clear()
                        snapshot = client.diagnostics.get(os.path.normcase(path))
                        # LSP versions are optional; YAML publishes unversioned
                        # results. didChange clears its previous document result.
                        if snapshot and snapshot.get("version", expected) == expected:
                            return snapshot["diagnostics"]
                        await client.diagnostics_changed.wait()

                result = await asyncio.wait_for(current_diagnostics(), 15)
            elif method == "formatting" and language == "python":
                process = await spawn_exec(
                    sys.executable,
                    "-m",
                    "ruff",
                    "format",
                    "--stdin-filename",
                    documents[0]["path"],
                    "-",
                    cwd=root,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await communicate(
                    process,
                    input_data=documents[0]["content"].encode("utf-8"),
                    timeout=15,
                )
                if process.returncode:
                    raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())
                result = {"text": stdout.decode("utf-8")}
            else:
                result = await client._send_request(f"textDocument/{method}", params)
            return {"result": result, "capabilities": client.capabilities}

    async def shutdown(self):
        for client in self.clients.values():
            await client.stop()
        self.clients.clear()
        self.locks.clear()


editor_language_servers = EditorLanguageServers()
