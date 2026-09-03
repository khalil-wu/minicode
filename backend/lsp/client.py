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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from backend.runtime_env import sanitized_subprocess_env
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner, SandboxUnavailableError

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

    def __init__(
        self,
        command: str,
        args: list[str],
        workspace_root: str,
        *,
        server_name: str | None = None,
        sandbox_runner: SandboxRunner | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._workspace_root = workspace_root
        self._server_name = server_name or Path(command).name
        self._sandbox_runner = sandbox_runner or _lsp_sandbox_runner(workspace_root)
        self._process: asyncio.subprocess.Process | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._initialized = False
        self._opened_files: dict[str, tuple[int, str]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            if self._reader_task is not None and not self._reader_task.done():
                return
            # The server kept its process handle after the protocol reader
            # reached EOF.  Terminate that stale process before replacing it;
            # otherwise a direct ``start()`` call leaks the old server.
            try:
                await self._sandbox_runner.terminate(self._process)
            except (ProcessLookupError, OSError):
                pass
            for task_attr in ("_reader_task", "_stderr_task"):
                task = getattr(self, task_attr)
                if task is None or task.done():
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)
            self._process = None
            self._stdin = None
            self._stdout = None
        try:
            self._process = await self._sandbox_runner.spawn_interactive(
                [self._command, *self._args],
                container_argv=[self._server_name, *self._args],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workspace_root,
            )
            self._stdin = self._process.stdin
            self._stdout = self._process.stdout
        except FileNotFoundError:
            raise RuntimeError(f"Language server not found: {self._command}")
        except SandboxUnavailableError as exc:
            raise RuntimeError(f"Language server sandbox unavailable: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to start language server: {exc}")

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    async def stop(self) -> None:
        # Graceful LSP shutdown requires the protocol reader to deliver the
        # response.  If stdout already reached EOF, the process handle can
        # still report ``returncode is None`` while no task remains to settle
        # the shutdown future; sending a request then waits for the full
        # request timeout.  Skip directly to owned-process termination for
        # that stale lifecycle state.
        if self.is_running():
            try:
                for file_path in list(self._opened_files):
                    await self.close_file(file_path)
                await self._send_request("shutdown", {})
                await self._send_notification("exit", {})
            except Exception:
                pass
        if self._process is not None:
            try:
                await self._sandbox_runner.terminate(self._process)
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
        await self._send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": self._path_to_uri(self._workspace_root),
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
                {"uri": self._path_to_uri(self._workspace_root), "name": Path(self._workspace_root).name}
            ],
        })
        await self._send_notification("initialized", {})
        self._initialized = True

    async def _ensure_file_open(self, file_path: str) -> None:
        abs_path = str(Path(file_path).resolve())
        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"Unable to read source file for LSP: {abs_path}") from exc
        content_hash = __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()
        opened = self._opened_files.get(abs_path)
        if opened is not None:
            version, previous_hash = opened
            if previous_hash == content_hash:
                return
            version += 1
            await self._send_notification("textDocument/didChange", {
                "textDocument": {"uri": self._path_to_uri(abs_path), "version": version},
                "contentChanges": [{"text": content}],
            })
            self._opened_files[abs_path] = (version, content_hash)
            return
        ext = Path(abs_path).suffix.lstrip(".")
        lang_id = _language_id_for_extension(ext)
        await self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": self._path_to_uri(abs_path),
                "languageId": lang_id,
                "version": 1,
                "text": content,
            }
        })
        self._opened_files[abs_path] = (1, content_hash)

    async def close_file(self, file_path: str) -> None:
        """Notify the server that a tracked document is no longer active."""
        abs_path = str(Path(file_path).resolve())
        if abs_path not in self._opened_files:
            return
        await self._send_notification("textDocument/didClose", {
            "textDocument": {"uri": self._path_to_uri(abs_path)},
        })
        self._opened_files.pop(abs_path, None)

    async def definition(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/definition", {
            "textDocument": {"uri": self._path_to_uri(str(Path(file_path).resolve()))},
            "position": {"line": line, "character": character},
        })
        return _parse_locations(result, path_mapper=self._sandbox_runner.map_path_from_sandbox)

    async def references(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/references", {
            "textDocument": {"uri": self._path_to_uri(str(Path(file_path).resolve()))},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        })
        return _parse_locations(result, path_mapper=self._sandbox_runner.map_path_from_sandbox)

    async def hover(self, file_path: str, line: int, character: int) -> LSPHover | None:
        await self._ensure_file_open(file_path)
        result = await self._send_request("textDocument/hover", {
            "textDocument": {"uri": self._path_to_uri(str(Path(file_path).resolve()))},
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
            "textDocument": {"uri": self._path_to_uri(str(Path(file_path).resolve()))},
        })
        if not isinstance(result, list):
            return []
        symbols: list[LSPSymbol] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                symbols.append(_parse_symbol(item))
            except ValueError:
                logger.debug("Skipping malformed LSP document symbol")
        return symbols

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
        try:
            self._stdin.write(header.encode("utf-8") + body.encode("utf-8"))
            await self._stdin.drain()
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"LSP request timed out: {method}")
        except BaseException:
            self._pending.pop(msg_id, None)
            raise

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
        try:
            while True:
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
            raise
        except Exception as exc:
            logger.debug("LSP read loop error: %s", exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Language server connection closed"))
            self._pending.clear()

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

    def _path_to_uri(self, path: str) -> str:
        mapped = self._sandbox_runner.map_path_to_sandbox(path)
        if mapped.startswith("/"):
            return "file://" + quote(mapped, safe="/")
        return _path_to_uri(mapped)


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
        workspace_root = str(Path(file_path).resolve().parent)
        executable = _resolve_server_executable(server, workspace_root)
        if executable is None:
            return False
        return _lsp_sandbox_runner(workspace_root).capability().available

    async def get_client(self, file_path: str, workspace_root: str) -> LSPClient | None:
        server = self.server_for_file(file_path)
        if not server:
            return None
        workspace_root = str(Path(workspace_root).expanduser().resolve())
        executable = _resolve_server_executable(server, workspace_root)
        if executable is None:
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
                runner = _lsp_sandbox_runner(workspace_root)
                if not runner.capability().available:
                    logger.warning(
                        "LSP sandbox unavailable for %s: %s",
                        server,
                        runner.capability().reason,
                    )
                    return None
                client = LSPClient(
                    executable,
                    args,
                    workspace_root,
                    server_name=server,
                    sandbox_runner=runner,
                )
                try:
                    await client.start()
                except RuntimeError as exc:
                    logger.debug("LSP start failed for %s: %s", server, exc)
                    try:
                        await client.stop()
                    except Exception:
                        logger.debug("LSP cleanup failed after start error for %s", server, exc_info=True)
                    return None
                self._clients[key] = client
            return client

    async def close_file(self, file_path: str, workspace_root: str) -> None:
        server = self.server_for_file(file_path)
        if not server:
            return
        client = self._clients.get((workspace_root, server))
        if client is not None and client.is_running():
            await client.close_file(file_path)

    async def shutdown_all(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                try:
                    await client.stop()
                except Exception:
                    pass
            self._clients.clear()


# ── Helpers ─────────────────────────────────────────────────────────

def _is_within_path(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_server_executable(server: str, workspace_root: str) -> str | None:
    """Resolve from absolute non-workspace PATH entries only."""
    workspace = Path(workspace_root).expanduser().resolve()
    raw_path = str(sanitized_subprocess_env().get("PATH") or "")
    safe_entries: list[str] = []
    for raw_entry in raw_path.split(os.pathsep):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        candidate = Path(entry).expanduser()
        if not candidate.is_absolute():
            continue
        try:
            resolved_entry = candidate.resolve()
        except OSError:
            continue
        if _is_within_path(resolved_entry, workspace):
            continue
        safe_entries.append(str(resolved_entry))
    executable = shutil.which(server, path=os.pathsep.join(safe_entries))
    if not executable:
        return None
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError:
        return None
    if _is_within_path(resolved, workspace):
        return None
    return str(resolved)


def _lsp_sandbox_runner(workspace_root: str) -> SandboxRunner:
    workspace = Path(workspace_root).expanduser().resolve()
    return SandboxRunner(
        SandboxPolicy(
            workspace_root=workspace,
            writable_roots=(),
            readable_roots=(),
            # Codex's Windows restricted-token backend provides the required
            # filesystem boundary only for network-enabled policies unless its
            # elevated WFP layer is installed. LSP servers are trusted host
            # executables and need cross-platform availability, so use that
            # established Codex boundary instead of requiring a container.
            allow_network=sys.platform == "win32",
            timeout=0,
        )
    )

def _path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _uri_to_path(uri: str, *, path_mapper: Callable[[str], str] | None = None) -> str:
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        return uri
    path = unquote(parsed.path if parsed.scheme else uri)
    if path_mapper is not None:
        path = path_mapper(path)
    if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    return path.replace("/", os.sep) if os.name == "nt" else path


def _parse_locations(
    result: Any,
    *,
    path_mapper: Callable[[str], str] | None = None,
) -> list[LSPLocation]:
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
        start = (
            rng.get("start", {})
            if isinstance(rng, dict) and isinstance(rng.get("start", {}), dict)
            else {}
        )
        end = (
            rng.get("end", {})
            if isinstance(rng, dict) and isinstance(rng.get("end", {}), dict)
            else {}
        )
        line = _lsp_position_component(start, "line")
        character = _lsp_position_component(start, "character")
        end_line = _lsp_position_component(end, "line")
        end_character = _lsp_position_component(end, "character")
        if None in {line, character, end_line, end_character}:
            continue
        locations.append(LSPLocation(
            file=_uri_to_path(uri, path_mapper=path_mapper),
            line=line,
            character=character,
            end_line=end_line,
            end_character=end_character,
        ))
    return locations


def _parse_symbol(item: dict[str, Any]) -> LSPSymbol:
    location = item.get("location")
    location_range = location.get("range") if isinstance(location, dict) else None
    rng = location_range or item.get("range") or {}
    start = (
        rng.get("start", {})
        if isinstance(rng, dict) and isinstance(rng.get("start", {}), dict)
        else {}
    )
    end = (
        rng.get("end", {})
        if isinstance(rng, dict) and isinstance(rng.get("end", {}), dict)
        else {}
    )
    line = _lsp_position_component(start, "line")
    character = _lsp_position_component(start, "character")
    end_line = _lsp_position_component(end, "line")
    end_character = _lsp_position_component(end, "character")
    kind = item.get("kind", 0)
    if (
        line is None
        or character is None
        or end_line is None
        or end_character is None
        or isinstance(kind, bool)
        or not isinstance(kind, int)
        or kind < 0
        or kind > 9_007_199_254_740_991
    ):
        raise ValueError("malformed LSP symbol")
    children: list[LSPSymbol] = []
    raw_children = item.get("children")
    if isinstance(raw_children, list):
        for child in raw_children:
            if not isinstance(child, dict):
                continue
            try:
                children.append(_parse_symbol(child))
            except (ValueError, TypeError):
                logger.debug("Skipping malformed nested LSP symbol")
    return LSPSymbol(
        name=str(item.get("name") or ""),
        kind=kind,
        line=line,
        character=character,
        end_line=end_line,
        end_character=end_character,
        children=children,
    )


def _lsp_position_component(value: Any, field: str) -> int | None:
    if not isinstance(value, dict):
        return None
    if field not in value:
        return 0
    component = value[field]
    if isinstance(component, bool) or not isinstance(component, int) or component < 0:
        return None
    if component > 9_007_199_254_740_991:
        return None
    return component


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
