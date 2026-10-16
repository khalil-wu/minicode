"""Thread-owned MCP App invocations, separate from a model turn's cleanup fence."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import secrets
from typing import Any
from uuid import uuid4

from backend.mcp.app_ui import MCPAppDeclaration, MCPAppError, MCPAppResource, app_can_call_tool, parse_app_resource


@dataclass(frozen=True, slots=True)
class MCPAppReceipt:
    invocation_id: str
    server: str
    tool: str
    declaration: MCPAppDeclaration
    title: str
    read_only: bool

    def public_fields(self) -> dict[str, Any]:
        return {**self.declaration.public_fields(), "mcp_app": {
            "id": self.invocation_id, "server": self.server, "tool": self.tool,
            "resourceUri": self.declaration.resource_uri,
            "displayMode": self.declaration.preferred_model_display_mode or "inline",
            "title": self.title, "readOnly": self.read_only}}


@dataclass
class _Invocation:
    receipt: MCPAppReceipt
    scope: tuple[str, str]
    call_id: str
    manager: Any
    client: Any
    connection_lease: Any
    environment_lease: Any
    catalog: tuple
    request_owner: dict[str, Any]
    tool_input: dict[str, Any]
    tool_result: dict[str, Any]
    tool_definition: dict[str, Any]
    origin_key: str
    resource: MCPAppResource | None = None
    views: set[str] = field(default_factory=set)
    requests: set[asyncio.Task] = field(default_factory=set)
    model_context: dict[str, Any] | None = None
    widget_state: Any = None
    open_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class _Frame:
    invocation_id: str
    view_id: str


@dataclass
class MCPAppSourceLease:
    manager: Any
    client: Any
    connection_lease: Any
    environment_lease: Any
    catalog: tuple

    async def aclose(self) -> None:
        await self.connection_lease.aclose()
        if self.environment_lease is not None:
            await self.environment_lease.aclose()


class MCPAppRuntime:
    def __init__(self):
        self.invocations: dict[str, _Invocation] = {}
        self.frames: dict[str, _Frame] = {}

    def retain_source(self, proxy, client) -> MCPAppSourceLease:
        source = proxy._manager
        runtime = source.environment_runtime
        return MCPAppSourceLease(source, client, client.acquire_app_lease(),
            runtime.acquire_app_lease() if runtime is not None else None,
            tuple(deepcopy(source.get_all_tools()[proxy.server_name])))

    def capture(self, proxy, source_lease, arguments, result, context, request_owner) -> MCPAppReceipt:
        """Retain the admitted catalog/environment; never resolve a window's latest selection."""
        source = source_lease.manager
        declaration = proxy.app_declaration
        invocation_id = uuid4().hex
        receipt = MCPAppReceipt(invocation_id, proxy.server_name, proxy._tool_def.name, declaration,
            proxy._tool_def.title or proxy._tool_def.name, bool(context.run_context.is_subagent))
        scope = (context.metadata["conversation_store_id"], context.conversation_id)
        origin = json.dumps([scope, proxy._server_contract, proxy.server_name, declaration.resource_uri], sort_keys=True)
        raw = deepcopy(result.wire_result) if result.wire_result is not None else {
            "content": deepcopy(result.content), "isError": result.is_error,
            **({"structuredContent": deepcopy(result.structured_content)} if result.structured_content is not None else {}),
            "_meta": deepcopy(result.meta)}
        self.invocations[invocation_id] = _Invocation(receipt, scope, context.tool_call_id, source, source_lease.client,
            source_lease.connection_lease, source_lease.environment_lease, source_lease.catalog,
            {**request_owner, "mcp_manager": source, "mcp_app_invocation_id": invocation_id,
                "conversation_store_id": scope[0],
                "cancel_event": None, "deadline_monotonic": None},
            deepcopy(arguments), raw, {"name": proxy._tool_def.name, "description": proxy._tool_def.description,
                "inputSchema": deepcopy(proxy._tool_def.input_schema), "_meta": deepcopy(proxy._tool_def.meta)},
            hashlib.sha256(origin.encode()).hexdigest()[:48])
        return receipt

    def invocation(self, invocation_id: str, conversation_id: str) -> _Invocation:
        invocation = self.invocations.get(invocation_id)
        if invocation is None or invocation.scope[1] != conversation_id:
            raise MCPAppError("MCP App source connection is unavailable", code="source_unavailable", status=410)
        return invocation

    async def open(self, invocation_id: str, conversation_id: str) -> dict[str, Any]:
        invocation = self.invocation(invocation_id, conversation_id)
        task = asyncio.current_task()
        invocation.requests.add(task)
        try:
            return await self._open(invocation)
        finally:
            invocation.requests.discard(task)

    async def _open(self, invocation: _Invocation) -> dict[str, Any]:
        async with invocation.open_lock:
            client = invocation.client
            if not client.connected:
                raise MCPAppError("MCP App source connection is unavailable", code="source_unavailable", status=410)
            if invocation.resource is None:
                from backend.mcp.client import mcp_request_owner

                with mcp_request_owner(invocation.request_owner):
                    result = await client.read_resource_result(invocation.receipt.declaration.resource_uri)
                invocation.resource = parse_app_resource(result, invocation.receipt.declaration.resource_uri)
        view_id, token = uuid4().hex, secrets.token_urlsafe(32)
        invocation.views.add(view_id)
        self.frames[token] = _Frame(invocation.receipt.invocation_id, view_id)
        return {"viewId": view_id, "frameToken": token,
            "hostname": f"mcp-app-{invocation.origin_key}.localhost", "mimeType": invocation.resource.mime_type,
            "prefersBorder": invocation.resource.prefers_border, "readOnly": invocation.receipt.read_only,
            "toolInput": deepcopy(invocation.tool_input), "toolResult": deepcopy(invocation.tool_result),
            "toolDefinition": deepcopy(invocation.tool_definition), "widgetState": deepcopy(invocation.widget_state)}

    def frame(self, token: str, hostname: str) -> tuple[_Invocation, _Frame]:
        frame = self.frames.get(token)
        if frame is None:
            raise MCPAppError("MCP App frame is no longer available", code="frame_unavailable", status=404)
        invocation = self.invocations[frame.invocation_id]
        if hostname != f"mcp-app-{invocation.origin_key}.localhost":
            raise MCPAppError("MCP App frame belongs to another origin", code="origin_mismatch", status=403)
        return invocation, frame

    async def request(self, invocation_id: str, conversation_id: str, view_id: str,
            method: str, params: dict[str, Any]) -> dict[str, Any]:
        invocation = self.invocation(invocation_id, conversation_id)
        if view_id not in invocation.views:
            raise MCPAppError("MCP App view has closed", code="view_closed", status=410)
        task = asyncio.current_task()
        invocation.requests.add(task)
        try:
            if method in {"ui/update-model-context", "ui/legacy/set-widget-state"}:
                if invocation.receipt.read_only:
                    raise MCPAppError("This agent's work record is read-only", code="read_only", status=403)
                encoded = json.dumps(params, ensure_ascii=False, allow_nan=False)
                if len(encoded) > 32768:
                    raise MCPAppError("MCP App context exceeds 32768 characters", code="context_too_large", status=422)
                if method == "ui/update-model-context":
                    invocation.model_context = deepcopy(params)
                else:
                    invocation.widget_state = deepcopy(params.get("state"))
                return {}
            client = invocation.client
            if not client.connected:
                raise MCPAppError("MCP App source connection is unavailable", code="source_unavailable", status=410)
            if method == "tools/list":
                return {"tools": [{"name": tool.name, "description": tool.description,
                    "inputSchema": tool.input_schema, "_meta": tool.meta}
                    for tool in invocation.catalog
                    if app_can_call_tool(tool.meta)]}
            if method == "tools/call":
                if invocation.receipt.read_only:
                    raise MCPAppError("This agent's work record is read-only", code="read_only", status=403)
                tools = invocation.catalog
                tool = next((tool for tool in tools if tool.name == params.get("name") and app_can_call_tool(tool.meta)), None)
                if tool is None:
                    raise MCPAppError("MCP App tool is not in its permitted server catalog", code="tool_unavailable", status=403)
                result = await client.call_tool(tool.name, params.get("arguments"),
                    request_owner=invocation.request_owner,
                    request_meta={**params.get("_meta", {}), "threadId": invocation.scope[1]})
                return deepcopy(result.wire_result) if result.wire_result is not None else {
                    "content": deepcopy(result.content), "isError": result.is_error,
                    **({"structuredContent": result.structured_content} if result.structured_content is not None else {}),
                    "_meta": result.meta}
            from backend.mcp.client import mcp_request_owner

            with mcp_request_owner(invocation.request_owner):
                if method == "resources/read":
                    return await client.read_resource_result(params["uri"])
                if method == "resources/list":
                    return await client.list_resources_result(params.get("cursor"))
                if method == "resources/templates/list":
                    return await client.list_resource_templates_result(params.get("cursor"))
            raise MCPAppError(f"Unsupported MCP App method: {method}", code="unsupported_method", status=422)
        finally:
            invocation.requests.discard(task)

    async def close_view(self, invocation_id: str, conversation_id: str, view_id: str) -> None:
        invocation = self.invocation(invocation_id, conversation_id)
        invocation.views.discard(view_id)
        self.frames = {token: frame for token, frame in self.frames.items()
            if frame.invocation_id != invocation_id or frame.view_id != view_id}

    def model_context(self, store_id: str, conversation_id: str) -> list[dict[str, Any]]:
        return [{"server": invocation.receipt.server, "tool": invocation.receipt.tool,
            "callId": invocation.call_id, "context": deepcopy(invocation.model_context)}
            for invocation in self.invocations.values()
            if invocation.scope == (store_id, conversation_id) and invocation.model_context is not None]

    def owns_request(self, owner: dict[str, Any]) -> bool:
        invocation = self.invocations.get(owner.get("mcp_app_invocation_id"))
        return invocation is not None and not invocation.receipt.read_only and bool(invocation.requests) \
            and invocation.manager is owner.get("mcp_manager") \
            and invocation.scope == (owner.get("conversation_store_id"), owner.get("conversation_id"))

    async def close_conversation(self, store_id: str, conversation_id: str) -> None:
        ids = [key for key, invocation in self.invocations.items() if invocation.scope == (store_id, conversation_id)]
        for key in ids:
            invocation = self.invocations[key]
            if invocation.requests:
                await asyncio.gather(*tuple(invocation.requests), return_exceptions=True)
            await invocation.connection_lease.aclose()
            if invocation.environment_lease is not None:
                await invocation.environment_lease.aclose()
            self.invocations.pop(key)
        self.frames = {token: frame for token, frame in self.frames.items() if frame.invocation_id not in ids}

    async def aclose(self) -> None:
        for scope in {invocation.scope for invocation in self.invocations.values()}:
            await self.close_conversation(*scope)
