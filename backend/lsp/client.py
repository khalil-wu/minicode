"""
Lightweight Language Server Protocol (LSP) client.

Provides a minimal LSP client that can communicate with any language server
(pyright, typescript-language-server, gopls, rust-analyzer, etc.) via stdio.

The client is intentionally non-conformant to the full LSP spec — it implements
only the subset needed for agent-driven code navigation:
  - initialize / shutdown
  - textDocument/didOpen (lazy, per-file)
  - textDocument/definition
  - textDocument/references
  - textDocument/hover
  - textDocument/documentSymbol

Design goals:
  - Zero hard dependencies (the `lsprotocol` package is optional).
  - One manager per workspace root, reused across tool calls.
  - Graceful fallback: if the server crashes or is unavailable, callers
    receive empty results and the manager auto-restarts on next use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# ── Language server commands by file extension ──────────────────────

_LANGUAGE_SERVERS: dict[str, str] = {
    "py": "pyright-langserver",
    "pyi": "pyright-langserver",
    "ts": "typescript-language-server",
    "tsx": "typescript-language-server",
    "js": "typescript-language-server",
    "jsx": "typescript-language-server",
    "go": "gopls",
    "rs": "rust-analyzer",
    "java": "jdtls",
    "c": "clangd",
    "cpp": "clangd",
    "h": "clangd",
    "hpp": "clangd",
    "rb": "solargraph",
    "cs": "OmniSharp",
}

# Common args for servers that need them
_SERVER_ARGS: dict[str, list[str]] = {
    "pyright-langserver": ["--stdio"],
    "typescript-language-server": ["--stdio"],
    "gopls": ["serve"],
    "rust-analyzer": [],
    "clangd": [],
}

_EXTENSIONS_BY_LANG: dict[str, str] = {
    "python": "py",
    "typescript": "ts",
    "typescriptreact": "tsx",
    "javascript": "js",
    "javascriptreact": "jsx",
    "go": "go",
    "rust": "rs",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "ruby": "rb",
    "csharp": "cs",
}


@dataclass
class LSPLocation:
    """A single location returned by definition/references."""

    file: str
    line: int  # 0-based
    character: int  # 0-based
    end_line: int = 0
    end_character: int = 0

    def to_display(self) -> str:
        return f"{self.file}:{self.line + 1}:{self.character + 1}"


@dataclass
class LSPHover:
    """Hover information for a symbol."""

    contents: str
    range_start_line: int = 0
    range_end_line: int = 0


@dataclass
class LSPSymbol:
    """Document symbol."""

    name: str
    kind: int
    line: int
    character: int
    end_line: int = 0
    end_character: int = 0
    children: list["LSPSymbol"] = field(default_factory=list)


class LSPClient:
    """Minimal stdio-based LSP client for a single language server."""

    def __init__(self, command: str, args: list[str], workspace_root: str) -> None:
        self._command = command
        self._args = args
        self._workspace_root = workspace_root
        self._process: asyncio.subprocess.Process | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._initialized = False
        self._opened_files: set[str] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workspace_root,
            )
            self._stdin = self._process.stdin
            self._stdout = self._process.stdout
        except FileNotFoundError:
            raise RuntimeError(f"Language server not found: {self._command}")
        except OSError as exc:
            raise RuntimeError(f"Failed to start language server: {exc}")

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    async def stop(self) -> None:
        if self._process and self._process.returncode is None:
            try:
                await self._send_request("shutdown", {})
                await self._send_notification("exit", {})
            except Exception:
                pass
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
        for task_attr in ("_reader_task", "_stderr_task"):
            task = getattr(self, task_attr)
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, task_attr, None)
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._process = None
        self._stdin = None
        self._stdout = None
        self._initialized = False
        self._opened_files.clear()

    async def _initialize(self) -> None:
        result = await self._send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": _path_to_uri(self._workspace_root),
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"dynamicRegistration": False},
                    "documentSymbol": {"dynamicRegistration": False},
                    "synchronization": {
                        "didOpen": True,
                        "didChange": True,
                        "didClose": True,
                    },
                },
                "workspace": {
                    "symbol": {"dynamicRegistration": False},
                },
            },
            "workspaceFolders": [
                {"uri": _path_to_uri(self._workspace_root), "name": Path(self._workspace_root).name}
            ],
        })
        await self._send_notification("initialized", {})
        self._initialized = True

    async def _ensure_file_open(self, file_path: str) -> None:
        abs_path = str(Path(file_path).resolve())
        if abs_path in self._opened_files:
            return
        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        ext = Path(abs_path).suffix.lstrip(".")
        lang_id = _language_id_for_extension(ext)
        await self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": _path_to_uri(abs_path),
                "languageId": lang_id,
                "version": 1,
                "text": content,
            }
        })
        self._opened_files.add(abs_path)

    async def definition(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/definition", {
            "textDocument": {"uri": _path_to_uri(str(Path(file_path).resolve()))},
            "position": {"line": line, "character": character},
        })
        return _parse_locations(result)

    async def references(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/references", {
            "textDocument": {"uri": _path_to_uri(str(Path(file_path).resolve()))},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        })
        return _parse_locations(result)

    async def hover(self, file_path: str, line: int, character: int) -> LSPHover | None:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/hover", {
            "textDocument": {"uri": _path_to_uri(str(Path(file_path).resolve()))},
            "position": {"line": line, "character": character},
        })
        if not isinstance(result, dict):
            return None
        contents = result.get("contents")
        text = ""
        if isinstance(contents, str):
            text = contents
        elif isinstance(contents, list):
            text = "\n".join(
                c if isinstance(c, str) else str(c.get("value", "")) if isinstance(c, dict) else ""
                for c in contents
            )
        elif isinstance(contents, dict):
            text = str(contents.get("value", ""))
        rng = result.get("range") or {}
        start = rng.get("start", {}) if isinstance(rng, dict) else {}
        end = rng.get("end", {}) if isinstance(rng, dict) else {}
        return LSPHover(
            contents=text.strip(),
            range_start_line=start.get("line", 0),
            range_end_line=end.get("line", 0),
        )

    async def document_symbols(self, file_path: str) -> list[LSPSymbol]:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/documentSymbol", {
            "textDocument": {"uri": _path_to_uri(str(Path(file_path).resolve()))},
        })
        if not isinstance(result, list):
            return []
        return [_parse_symbol(item) for item in result if isinstance(item, dict)]

    async def _send_request(self, method: str, params: dict[str, Any]) -> Any:
        if not self._stdin or not self._process or self._process.returncode is not None:
            raise RuntimeError("LSP client not running")
        msg_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        body = json.dumps(message)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        self._stdin.write(header.encode("utf-8") + body.encode("utf-8"))
        await self._stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"LSP request timed out: {method}")

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if not self._stdin or not self._process or self._process.returncode is not None:
            return
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        body = json.dumps(message)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        self._stdin.write(header.encode("utf-8") + body.encode("utf-8"))
        await self._stdin.drain()

    async def _read_loop(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = await self._stdout.read(4096)  # type: ignore[union-attr]
                if not chunk:
                    break
                buffer += chunk
                while b"\r\n\r\n" in buffer:
                    header_end = buffer.index(b"\r\n\r\n") + 4
                    header = buffer[:header_end].decode("utf-8", errors="replace")
                    content_length = 0
                    for line in header.strip().split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            content_length = int(line.split(":", 1)[1].strip())
                    if len(buffer) < header_end + content_length:
                        break
                    body = buffer[header_end:header_end + content_length]
                    buffer = buffer[header_end + content_length:]
                    try:
                        message = json.loads(body.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    msg_id = message.get("id")
                    if msg_id is not None and msg_id in self._pending:
                        future = self._pending.pop(msg_id)
                        if not future.done():
                            if "error" in message:
                                error = message.get("error") if isinstance(message.get("error"), dict) else {}
                                future.set_exception(
                                    RuntimeError(str(error.get("message") or error or "LSP request failed"))
                                )
                            else:
                                future.set_result(message.get("result"))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("LSP read loop error: %s", exc)
                break

    async def _drain_stderr(self) -> None:
        """Drain language-server stderr so verbose logs cannot block stdio."""
        stderr = self._process.stderr if self._process else None
        if stderr is None:
            return
        while True:
            try:
                chunk = await stderr.read(4096)
                if not chunk:
                    break
                logger.debug("LSP stderr %s: %s", self._command, chunk.decode("utf-8", errors="replace").rstrip())
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("LSP stderr drain error: %s", exc)
                break


class LSPManager:
    """Manages LSP clients per workspace root and language server type."""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], LSPClient] = {}
        self._lock = asyncio.Lock()

    def server_for_file(self, file_path: str) -> str | None:
        ext = Path(file_path).suffix.lstrip(".")
        return _LANGUAGE_SERVERS.get(ext)

    def is_available(self, file_path: str) -> bool:
        server = self.server_for_file(file_path)
        if not server:
            return False
        return shutil.which(server) is not None

    async def get_client(self, file_path: str, workspace_root: str) -> LSPClient | None:
        server = self.server_for_file(file_path)
        if not server:
            return None
        if not shutil.which(server):
            return None
        key = (workspace_root, server)
        async with self._lock:
            client = self._clients.get(key)
            if client is not None and not client.is_running():
                try:
                    await client.stop()
                except Exception:
                    pass
                client = None
                self._clients.pop(key, None)
            if client is None:
                args = _SERVER_ARGS.get(server, [])
                client = LSPClient(server, args, workspace_root)
                try:
                    await client.start()
                except RuntimeError as exc:
                    logger.debug("LSP start failed for %s: %s", server, exc)
                    return None
                self._clients[key] = client
            return client

    async def shutdown_all(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                try:
                    await client.stop()
                except Exception:
                    pass
            self._clients.clear()


# ── Helpers ─────────────────────────────────────────────────────────

def _path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        return uri
    path = unquote(parsed.path if parsed.scheme else uri)
    if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    return path.replace("/", os.sep) if os.name == "nt" else path


def _parse_locations(result: Any) -> list[LSPLocation]:
    if not result:
        return []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    locations: list[LSPLocation] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        # Definition may return either Location or LocationLink.
        uri = str(item.get("uri") or item.get("targetUri") or "")
        if not uri:
            continue
        rng = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange") or {}
        start = rng.get("start", {}) if isinstance(rng, dict) else {}
        end = rng.get("end", {}) if isinstance(rng, dict) else {}
        locations.append(LSPLocation(
            file=_uri_to_path(uri),
            line=start.get("line", 0),
            character=start.get("character", 0),
            end_line=end.get("line", 0),
            end_character=end.get("character", 0),
        ))
    return locations


def _parse_symbol(item: dict[str, Any]) -> LSPSymbol:
    rng = item.get("location", {}).get("range") or item.get("range") or {}
    start = rng.get("start", {}) if isinstance(rng, dict) else {}
    end = rng.get("end", {}) if isinstance(rng, dict) else {}
    children = [
        _parse_symbol(child)
        for child in (item.get("children") or [])
        if isinstance(child, dict)
    ]
    return LSPSymbol(
        name=str(item.get("name") or ""),
        kind=int(item.get("kind") or 0),
        line=start.get("line", 0),
        character=start.get("character", 0),
        end_line=end.get("line", 0),
        end_character=end.get("character", 0),
        children=children,
    )


def _language_id_for_extension(ext: str) -> str:
    mapping = {
        "py": "python", "pyi": "python",
        "ts": "typescript", "tsx": "typescriptreact",
        "js": "javascript", "jsx": "javascriptreact",
        "go": "go", "rs": "rust", "java": "java",
        "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp",
        "rb": "ruby", "cs": "csharp",
    }
    return mapping.get(ext, "plaintext")


# Global singleton
_lsp_manager: LSPManager | None = None


def get_lsp_manager() -> LSPManager:
    global _lsp_manager
    if _lsp_manager is None:
        _lsp_manager = LSPManager()
    return _lsp_manager
