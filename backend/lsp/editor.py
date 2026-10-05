"""Language servers for explicit editor operations in a trusted workspace.

Only the bundled Pyright and YAML entry points are launched here. Agent tool
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
                node = os.environ.get("MINICODE_EDITOR_NODE") or shutil.which("node")
                resources = os.environ.get("MINICODE_APP_RESOURCES_DIR")
                directory = (
                    Path(resources) / "language-services"
                    if resources
                    else PROJECT_ROOT / "desktop/language-services"
                )
                entry = (
                    directory
                    / "node_modules"
                    / (
                        "pyright/langserver.index.js"
                        if language == "python"
                        else "yaml-language-server/bin/yaml-language-server"
                    )
                )
                if not node or not entry.is_file():
                    raise RuntimeError(
                        "内置语言服务尚未安装，请在 desktop/language-services 运行 npm ci 后重启。"
                    )
                client = LSPClient(
                    node,
                    [str(entry), "--stdio"],
                    str(root),
                    sandbox_runner=EditorLanguageProcess(),
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
                )
                await client._send_notification(
                    "workspace/didChangeConfiguration", {"settings": settings}
                )
            for document in documents:
                await client._sync_file(document["path"], document["content"])
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
