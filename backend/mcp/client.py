"""MiniCode's product-facing MCP client.

All standard MCP protocol and transport work is delegated to the official
``mcp`` Python SDK.  This module only owns MiniCode-specific concerns:
normalised product models, OAuth/token wiring, callback ownership fencing and
the deliberately small in-process test/plugin adapter.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import itertools
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.websocket import websocket_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from backend.feature_flags import feature_enabled
from backend.mcp import MAX_MCP_INSTRUCTIONS_LENGTH, truncate_mcp_instructions
from backend.mcp.oauth import MCPAuthenticationRequired
from backend.runtime_env import sanitized_subprocess_env

logger = logging.getLogger(__name__)

RETRIABLE_TIMEOUT_TOOLS = {
    "search",
    "fetch_page",
    "mcp__websearch__search",
    "mcp__websearch__fetch_page",
}
_MAX_PAGES = 1_000


class MCPTransport(Enum):
    STDIO = "stdio"
    HTTP = "http"  # Legacy MCP SSE transport.
    WEBSOCKET = "websocket"
    STREAMABLE_HTTP = "streamable_http"
    IN_PROCESS = "in_process"


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
        timeout: float = 30.0,
        token_store: Any = None,
        interactive_oauth: bool = False,
        sampling_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        elicitation_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        server_factory: Callable[[], dict[str, Any]] | None = None,
        on_disconnect: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.server_name = server_name
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._cwd = cwd
        self._transport = transport
        self._url = (url or "").strip() or None
        self._timeout = float(timeout)
        self._token_store = token_store
        self._tokens = token_store.get(server_name) if token_store is not None else None
        self._interactive_oauth = interactive_oauth
        self._sampling_handler = sampling_handler
        self._elicitation_handler = elicitation_handler
        self._server_factory = server_factory
        self._on_disconnect = on_disconnect

        self._session: ClientSession | None = None
        self._in_process_server: dict[str, Any] | None = None
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
        self._disconnect_notified = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_capabilities(self) -> MCPServerCapabilities:
        return self._server_capabilities

    @property
    def instructions(self) -> str:
        return self._instructions

    @property
    def has_valid_token(self) -> bool:
        return self._tokens is not None and not self._tokens.is_expired()

    def set_tokens(self, tokens: Any) -> None:
        self._tokens = tokens
        if self._token_store is not None and tokens is not None:
            self._token_store.set(self.server_name, tokens)

    def clear_tokens(self) -> None:
        self._tokens = None
        if self._token_store is not None:
            self._token_store.clear(self.server_name)

    def _set_server_instructions(self, value: Any) -> None:
        raw = str(value or "")
        self._instructions = truncate_mcp_instructions(raw)
        if len(raw) > MAX_MCP_INSTRUCTIONS_LENGTH:
            logger.warning(
                "[MCP:%s] Server instructions truncated from %d to %d characters",
                self.server_name,
                len(raw),
                MAX_MCP_INSTRUCTIONS_LENGTH,
            )

    async def connect(self) -> None:
        if self._connected:
            return
        self._closing = False
        self._disconnect_notified = False
        self._loop = asyncio.get_running_loop()
        if self._transport == MCPTransport.IN_PROCESS:
            await self._connect_in_process()
            return

        ready: asyncio.Future[None] = self._loop.create_future()
        self._close_event = asyncio.Event()
        self._lifecycle_task = asyncio.create_task(
            self._run_sdk_lifecycle(ready, self._close_event),
            name=f"mcp-session-{self.server_name}",
        )
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=max(self._timeout, 1.0))
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
                sampling_callback = (
                    self._sdk_sampling_callback
                    if feature_enabled("mcp_sampling") and self._sampling_handler is not None
                    else None
                )
                elicitation_callback = (
                    self._sdk_elicitation_callback
                    if feature_enabled("mcp_elicitation") and self._elicitation_handler is not None
                    else None
                )
                roots_callback = self._sdk_list_roots if feature_enabled("mcp_roots") else None
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._timeout),
                    sampling_callback=sampling_callback,
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
                    await close_event.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            mapped = self._map_connection_error(exc)
            if not ready.done():
                ready.set_exception(mapped)
            elif not self._closing:
                logger.warning("[MCP:%s] session ended: %s", self.server_name, mapped)
                await self._notify_disconnect()
        finally:
            self._connected = False
            self._session = None
            if self._oauth_callback is not None:
                try:
                    await self._oauth_callback.close()
                except Exception:
                    logger.debug("[MCP:%s] OAuth callback close failed", self.server_name, exc_info=True)
                self._oauth_callback = None

    async def _sdk_transport_context(self) -> Any:
        if self._transport == MCPTransport.STDIO:
            from backend.config import PROJECT_ROOT

            command = sys.executable if self._command == "python" else self._command
            env = sanitized_subprocess_env(self._env)
            env = self._fix_stdio_pythonpath(env)
            return stdio_client(
                StdioServerParameters(
                    command=command,
                    args=self._args,
                    env=env,
                    cwd=self._cwd or str(PROJECT_ROOT),
                )
            )
        if not self._url:
            raise ConnectionError(f"MCP transport '{self._transport.value}' requires a URL")
        if self._transport == MCPTransport.HTTP:
            return sse_client(
                self._url,
                headers=self._http_headers(),
                timeout=self._timeout,
                auth=await self._http_oauth_auth(),
            )
        if self._transport == MCPTransport.STREAMABLE_HTTP:
            if not feature_enabled("mcp_streamable_http_transport"):
                raise ConnectionError("MCP Streamable HTTP transport is disabled by feature flag")
            return streamablehttp_client(
                self._url,
                headers=self._http_headers(),
                timeout=self._timeout,
                auth=await self._http_oauth_auth(),
            )
        if self._transport == MCPTransport.WEBSOCKET:
            if not feature_enabled("mcp_websocket_transport"):
                raise ConnectionError("MCP WebSocket transport is disabled by feature flag")
            return websocket_client(self._url)
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
        )
        self._oauth_callback = callback
        return provider

    def _http_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._tokens is not None and not self._tokens.is_expired():
            token = str(getattr(self._tokens, "access_token", "") or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
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

    async def _connect_in_process(self) -> None:
        if self._server_factory is not None:
            server = self._server_factory()
        elif self._args:
            module = importlib.import_module(self._args[0])
            factory = getattr(module, "create_mcp_server", None)
            if factory is None:
                raise ConnectionError(f"Module {self._args[0]} has no create_mcp_server()")
            server = factory()
        else:
            raise ConnectionError("In-process transport requires server_factory or args[0]")
        if not isinstance(server, dict) or not callable(server.get("handle_request")):
            raise ConnectionError("In-process server is missing handle_request")
        self._in_process_server = server
        initialized = await self._in_process_request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": self._product_client_capabilities(),
                "clientInfo": self.CLIENT_INFO,
            },
        )
        self._apply_initialize_result(initialized)
        notify = server.get("handle_notification")
        if callable(notify):
            await notify("notifications/initialized", {})
        self._connected = True

    def _product_client_capabilities(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if feature_enabled("mcp_roots"):
            result["roots"] = {"listChanged": True}
        if feature_enabled("mcp_sampling") and self._sampling_handler is not None:
            result["sampling"] = {}
        if feature_enabled("mcp_elicitation") and self._elicitation_handler is not None:
            result["elicitation"] = {}
        return result

    async def _in_process_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        server = self._in_process_server
        if server is None:
            raise ConnectionError("In-process server is not connected")
        result = await server["handle_request"](method, params or {})
        return _model_dict(result) if not isinstance(result, dict) else result

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._transport == MCPTransport.IN_PROCESS:
            return await self._in_process_request(method, params)
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
                read_timeout_seconds=timedelta(seconds=self._timeout),
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
        return _model_dict(result)

    async def _paged(self, method: str, result_key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = await self._request(method, params)
            items.extend(item for item in result.get(result_key, []) if isinstance(item, dict))
            next_cursor = str(result.get("nextCursor") or "").strip() or None
            if next_cursor is None:
                return items
            if next_cursor in seen:
                raise ConnectionError(f"MCP server '{self.server_name}' repeated pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ConnectionError(f"MCP server '{self.server_name}' exceeded pagination limit")

    async def list_tools(self) -> list[MCPToolDef]:
        if not self._connected:
            return []
        raw_tools = await self._paged("tools/list", "tools")
        tools = [
            MCPToolDef(
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                input_schema=dict(item.get("inputSchema") or {}),
                annotations=dict(item.get("annotations") or {}),
                meta=dict(item.get("_meta") or {}),
            )
            for item in raw_tools
            if item.get("name")
        ]
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
        try:
            may_retry = tool_name in RETRIABLE_TIMEOUT_TOOLS and tool_name in self._read_only_tools
            for attempt in range(2 if may_retry else 1):
                try:
                    result = await self._request("tools/call", params)
                    break
                except BaseException as exc:
                    if attempt == 0 and may_retry and _is_timeout_error(exc):
                        logger.warning(
                            "[MCP:%s] Tool call timed out, retrying once: %s",
                            self.server_name,
                            tool_name,
                        )
                        continue
                    return self._tool_exception(exc)
            else:  # pragma: no cover - the bounded loop always returns or breaks
                return self._tool_error("Tool call failed")
        finally:
            self._active_tool_request_owners.pop(owner_key, None)
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
        if not self._connected or not self._server_capabilities.resources:
            return []
        return [
            MCPResourceDef(
                uri=str(item.get("uri") or ""),
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                mime_type=str(item.get("mimeType") or "text/plain"),
            )
            for item in await self._paged("resources/list", "resources")
            if item.get("uri")
        ]

    async def list_resource_templates(self) -> list[MCPResourceTemplateDef]:
        if not self._connected or not self._server_capabilities.resources:
            return []
        return [
            MCPResourceTemplateDef(
                uri_template=str(item.get("uriTemplate") or item.get("uri_template") or ""),
                name=str(item.get("name") or item.get("uriTemplate") or ""),
                description=str(item.get("description") or ""),
                mime_type=str(item.get("mimeType") or item.get("mime_type") or "text/plain"),
            )
            for item in await self._paged("resources/templates/list", "resourceTemplates")
            if item.get("uriTemplate") or item.get("uri_template")
        ]

    async def read_resource(self, uri: str) -> str:
        if not self._connected:
            return ""
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
        if not self._connected or not self._server_capabilities.prompts:
            return []
        prompts: list[MCPPromptDef] = []
        for item in await self._paged("prompts/list", "prompts"):
            arguments = [
                MCPPromptArgDef(
                    name=str(arg.get("name") or ""),
                    description=str(arg.get("description") or ""),
                    required=bool(arg.get("required", False)),
                )
                for arg in item.get("arguments", [])
                if isinstance(arg, dict) and arg.get("name")
            ]
            name = str(item.get("name") or "")
            if name:
                prompts.append(MCPPromptDef(name, str(item.get("description") or ""), arguments))
        return prompts

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if not self._connected:
            return ""
        result = await self._request("prompts/get", {"name": name, "arguments": arguments or {}})
        lines: list[str] = []
        description = str(result.get("description") or "").strip()
        if description:
            lines.append(description)
        for message in result.get("messages", []):
            if not isinstance(message, dict):
                continue
            content = _prompt_content_to_text(message.get("content")).strip()
            if content:
                lines.append(f"{message.get('role') or 'user'}: {content}")
        return "\n\n".join(lines).strip()

    async def _sdk_list_roots(self, _context: Any) -> types.ListRootsResult | types.ErrorData:
        if not feature_enabled("mcp_roots"):
            return types.ErrorData(code=types.INVALID_REQUEST, message="roots not supported")
        return types.ListRootsResult(roots=[types.Root.model_validate(root) for root in self._client_roots()])

    async def _sdk_sampling_callback(self, context: Any, params: Any) -> Any:
        if not feature_enabled("mcp_sampling") or self._sampling_handler is None:
            return types.ErrorData(code=types.INVALID_REQUEST, message="sampling not supported")
        owner = self._active_callback_owner()
        if owner is None:
            return types.ErrorData(code=-32002, message="sampling request has no unambiguous active turn owner")
        payload = _model_dict(params)
        payload.update(
            _minicode_owner=owner,
            _mcp_server_name=self.server_name,
            _mcp_request_id=str(context.request_id),
        )
        try:
            result = await self._sampling_handler(payload)
            return types.CreateMessageResult.model_validate(result)
        except PermissionError as exc:
            return types.ErrorData(code=-32003, message=str(exc) or "request rejected")
        except Exception:
            logger.debug("[MCP:%s] sampling callback failed", self.server_name, exc_info=True)
            return types.ErrorData(code=types.INTERNAL_ERROR, message="client request failed")

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
            logger.debug("[MCP:%s] SDK transport message error: %s", self.server_name, message)
            return
        notification = getattr(message, "root", None)
        if isinstance(notification, types.ResourceUpdatedNotification):
            params = _model_dict(notification.params)
            self._record_resource_notification("notifications/resources/updated", params)
        elif isinstance(notification, types.ResourceListChangedNotification):
            params = _model_dict(notification.params)
            self._record_resource_notification("notifications/resources/list_changed", params)

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
        from backend.config import PROJECT_ROOT
        from backend.workspace.state import get_active_workspace_root

        candidates: list[Path] = []
        try:
            candidates.append(get_active_workspace_root().resolve())
        except Exception:
            logger.debug("Could not resolve active workspace root", exc_info=True)
        candidates.extend([Path.cwd(), PROJECT_ROOT])
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
            except Exception:
                logger.debug("Skipping unresolvable MCP root", exc_info=True)
        return roots

    async def _notify_disconnect(self) -> None:
        if self._on_disconnect is None or self._closing or self._disconnect_notified:
            return
        self._disconnect_notified = True
        try:
            await self._on_disconnect(self.server_name)
        except Exception:
            logger.debug("[MCP:%s] disconnect callback failed", self.server_name, exc_info=True)

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

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        self._active_tool_request_owners.clear()
        if self._transport == MCPTransport.IN_PROCESS:
            server = self._in_process_server
            self._in_process_server = None
            close = server.get("close") if server else None
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
            return
        if self._close_event is not None:
            self._close_event.set()
        task = self._lifecycle_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(self._timeout, 3.0))
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._lifecycle_task = None
        self._close_event = None
        self._session = None
