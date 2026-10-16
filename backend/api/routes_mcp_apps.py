"""Authenticated App host RPC and single-invocation HTML capabilities."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict

from backend.api import _state
from backend.mcp.app_ui import MCPAppError, resource_csp

router = APIRouter()
_LEGACY_BRIDGE = Path(__file__).parents[1] / "mcp/app_legacy_bridge.js"


class AppScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str


class AppView(AppScope):
    view_id: str


class AppRequest(AppView):
    method: str
    params: dict[str, Any]


def _runtime():
    if _state.bootstrap is None:
        raise HTTPException(503, detail={"code": "app_unavailable", "message": "MCP App host is unavailable"})
    return _state.bootstrap.mcp_apps


@asynccontextmanager
async def _app_operation():
    try:
        yield
    except MCPAppError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from error
    except McpError as error:
        raise HTTPException(502, detail={"code": "mcp_error", "message": error.error.message,
            "mcp_code": error.error.code}) from error
    except (ConnectionError, TimeoutError) as error:
        raise HTTPException(502, detail={"code": "source_unavailable", "message": str(error)}) from error


@router.post("/api/mcp/apps/{invocation_id}/open")
async def open_app(invocation_id: str, scope: AppScope, request: Request):
    async with _app_operation():
        view = await _runtime().open(invocation_id, scope.conversation_id)
        frame_url = request.url.replace(scheme="http", hostname=view.pop("hostname"),
            path=f"/mcp-app/frame/{view.pop('frameToken')}", query="")
        return {**view, "frameUrl": str(frame_url), "frameOrigin": f"{frame_url.scheme}://{frame_url.netloc}"}


@router.post("/api/mcp/apps/{invocation_id}/request")
async def app_request(invocation_id: str, body: AppRequest):
    async with _app_operation():
        return await _runtime().request(invocation_id, body.conversation_id, body.view_id, body.method, body.params)


@router.post("/api/mcp/apps/{invocation_id}/close")
async def close_app(invocation_id: str, body: AppView):
    async with _app_operation():
        await _runtime().close_view(invocation_id, body.conversation_id, body.view_id)
        return {}


def _frame(request: Request, token: str):
    return _runtime().frame(token, request.url.hostname or "")


@router.get("/mcp-app/frame/{token}")
async def app_frame(token: str, request: Request):
    async with _app_operation():
        invocation, _ = _frame(request, token)
        resource = invocation.resource
        html = resource.html
        if resource.mime_type == "text/html+skybridge":
            script = f'<script src="/mcp-app/frame/{token}/legacy.js"></script>'
            head = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
            html = (html[:head.end()] + script + html[head.end():] if head is not None else
                "<!doctype html><html><head>" + script + "</head><body>" + html + "</body></html>")
        return HTMLResponse(html, headers={"Content-Security-Policy": resource_csp(resource),
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})


@router.get("/mcp-app/frame/{token}/legacy.js")
async def app_legacy_bridge(token: str, request: Request):
    async with _app_operation():
        invocation, _ = _frame(request, token)
        globals = {"toolInput": invocation.tool_input,
            "toolOutput": invocation.tool_result.get("structuredContent"),
            "toolResponseMetadata": invocation.tool_result.get("_meta", {}),
            "widgetState": invocation.widget_state, "displayMode": invocation.receipt.declaration.preferred_model_display_mode or "inline"}
        source = "const initialGlobals = " + json.dumps(globals, ensure_ascii=False) + ";\n" + _LEGACY_BRIDGE.read_text(encoding="utf-8")
        return Response(source, media_type="text/javascript", headers={"Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})
