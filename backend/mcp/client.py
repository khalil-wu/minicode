"""MiniCode's product-facing MCP client.

The official ``mcp`` Python SDK owns MCP protocol framing. This module owns the
single MiniCode client lifecycle, transport selection, OAuth wiring, request
ownership and product projections.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import os
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.message import SessionMessage
from mcp.shared.exceptions import McpError
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import AnyUrl
from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Subprotocol

from backend.feature_flags import feature_enabled
from backend.mcp import MAX_MCP_INSTRUCTIONS_LENGTH, truncate_mcp_instructions
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain,
    cancel_and_drain_receipt,
)
from backend.mcp.oauth import MCPAuthenticationRequired
from backend.runtime_env import mcp_subprocess_env, sanitized_subprocess_env
from backend.security.unicode_sanitizer import (
    UnsafeUnicodeMetadataKey,
    sanitize_untrusted_metadata,
    sanitize_untrusted_unicode,
    unicode_identifier_is_safe,
)
from backend.subprocesses import communicate_bounded, spawn_shell

logger = logging.getLogger(__name__)

_MAX_PAGES = 100
_MAX_CATALOG_ITEMS = 2_048
_MAX_PAGINATION_CURSOR_BYTES = 64 * 1024
_MAX_MCP_RESPONSE_BYTES = 8 * 1024 * 1024

# MiniCode's own contract with a user-configured `headers_helper`: the helper is
# told which server it is being asked to authenticate.
_HEADERS_HELPER_ENV = ("MINICODE_MCP_SERVER_NAME", "MINICODE_MCP_SERVER_URL")


class _LifecycleClientSession(ClientSession):
    """Expose the SDK receive-loop close as a transport lifecycle event."""

    def __init__(
        self,
        *args: Any,
        transport_closed: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._transport_closed = transport_closed

    async def _receive_loop(self) -> None:
        try:
            await super()._receive_loop()
        finally:
            self._transport_closed.set()


@asynccontextmanager
async def _websocket_client_with_headers(
    url: str,
    headers: dict[str, str],
) -> AsyncGenerator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ],
    None,
]:
    """Open one MiniCode-owned MCP WebSocket transport."""

    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)
    async with ws_connect(
        url,
        subprotocols=[Subprotocol("mcp")],
        additional_headers=headers or None,
    ) as websocket:

        async def ws_reader() -> None:
            async with read_stream_writer:
                async for raw_text in websocket:
                    try:
                        message = types.JSONRPCMessage.model_validate_json(raw_text)
                        await read_stream_writer.send(SessionMessage(message))
                    except ValidationError as exc:
                        await read_stream_writer.send(exc)

        async def ws_writer() -> None:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump(
                        by_alias=True,
                        mode="json",
                        exclude_none=True,
                    )
                    await websocket.send(json.dumps(payload))

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(ws_reader)
            task_group.start_soon(ws_writer)
            yield read_stream, write_stream
            task_group.cancel_scope.cancel()


class MCPTransport(Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    WEBSOCKET = "ws"


@dataclass
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPResourceDef:
    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"


@dataclass
class MCPResourceTemplateDef:
    uri_template: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"


@dataclass
class MCPPromptArgDef:
    name: str
    description: str = ""
    required: bool = False


@dataclass
class MCPPromptDef:
    name: str
    description: str = ""
    arguments: list[MCPPromptArgDef] = field(default_factory=list)


@dataclass
class MCPCallResult:
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    @property
    def text(self) -> str:
        return "\n".join(
            str(item.get("text") or "")
            for item in self.content
            if isinstance(item, dict) and item.get("type") == "text"
        )

    @property
    def summary_text(self) -> str:
        text = self.text.strip()
        if text:
            return text
        parts: list[str] = []
        for item in self.content:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").strip().lower()
            if block_type == "image":
                mime = str(item.get("mimeType") or item.get("media_type") or "image")
                parts.append(f"[image content: {mime}]")
            elif block_type == "resource":
                resource = item.get("resource")
                if isinstance(resource, dict):
                    label = str(
                        resource.get("uri")
                        or resource.get("mimeType")
                        or resource.get("media_type")
                        or "resource"
                    )
                    parts.append(f"[resource content: {label}]")
                else:
                    parts.append("[resource content]")
            elif block_type:
                parts.append(f"[{block_type} content]")
        return "\n".join(parts)


@dataclass
class MCPServerCapabilities:
    tools: bool = False
    resources: bool = False
    resources_subscribe: bool = False
    resources_list_changed: bool = False
    prompts: bool = False
    logging: bool = False


class MCPAuthenticationError(ConnectionError):
    def __init__(self, message: str = "authentication required", *, expired: bool = False) -> None:
        super().__init__(message)
        self.mcp_auth_required = True
        self.mcp_auth_expired = bool(expired)


def _model_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, mode="json", exclude_none=True)
    return {}


def _payload_size_bytes(value: dict[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _bounded_response(server_name: str, method: str, value: Any) -> dict[str, Any]:
    payload = _model_dict(value)
    size = _payload_size_bytes(payload)
    if size > _MAX_MCP_RESPONSE_BYTES:
        raise ConnectionError(
            f"MCP server '{server_name}' returned {size} bytes for {method}, "
            f"exceeding the {_MAX_MCP_RESPONSE_BYTES}-byte response limit"
        )
    return payload


def _prompt_content_to_text(content: Any) -> str:
    if not isinstance(content, (str, dict, list)):
        content = _model_dict(content) or content
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" or "text" in content:
            return str(content.get("text") or "")
        resource = content.get("resource")
        if isinstance(resource, dict):
            return str(resource.get("text") or resource.get("uri") or "")
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        return "\n".join(filter(None, (_prompt_content_to_text(item).strip() for item in content)))
    return "" if content is None else str(content)


def _iter_exceptions(exc: BaseException):
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from _iter_exceptions(nested)
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        yield from _iter_exceptions(cause)


def _is_authentication_error(exc: BaseException) -> bool:
    for item in _iter_exceptions(exc):
        if isinstance(item, (MCPAuthenticationRequired, MCPAuthenticationError)):
            return True
        response = getattr(item, "response", None)
        if getattr(response, "status_code", None) in {401, 403}:
            return True
        if isinstance(item, McpError) and item.error.code in {401, 403, -32001}:
            return True
    return False


def _is_timeout_error(exc: BaseException) -> bool:
    for item in _iter_exceptions(exc):
        if isinstance(item, (TimeoutError, asyncio.TimeoutError)):
            return True
        if isinstance(item, McpError):
            if item.error.code == 408 or "timed out" in item.error.message.lower():
                return True
        if "timeout" in type(item).__name__.lower():
            return True
    return False


def _positive_timeout(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{field} must be a positive number")
    return float(value)


class MCPClient:
    """Stable MiniCode facade over the official MCP SDK client session."""

    CLIENT_INFO = {"name": "MiniCode", "version": "0.2.0"}
    PROTOCOL_VERSION = types.LATEST_PROTOCOL_VERSION

    def __init__(
        self,
        server_name: str,
        command: str = "python",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        transport: MCPTransport = MCPTransport.STDIO,
        url: str | None = None,
        token_store: Any = None,
        interactive_oauth: bool = False,
        elicitation_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        on_disconnect: Callable[[str], Awaitable[None]] | None = None,
        *,
        headers: dict[str, str] | None = None,
        headers_helper: str = "",
        oauth_client_id: str = "",
        oauth_callback_port: int | None = None,
        on_tools_changed: Callable[[str], Awaitable[None]] | None = None,
        startup_timeout: float | None = None,
        request_timeout: float | None = None,
        tool_timeout: float | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.server_name = server_name
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._headers = dict(headers or {})
        self._headers_helper = str(headers_helper or "").strip()
        self._oauth_client_id = str(oauth_client_id or "").strip()
        self._oauth_callback_port = oauth_callback_port
        self._cwd = cwd
        # Roots are owned by the client instance.  A client must never resolve
        # the process-global "active" workspace because reconnects and
        # concurrent sessions can otherwise expose another conversation's tree.
        self._workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )
        self._transport = transport
        self._url = (url or "").strip() or None
        self._startup_timeout = _positive_timeout(
            startup_timeout if startup_timeout is not None else 30.0,
            "startup_timeout",
        )
        self._request_timeout = _positive_timeout(
            request_timeout if request_timeout is not None else 60.0,
            "request_timeout",
        )
        self._tool_timeout = _positive_timeout(
            tool_timeout if tool_timeout is not None else 100_000.0,
            "tool_timeout",
        )
        self._token_store = token_store
        self._tokens = token_store.get(server_name) if token_store is not None else None
        self._interactive_oauth = interactive_oauth
        self._elicitation_handler = elicitation_handler
        self._on_disconnect = on_disconnect
        self._on_tools_changed = on_tools_changed

        self._session: ClientSession | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._close_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._oauth_callback: Any = None
        self._closing = False
        self._connected = False
        self._server_capabilities = MCPServerCapabilities()
        self._server_info: dict[str, Any] = {}
        self._instructions = ""
        self._subscribed_resources: set[str] = set()
        self._resource_notifications: list[dict[str, Any]] = []
        self._read_only_tools: set[str] = set()
        self._tool_owner_seq = itertools.count(1)
        self._active_tool_request_owners: dict[int, dict[str, Any]] = {}
        self._active_request_tasks: set[asyncio.Task[Any]] = set()
        self._disconnect_notified = False
        self._cleanup_pending = False
        self._cleanup_reason = ""
        self._cleanup_requested_at: float | None = None
        self._cleanup_completed_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def cleanup_status(self) -> dict[str, Any]:
        """Expose bounded shutdown evidence without leaking task ownership."""
        return {
            "pending": self._cleanup_pending,
            "reason": self._cleanup_reason,
            "requested_at": self._cleanup_requested_at,
            "completed_at": self._cleanup_completed_at,
        }

    @property
    def server_capabilities(self) -> MCPServerCapabilities:
        return self._server_capabilities

    @property
    def instructions(self) -> str:
        return self._instructions

    @property
    def has_valid_token(self) -> bool:
        return self._tokens is not None and not self._tokens.is_expired()

    def _set_server_instructions(self, value: Any) -> None:
        raw = str(value or "")
        sanitized = sanitize_untrusted_unicode(raw)
        self._instructions = truncate_mcp_instructions(sanitized)
        if sanitized != raw:
            logger.warning(
                "[MCP:%s] Removed unsafe Unicode from server instructions",
                self.server_name,
            )
        if len(sanitized) > MAX_MCP_INSTRUCTIONS_LENGTH:
            logger.warning(
                "[MCP:%s] Server instructions truncated from %d to %d characters",
                self.server_name,
                len(sanitized),
                MAX_MCP_INSTRUCTIONS_LENGTH,
            )

    async def connect(self) -> None:
        if self._connected:
            return
        self._closing = False
        self._disconnect_notified = False
        self._loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = self._loop.create_future()
        self._close_event = asyncio.Event()
        self._lifecycle_task = asyncio.create_task(
            self._run_sdk_lifecycle(ready, self._close_event),
            name=f"mcp-session-{self.server_name}",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(ready),
                timeout=max(self._startup_timeout, 1.0),
            )
        except BaseException:
            self._closing = True
            self._close_event.set()
            if self._lifecycle_task and not self._lifecycle_task.done():
                self._lifecycle_task.cancel()
            await asyncio.gather(self._lifecycle_task, return_exceptions=True)
            self._lifecycle_task = None
            raise

    async def _run_sdk_lifecycle(
        self,
        ready: asyncio.Future[None],
        close_event: asyncio.Event,
    ) -> None:
        try:
            transport_context = await self._sdk_transport_context()
            async with transport_context as streams:
                read_stream, write_stream = streams[0], streams[1]
                transport_closed = asyncio.Event()
                elicitation_callback = (
                    self._sdk_elicitation_callback
                    if feature_enabled("mcp_elicitation") and self._elicitation_handler is not None
                    else None
                )
                roots_callback = self._sdk_list_roots if feature_enabled("mcp_roots") else None
                async with _LifecycleClientSession(
                    read_stream,
                    write_stream,
                    transport_closed=transport_closed,
                    read_timeout_seconds=timedelta(seconds=self._request_timeout),
                    elicitation_callback=elicitation_callback,
                    list_roots_callback=roots_callback,
                    message_handler=self._sdk_message_handler,
                    client_info=types.Implementation(**self.CLIENT_INFO),
                ) as session:
                    self._session = session
                    initialized = await session.initialize()
                    self._apply_initialize_result(initialized)
                    self._connected = True
                    if not ready.done():
                        ready.set_result(None)
                    close_waiter = asyncio.create_task(close_event.wait())
                    transport_waiter = asyncio.create_task(transport_closed.wait())
                    try:
                        await asyncio.wait(
                            (close_waiter, transport_waiter),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if transport_closed.is_set() and not close_event.is_set():
                            raise ConnectionError(
                                f"MCP server '{self.server_name}' transport closed"
                            )
                    finally:
                        for waiter in (close_waiter, transport_waiter):
                            if not waiter.done():
                                waiter.cancel()
                        await asyncio.gather(
                            close_waiter,
                            transport_waiter,
                            return_exceptions=True,
                        )
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            mapped = self._map_connection_error(exc)
            if not ready.done():
                ready.set_exception(mapped)
            elif not self._closing:
                self._connected = False
                logger.warning("[MCP:%s] session ended: %s", self.server_name, mapped)
                await self._notify_disconnect()
        finally:
            self._connected = False
            self._session = None
            await self._close_oauth_callback()

    async def _close_oauth_callback(self) -> bool:
        callback = self._oauth_callback
        if callback is None:
            return True
        try:
            await callback.close()
        except Exception:
            self._cleanup_pending = True
            self._cleanup_reason = "oauth_callback_close_failed"
            return False
        self._oauth_callback = None
        return True

    async def _sdk_transport_context(self) -> Any:
        if self._transport == MCPTransport.STDIO:
            from backend.config import PROJECT_ROOT

            command = sys.executable if self._command == "python" else self._command
            # Local MCP processes receive the platform core plus variables and
            # values explicitly selected by the MiniCode server config.
            env = mcp_subprocess_env(self._env)
            env = self._fix_stdio_pythonpath(env)
            return stdio_client(
                StdioServerParameters(
                    command=command,
                    args=self._args,
                    env=env,
                    cwd=self._cwd or None,
                )
            )
        if not self._url:
            raise ConnectionError(f"MCP transport '{self._transport.value}' requires a URL")
        headers = await self._resolved_http_headers()
        if self._transport == MCPTransport.SSE:
            return sse_client(
                self._url,
                headers=headers,
                timeout=self._request_timeout,
                sse_read_timeout=max(self._request_timeout, self._tool_timeout),
                auth=await self._http_oauth_auth(),
            )
        if self._transport == MCPTransport.HTTP:
            if not feature_enabled("mcp_streamable_http_transport"):
                raise ConnectionError("MCP Streamable HTTP transport is disabled by feature flag")
            return streamablehttp_client(
                self._url,
                headers=headers,
                timeout=self._request_timeout,
                sse_read_timeout=max(self._request_timeout, self._tool_timeout),
                auth=await self._http_oauth_auth(),
            )
        if self._transport == MCPTransport.WEBSOCKET:
            if not feature_enabled("mcp_websocket_transport"):
                raise ConnectionError("MCP WebSocket transport is disabled by feature flag")
            return _websocket_client_with_headers(self._url, headers)
        raise ConnectionError(f"Unsupported MCP transport: {self._transport.value}")

    @staticmethod
    def _fix_stdio_pythonpath(env: dict[str, str]) -> dict[str, str]:
        """Prevent MiniCode's ``backend/mcp`` package shadowing the SDK."""
        from backend.config import PROJECT_ROOT

        backend_dir = os.path.normcase(os.path.abspath(PROJECT_ROOT / "backend"))
        parts = []
        for entry in env.get("PYTHONPATH", "").split(os.pathsep):
            if not entry:
                continue
            if os.path.normcase(os.path.abspath(entry)) != backend_dir:
                parts.append(entry)
        project_root = str(PROJECT_ROOT)
        if project_root not in parts:
            parts.append(project_root)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    async def _http_oauth_auth(self) -> Any:
        if self._token_store is None or not self._url:
            return None
        from backend.mcp.oauth import create_sdk_oauth_provider

        provider, callback = await create_sdk_oauth_provider(
            self._url,
            self.server_name,
            self._token_store,
            interactive=self._interactive_oauth,
            client_id=self._oauth_client_id,
            callback_port=self._oauth_callback_port,
        )
        self._oauth_callback = callback
        return provider

    def _http_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._tokens is not None and not self._tokens.is_expired():
            token = str(getattr(self._tokens, "access_token", "") or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _resolved_http_headers(self) -> dict[str, str]:
        headers = self._http_headers()
        if not self._headers_helper:
            return headers
        name_key, url_key = _HEADERS_HELPER_ENV
        server_url = str(self._url or "")
        env = sanitized_subprocess_env({
            name_key: self.server_name,
            url_key: server_url,
        })
        process = await spawn_shell(
            self._headers_helper,
            cwd=self._cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await communicate_bounded(
            process,
            timeout=10.0,
            stdout_limit_bytes=1024 * 1024,
            stderr_limit_bytes=1024 * 1024,
        )
        if process.returncode != 0 or not stdout:
            raise ValueError("headers_helper did not return a valid value")
        payload = json.loads(stdout.decode("utf-8", errors="strict").strip())
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in payload.items()
        ):
            raise ValueError("headers_helper must return a JSON object of string values")
        headers.update(payload)
        return headers

    def _map_connection_error(self, exc: BaseException) -> BaseException:
        if isinstance(exc, MCPAuthenticationError):
            return exc
        if _is_authentication_error(exc):
            expired = self._tokens is not None and self._tokens.is_expired()
            return MCPAuthenticationError(str(exc) or "authentication required", expired=expired)
        if isinstance(exc, ConnectionError):
            return exc
        return ConnectionError(f"MCP server '{self.server_name}' connection failed: {exc}")

    def _apply_initialize_result(self, result: Any) -> None:
        raw = _model_dict(result)
        caps = getattr(result, "capabilities", None)
        if caps is not None and not isinstance(caps, dict):
            resources = getattr(caps, "resources", None)
            self._server_capabilities = MCPServerCapabilities(
                tools=getattr(caps, "tools", None) is not None,
                resources=resources is not None,
                resources_subscribe=bool(getattr(resources, "subscribe", False)),
                resources_list_changed=bool(getattr(resources, "listChanged", False)),
                prompts=getattr(caps, "prompts", None) is not None,
                logging=getattr(caps, "logging", None) is not None,
            )
        else:
            caps_dict = raw.get("capabilities", {}) if isinstance(raw, dict) else {}
            resources = caps_dict.get("resources")
            self._server_capabilities = MCPServerCapabilities(
                tools="tools" in caps_dict and caps_dict.get("tools") is not False,
                resources="resources" in caps_dict and resources is not False,
                resources_subscribe=isinstance(resources, dict) and bool(resources.get("subscribe")),
                resources_list_changed=isinstance(resources, dict) and bool(resources.get("listChanged")),
                prompts="prompts" in caps_dict and caps_dict.get("prompts") is not False,
                logging="logging" in caps_dict and caps_dict.get("logging") is not False,
            )
        server_info = getattr(result, "serverInfo", None)
        self._server_info = _model_dict(server_info) or dict(raw.get("serverInfo") or {})
        self._set_server_instructions(getattr(result, "instructions", raw.get("instructions", "")))

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        params = params or {}
        if method == "tools/list":
            result = await session.list_tools(cursor=params.get("cursor"))
        elif method == "tools/call":
            result = await session.call_tool(
                str(params.get("name") or ""),
                dict(params.get("arguments") or {}),
                read_timeout_seconds=timedelta(seconds=self._tool_timeout),
                meta=dict(params.get("_meta") or {}) or None,
            )
        elif method == "resources/list":
            result = await session.list_resources(cursor=params.get("cursor"))
        elif method == "resources/templates/list":
            result = await session.list_resource_templates(cursor=params.get("cursor"))
        elif method == "resources/read":
            result = await session.read_resource(AnyUrl(str(params["uri"])))
        elif method == "resources/subscribe":
            result = await session.subscribe_resource(AnyUrl(str(params["uri"])))
        elif method == "resources/unsubscribe":
            result = await session.unsubscribe_resource(AnyUrl(str(params["uri"])))
        elif method == "prompts/list":
            result = await session.list_prompts(cursor=params.get("cursor"))
        elif method == "prompts/get":
            arguments = {str(k): str(v) for k, v in dict(params.get("arguments") or {}).items()}
            result = await session.get_prompt(str(params.get("name") or ""), arguments)
        else:
            raise ValueError(f"Unsupported MCP facade operation: {method}")
        return _bounded_response(self.server_name, method, result)

    async def _paged(self, method: str, result_key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        total_bytes = 0
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = await self._request(method, params)
            total_bytes += _payload_size_bytes(result)
            if total_bytes > _MAX_MCP_RESPONSE_BYTES:
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned a {method} catalog "
                    f"exceeding the {_MAX_MCP_RESPONSE_BYTES}-byte aggregate limit"
                )
            page_items = [
                item for item in result.get(result_key, []) if isinstance(item, dict)
            ]
            if len(items) + len(page_items) > _MAX_CATALOG_ITEMS:
                raise ConnectionError(
                    f"MCP server '{self.server_name}' exceeded the catalog limit of "
                    f"{_MAX_CATALOG_ITEMS} items"
                )
            items.extend(page_items)
            next_cursor = str(result.get("nextCursor") or "").strip() or None
            if next_cursor is None:
                return items
            if len(next_cursor.encode("utf-8")) > _MAX_PAGINATION_CURSOR_BYTES:
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned a pagination cursor "
                    f"exceeding {_MAX_PAGINATION_CURSOR_BYTES} bytes"
                )
            if next_cursor in seen:
                raise ConnectionError(f"MCP server '{self.server_name}' repeated pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ConnectionError(f"MCP server '{self.server_name}' exceeded pagination limit")

    async def list_tools(self) -> list[MCPToolDef]:
        if not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        raw_tools = await self._paged("tools/list", "tools")
        tools: list[MCPToolDef] = []
        for item in raw_tools:
            name = str(item.get("name") or "")
            if not unicode_identifier_is_safe(name):
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned an unsafe tool identifier"
                )
            raw_schema = item.get("inputSchema") or {}
            if not isinstance(raw_schema, dict):
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned a non-object schema for tool '{name}'"
                )
            try:
                input_schema = sanitize_untrusted_metadata(
                    raw_schema,
                    reject_unsafe_keys=True,
                )
            except UnsafeUnicodeMetadataKey as exc:
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned unsafe schema metadata for tool '{name}'"
                ) from exc
            annotations = item.get("annotations") or {}
            meta = item.get("_meta") or {}
            tools.append(
                MCPToolDef(
                    name=name,
                    description=sanitize_untrusted_unicode(item.get("description") or ""),
                    input_schema=input_schema,
                    annotations=(
                        sanitize_untrusted_metadata(annotations)
                        if isinstance(annotations, dict)
                        else {}
                    ),
                    meta=(
                        sanitize_untrusted_metadata(meta)
                        if isinstance(meta, dict)
                        else {}
                    ),
                )
            )
        self._read_only_tools = {
            tool.name for tool in tools if bool(tool.annotations.get("readOnlyHint", False))
        }
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        request_owner: dict[str, Any] | None = None,
        request_meta: dict[str, Any] | None = None,
    ) -> MCPCallResult:
        if not self._connected:
            return self._tool_error(f"MCP server '{self.server_name}' is not connected")
        params: dict[str, Any] = {"name": tool_name, "arguments": arguments or {}}
        if request_meta:
            params["_meta"] = dict(request_meta)
        owner_key = next(self._tool_owner_seq)
        if request_owner:
            self._active_tool_request_owners[owner_key] = dict(request_owner)
        request_task: asyncio.Task[dict[str, Any]] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        try:
            try:
                cancel_event = (
                    request_owner.get("cancel_event")
                    if isinstance(request_owner, dict)
                    else None
                )
                request_task = asyncio.create_task(self._request("tools/call", params))
                self._active_request_tasks.add(request_task)
                request_task.add_done_callback(self._active_request_tasks.discard)
                if isinstance(cancel_event, asyncio.Event):
                    cancel_task = asyncio.create_task(cancel_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {request_task, cancel_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if cancel_task in done and cancel_event.is_set():
                            await cancel_and_drain(
                                [request_task],
                                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                                label=f"MCP tool request {self.server_name}/{tool_name}",
                            )
                            raise asyncio.CancelledError
                        result = request_task.result()
                    finally:
                        cancel_task.cancel()
                        await asyncio.gather(cancel_task, return_exceptions=True)
                else:
                    result = await request_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._tool_exception(exc)
        finally:
            self._active_tool_request_owners.pop(owner_key, None)
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            if request_task is not None and not request_task.done():
                # Outer cancellation must propagate to the SDK request and
                # retain its task in _active_request_tasks until true exit.
                await cancel_and_drain(
                    [request_task],
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label=f"MCP tool request {self.server_name}/{tool_name}",
                )
        return MCPCallResult(
            content=list(result.get("content") or []),
            is_error=bool(result.get("isError", False)),
        )

    def _tool_exception(self, exc: BaseException) -> MCPCallResult:
        if _is_timeout_error(exc):
            return self._tool_error("Tool call timed out")
        if isinstance(exc, McpError):
            return self._tool_error(f"MCP RPC error: {exc.error.message} (code={exc.error.code})")
        self._mark_disconnected()
        return self._tool_error(f"MCP transport error: {exc}")

    @staticmethod
    def _tool_error(message: str) -> MCPCallResult:
        return MCPCallResult(content=[{"type": "text", "text": message}], is_error=True)

    async def list_resources(self) -> list[MCPResourceDef]:
        if not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        if not self._server_capabilities.resources:
            return []
        resources: list[MCPResourceDef] = []
        for item in await self._paged("resources/list", "resources"):
            uri = str(item.get("uri") or "")
            if not unicode_identifier_is_safe(uri):
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned an unsafe resource URI"
                )
            resources.append(
                MCPResourceDef(
                    uri=uri,
                    name=sanitize_untrusted_unicode(item.get("name") or ""),
                    description=sanitize_untrusted_unicode(item.get("description") or ""),
                    mime_type=sanitize_untrusted_unicode(item.get("mimeType") or "text/plain"),
                )
            )
        return resources

    async def list_resource_templates(self) -> list[MCPResourceTemplateDef]:
        if not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        if not self._server_capabilities.resources:
            return []
        templates: list[MCPResourceTemplateDef] = []
        for item in await self._paged("resources/templates/list", "resourceTemplates"):
            uri_template = str(item.get("uriTemplate") or item.get("uri_template") or "")
            if not unicode_identifier_is_safe(uri_template):
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned an unsafe resource template URI"
                )
            templates.append(
                MCPResourceTemplateDef(
                    uri_template=uri_template,
                    name=sanitize_untrusted_unicode(item.get("name") or uri_template),
                    description=sanitize_untrusted_unicode(item.get("description") or ""),
                    mime_type=sanitize_untrusted_unicode(
                        item.get("mimeType") or item.get("mime_type") or "text/plain"
                    ),
                )
            )
        return templates

    async def read_resource(self, uri: str) -> str:
        if not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        result = await self._request("resources/read", {"uri": uri})
        parts: list[str] = []
        for content in result.get("contents", []):
            if not isinstance(content, dict):
                continue
            if "text" in content:
                parts.append(str(content.get("text") or ""))
            elif "blob" in content:
                parts.append(f"[binary resource: {content.get('mimeType') or 'application/octet-stream'}]")
        return "\n".join(parts)

    async def subscribe_resource(self, uri: str) -> bool:
        if not self._connected or not self._server_capabilities.resources_subscribe:
            return False
        await self._request("resources/subscribe", {"uri": uri})
        self._subscribed_resources.add(uri)
        return True

    async def unsubscribe_resource(self, uri: str) -> bool:
        if not self._connected or not self._server_capabilities.resources_subscribe:
            return False
        await self._request("resources/unsubscribe", {"uri": uri})
        self._subscribed_resources.discard(uri)
        return True

    def list_resource_subscriptions(self) -> list[str]:
        return sorted(self._subscribed_resources)

    def consume_resource_notifications(self) -> list[dict[str, Any]]:
        result = list(self._resource_notifications)
        self._resource_notifications.clear()
        return result

    async def list_prompts(self) -> list[MCPPromptDef]:
        if not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        if not self._server_capabilities.prompts:
            return []
        prompts: list[MCPPromptDef] = []
        for item in await self._paged("prompts/list", "prompts"):
            name = str(item.get("name") or "")
            if not unicode_identifier_is_safe(name):
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned an unsafe prompt identifier"
                )
            arguments: list[MCPPromptArgDef] = []
            unsafe_argument = False
            for arg in item.get("arguments", []):
                if not isinstance(arg, dict) or not arg.get("name"):
                    continue
                argument_name = str(arg.get("name") or "")
                if not unicode_identifier_is_safe(argument_name):
                    unsafe_argument = True
                    break
                arguments.append(
                    MCPPromptArgDef(
                        name=argument_name,
                        description=sanitize_untrusted_unicode(arg.get("description") or ""),
                        required=bool(arg.get("required", False)),
                    )
                )
            if unsafe_argument:
                raise ConnectionError(
                    f"MCP server '{self.server_name}' returned unsafe arguments for prompt '{name}'"
                )
            prompts.append(
                MCPPromptDef(
                    name,
                    sanitize_untrusted_unicode(item.get("description") or ""),
                    arguments,
                )
            )
        return prompts

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")
        result = await self._request("prompts/get", {"name": name, "arguments": arguments or {}})
        lines: list[str] = []
        description = sanitize_untrusted_unicode(result.get("description") or "").strip()
        if description:
            lines.append(description)
        for message in result.get("messages", []):
            if not isinstance(message, dict):
                continue
            content = sanitize_untrusted_unicode(
                _prompt_content_to_text(message.get("content"))
            ).strip()
            if content:
                role = sanitize_untrusted_unicode(message.get("role") or "user")
                lines.append(f"{role}: {content}")
        return "\n\n".join(lines).strip()

    async def _sdk_list_roots(self, _context: Any) -> types.ListRootsResult | types.ErrorData:
        if not feature_enabled("mcp_roots"):
            return types.ErrorData(code=types.INVALID_REQUEST, message="roots not supported")
        return types.ListRootsResult(roots=[types.Root.model_validate(root) for root in self._client_roots()])

    async def _sdk_elicitation_callback(self, context: Any, params: Any) -> Any:
        if not feature_enabled("mcp_elicitation") or self._elicitation_handler is None:
            return types.ErrorData(code=types.INVALID_REQUEST, message="elicitation not supported")
        owner = self._active_callback_owner()
        if owner is None:
            return types.ErrorData(code=-32002, message="elicitation request has no unambiguous active turn owner")
        payload = _model_dict(params)
        payload.setdefault("prompt", payload.get("message", ""))
        payload.setdefault("schema", payload.get("requestedSchema", {}))
        payload.update(
            _minicode_owner=owner,
            _mcp_server_name=self.server_name,
            _mcp_request_id=str(context.request_id),
        )
        try:
            result = await self._elicitation_handler(payload)
            action = str(result.get("action") or "cancel")
            if action == "submit":
                result = {"action": "accept", "content": result.get("response") or {}}
            elif action not in {"accept", "decline", "cancel"}:
                result = {"action": "cancel"}
            return types.ElicitResult.model_validate(result)
        except PermissionError as exc:
            return types.ErrorData(code=-32003, message=str(exc) or "request rejected")
        except Exception:
            logger.debug("[MCP:%s] elicitation callback failed", self.server_name, exc_info=True)
            return types.ErrorData(code=types.INTERNAL_ERROR, message="client request failed")

    async def _sdk_message_handler(self, message: Any) -> None:
        if isinstance(message, Exception):
            self._mark_disconnected()
            raise ConnectionError(
                f"MCP server '{self.server_name}' transport message failed: {message}"
            ) from message
        notification = getattr(message, "root", None)
        if isinstance(notification, types.ResourceUpdatedNotification):
            params = _model_dict(notification.params)
            self._record_resource_notification("notifications/resources/updated", params)
        elif isinstance(notification, types.ResourceListChangedNotification):
            params = _model_dict(notification.params)
            self._record_resource_notification("notifications/resources/list_changed", params)
        elif isinstance(notification, types.ToolListChangedNotification):
            if self._on_tools_changed is not None:
                await self._on_tools_changed(self.server_name)
        elif isinstance(notification, types.PromptListChangedNotification):
            logger.debug("[MCP:%s] prompts/list_changed", self.server_name)
        elif isinstance(notification, types.LoggingMessageNotification):
            params = _model_dict(notification.params)
            logger.info(
                "[MCP:%s] server log %s: %s",
                self.server_name,
                params.get("level") or "info",
                params.get("data") or "",
            )

    def _record_resource_notification(self, method: str, params: dict[str, Any]) -> None:
        self._resource_notifications.append(
            {"method": method, "uri": str(params.get("uri") or ""), "params": params}
        )
        if len(self._resource_notifications) > 100:
            del self._resource_notifications[:-100]

    def _active_callback_owner(self) -> dict[str, Any] | None:
        owners: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for owner in self._active_tool_request_owners.values():
            identity = (
                str(owner.get("session_id") or ""),
                str(owner.get("conversation_id") or ""),
                str(owner.get("task_id") or ""),
                str(owner.get("run_id") or ""),
            )
            if identity[0] and identity[1]:
                owners[identity] = owner
        return dict(next(iter(owners.values()))) if len(owners) == 1 else None

    def _client_roots(self) -> list[dict[str, str]]:
        # Roots belong to this client's explicit workspace owner. With no owner,
        # do not consult process-global workspace state.
        candidates: list[Path] = []
        if self._workspace_root is not None:
            candidates.append(self._workspace_root)
        roots: list[dict[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                seen.add(key)
                roots.append({"uri": resolved.as_uri(), "name": resolved.name or str(resolved)})
            except Exception as exc:
                raise ConnectionError(
                    f"MCP workspace root cannot be resolved: {candidate}: {exc}"
                ) from exc
        return roots

    async def _notify_disconnect(self) -> None:
        if self._on_disconnect is None or self._closing or self._disconnect_notified:
            return
        self._disconnect_notified = True
        await self._on_disconnect(self.server_name)

    def _mark_disconnected(self) -> None:
        was_connected = self._connected
        self._connected = False
        if was_connected and not self._closing:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._notify_disconnect())
            except RuntimeError:
                if self._loop is not None and self._loop.is_running():
                    self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._notify_disconnect()))

    async def close(self) -> bool:
        self._cleanup_requested_at = time.time()
        self._cleanup_completed_at = None
        self._cleanup_reason = "close"
        self._closing = True
        self._connected = False
        self._active_tool_request_owners.clear()
        # Drain active requests before closing their transport lifecycle.
        request_tasks = set(self._active_request_tasks)
        if request_tasks:
            receipt = await cancel_and_drain_receipt(
                request_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"MCP {self.server_name} active requests",
            )
            if receipt.pending:
                self._cleanup_pending = True
                self._cleanup_reason = "active_requests_pending"
                return False
        if self._close_event is not None:
            self._close_event.set()
        task = self._lifecycle_task
        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(self._request_timeout, 3.0),
                )
            except asyncio.TimeoutError:
                receipt = await cancel_and_drain_receipt(
                    [task],
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label=f"MCP {self.server_name} lifecycle",
                )
                if receipt.pending:
                    self._cleanup_pending = True
                    self._cleanup_reason = "lifecycle_pending"
                    return False
        if not await self._close_oauth_callback():
            return False
        self._lifecycle_task = None
        self._close_event = None
        self._session = None
        self._cleanup_pending = False
        self._cleanup_completed_at = time.time()
        return True

    async def finish_pending_cleanup(self) -> bool:
        """Retain ownership until cancelled requests settle, then close again."""
        request_tasks = set(self._active_request_tasks)
        if request_tasks:
            await asyncio.gather(*request_tasks, return_exceptions=True)
        return await self.close()
