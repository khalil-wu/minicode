"""
MiniCode Backend entry point.

Provides two interfaces:
  - POST /api/chat  - REST API (Phase 1 compatible, synchronous blocking)
  - WS   /ws        - WebSocket (streaming, full Agent Loop)

Startup initialisation:
  - MCP Server manager (loads from .mcp.json and starts)
  - Skills discovery and manager
  - File memory system
  - Passive RAG pipeline for uploaded/document retrieval
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.agent.message import AgentEvent
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.config import PROJECT_ROOT, load_config
from backend.runtime_env import ensure_utf8_console_logging
from backend.version import __version__
from backend.workspace import create_workspace_router

# ── Decomposed sub-modules ──
from backend.api.auth import (
    _is_workspace_raw_token_authorized,
    _is_runtime_authorized,
    _is_websocket_authorized,
    _websocket_accept_subprotocol,
)
from backend.api.tool_registry import _build_tool_registry as _api_build_tool_registry
from backend.api.routes_health import _build_status_payload, get_mcp_status, get_mcp_manager
from backend.api.routes_chat import router as chat_router
from backend.api.routes_llm import router as llm_router
from backend.api.routes_skills import router as skills_router
from backend.api.routes_agents import router as agents_router
from backend.api.routes_replay import router as replay_router
from backend.api.routes_health import router as health_router
from backend.api import _state

logger = logging.getLogger(__name__)

# UTF-8 console/logging so Chinese log output is not mojibake'd by the Windows
# console codepage. Idempotent; runs once at import.
ensure_utf8_console_logging()

_bootstrap: AppBootstrap | None = None


def _build_tool_registry(
    artifact_store: ArtifactStore,
    vector_memory: Any = None,
    *,
    llm_provider: Any | None = None,
    mcp_manager: Any | None = None,
):
    """Backward-compatible registry builder wrapper.

    Older tests and integrations monkeypatch backend.main._bootstrap. The
    decomposed registry builder reads backend.api._state.bootstrap, so keep the
    two surfaces synchronized at the boundary.

    The signature must stay EXPLICIT (not ``*args, **kwargs``):
    ``AppBootstrap.create_tool_registry`` decides whether to forward
    ``llm_provider``/``mcp_manager`` by introspecting this wrapper's parameter
    names. With a varargs signature those names are absent, so the MCP manager
    is never passed and MCP proxies never register into rebuilt registries —
    silently breaking MCP hot-reload.
    """
    if _state.bootstrap is None and _bootstrap is not None:
        _state.bootstrap = _bootstrap
    return _api_build_tool_registry(
        artifact_store,
        vector_memory,
        llm_provider=llm_provider,
        mcp_manager=mcp_manager,
    )

# ── Windows event loop policy ──
if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception as exc:
        logger.warning("Failed to set WindowsProactorEventLoopPolicy: %s", exc)


from backend.llm.model_registry import (
    create_llm_adapter as _create_llm_adapter,
)


# ── Frontend paths ──

# Prefer built Vite assets when they exist; otherwise proxy to the dev server.
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_SRC = PROJECT_ROOT / "frontend"
FRONTEND_DIR = FRONTEND_DIST if FRONTEND_DIST.is_dir() else FRONTEND_SRC
IS_PRODUCTION = FRONTEND_DIR == FRONTEND_DIST


# ── MCP status broadcast callback (used by AppBootstrap) ──

async def _broadcast_mcp_status_change(server_name: str, _status: Any) -> None:
    """Push MCP status + per-server lifecycle/progress to every connected session.

    ``mcp_status`` keeps carrying the whole list (back-compat). ``mcp.lifecycle``
    and ``mcp.progress`` are per-server so the UI can surface auth/reconnect/failed
    state and connect progress without diffing the list. Fired on every status
    change, including health-loop auto-reconnects.
    """
    _state.invalidate_status_cache()
    await _state.ws_manager.broadcast_event(
        AgentEvent(
            type="mcp_status",
            data={"servers": get_mcp_status()},
        )
    )
    manager = get_mcp_manager()
    if manager is None:
        return
    lifecycle = manager.get_server_lifecycle(server_name)
    if lifecycle is not None:
        await _state.ws_manager.broadcast_event(
            AgentEvent(type="mcp.lifecycle", data=lifecycle)
        )
    progress = manager.get_server_progress(server_name)
    if progress is not None:
        await _state.ws_manager.broadcast_event(
            AgentEvent(type="mcp.progress", data=progress)
        )


# ── FastAPI application ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle - delegates to AppBootstrap."""

    global _bootstrap
    _state.bootstrap = AppBootstrap(
        build_tool_registry=_build_tool_registry,
        build_status_payload=_build_status_payload,
        create_llm_adapter=_create_llm_adapter,
        ws_manager=_state.ws_manager,
        on_mcp_status_change=_broadcast_mcp_status_change,
        status_cache_ttl_seconds=_state.STATUS_CACHE_TTL_SECONDS,
    )
    _bootstrap = _state.bootstrap
    await _state.bootstrap.startup()
    logger.info("MiniCode Backend startup complete")
    try:
        yield
    finally:
        if _state.bootstrap is not None:
            await _state.bootstrap.shutdown()
        _bootstrap = None
        logger.info("MiniCode Backend shutdown complete")


app = FastAPI(
    title="MiniCode Agent API",
    description="AI coding assistant backend - REST and WebSocket interfaces",
    version=__version__,
    lifespan=lifespan,
)


# ── CORS configuration ──

def _configured_cors_origins() -> list[str]:
    configured = os.environ.get("MINICODE_CORS_ORIGINS", "")
    return list(dict.fromkeys(origin.strip() for origin in configured.split(",") if origin.strip()))


def _build_cors_origins() -> list[str]:
    origins = _configured_cors_origins()
    runtime_frontend = os.environ.get("MINICODE_FRONTEND_URL") or os.environ.get("VITE_DEV_FRONTEND_ORIGIN")
    if runtime_frontend:
        origins.append(runtime_frontend.strip())
    if os.environ.get("MINICODE_RUNTIME_TOKEN"):
        # Electron's packaged file:// renderer sends Origin: null for
        # cross-origin backend fetches. The runtime token remains the auth gate.
        origins.append("null")
    if IS_PRODUCTION:
        return list(dict.fromkeys(origin for origin in origins if origin))
    return list(
        dict.fromkeys(
            [
                *origins,
                "http://localhost",
                "http://127.0.0.1",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ]
        )
    )


def _build_cors_origin_regex() -> str | None:
    if os.environ.get("MINICODE_DISABLE_DEV_CORS") == "1":
        return None
    is_source_checkout = (PROJECT_ROOT / "frontend" / "vite.config.ts").is_file()
    if IS_PRODUCTION and not is_source_checkout:
        return None
    return r"https?://(localhost|127\.0\.0\.1):(5[1-2][0-9]{2}|80[0-9]{2})"


# ── Middleware ──

@app.middleware("http")
async def _api_v1_path_alias(request: Request, call_next):
    """Rewrite /api/v1/* -> /api/* so v1 callers reach the same handlers."""
    path = request.scope.get("path", "")
    is_workspace_raw_authorized = _is_workspace_raw_token_authorized(request)
    if (
        request.method != "OPTIONS"
        and path.startswith("/api/")
        and not is_workspace_raw_authorized
        and not _is_runtime_authorized(request)
    ):
        return Response("Unauthorized", status_code=401)
    if path.startswith("/api/v1/"):
        rewritten = "/api/" + path[len("/api/v1/"):]
        request.scope["path"] = rewritten
        raw_path = request.scope.get("raw_path")
        if isinstance(raw_path, bytes) and raw_path.startswith(b"/api/v1/"):
            request.scope["raw_path"] = b"/api/" + raw_path[len(b"/api/v1/"):]
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_origin_regex=_build_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Existing sub-routers ──

from backend.api.projects import router as projects_router
from backend.api.git import router as git_router

app.include_router(projects_router)
app.include_router(git_router)


# ── Decomposed API routers ──

app.include_router(chat_router)
app.include_router(llm_router)
app.include_router(skills_router)
app.include_router(agents_router)
app.include_router(replay_router)
app.include_router(health_router)


# ── Workspace router ──

app.include_router(create_workspace_router(lambda: PROJECT_ROOT))


# ── Static file serving ──

# Prefer Vite build artifacts (dist/), proxy to Vite dev server in development
if IS_PRODUCTION:
    # Production: serve static assets from dist/
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")
    if (FRONTEND_DIST / "fonts").is_dir():
        app.mount("/fonts", StaticFiles(directory=str(FRONTEND_DIST / "fonts")), name="fonts")
    logger.info("Frontend: production mode, serving dist/")
else:
    # Development: proxy frontend resource requests to Vite dev server
    _vite_dev_origin = os.environ.get("MINICODE_FRONTEND_URL") or os.environ.get("VITE_DEV_FRONTEND_ORIGIN") or "http://localhost:5173"
    _vite_client = httpx.AsyncClient(base_url=_vite_dev_origin, timeout=5.0)
    logger.info("Frontend: dev mode, proxying to Vite dev server (%s)", _vite_dev_origin)

    async def _proxy_vite_request(path: str) -> Response:
        try:
            resp = await _vite_client.get(path)
            content_type = resp.headers.get("content-type", "text/plain")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=content_type,
            )
        except Exception as exc:
            logger.debug("Vite proxy request failed: %s", exc)
            return Response(
                content="Vite dev server is not running. Start it with npm run dev.",
                status_code=502,
            )

    @app.api_route("/src/{path:path}", methods=["GET"])
    async def proxy_vite_src(path: str):
        """Proxy /src/* requests to the Vite dev server."""
        return await _proxy_vite_request(f"/src/{path}")

    @app.api_route("/@react-refresh", methods=["GET"])
    async def proxy_vite_react_refresh():
        """Proxy the React Refresh runtime from Vite."""
        return await _proxy_vite_request("/@react-refresh")

    @app.api_route("/@id/{path:path}", methods=["GET"])
    @app.api_route("/@vite/{path:path}", methods=["GET"])
    @app.api_route("/@fs/{path:path}", methods=["GET"])
    @app.api_route("/node_modules/{path:path}", methods=["GET"])
    async def proxy_vite_other(request: Request, path: str):
        """Proxy Vite special runtime/module paths."""
        return await _proxy_vite_request(request.url.path)


_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#171717"/>'
    '<text x="16" y="23" text-anchor="middle" font-size="20" fill="#cc785c">M</text>'
    "</svg>"
)


@app.get("/favicon.ico")
@app.get("/favicon.svg")
async def favicon():
    """Serve the MiniCode favicon."""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/")
async def index():
    """Serve the frontend index page."""
    if IS_PRODUCTION and (FRONTEND_DIST / "index.html").is_file():
        return FileResponse(str(FRONTEND_DIST / "index.html"))
    # Dev mode: proxy to Vite dev server
    if not IS_PRODUCTION:
        try:
            resp = await _vite_client.get("/")
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "text/html"))
        except Exception as exc:
            # Vite not running, fallback to local index.html
            logger.debug("Vite not running, serving local index.html: %s", exc)
            return FileResponse(str(FRONTEND_SRC / "index.html"))
    return FileResponse(str(FRONTEND_SRC / "index.html"))


# ── UI Preferences API ──

@app.get("/api/ui/preferences")
async def get_ui_preferences(session_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    """Get user UI preferences (layout, panel sizes, theme overrides)."""
    from backend.ui.preferences import UIPreferencesStore
    from pathlib import Path

    store = UIPreferencesStore(Path("data/ui_preferences"))
    prefs = store.get(session_id)
    return prefs.to_dict()


@app.put("/api/ui/preferences")
async def update_ui_preferences(
    preferences: dict[str, Any],
    session_id: str = Query(..., min_length=1)
) -> dict[str, Any]:
    """Save UI preferences."""
    from backend.ui.preferences import UIPreferencesStore
    from pathlib import Path

    store = UIPreferencesStore(Path("data/ui_preferences"))
    updated = store.update(session_id, preferences)
    return {"status": "ok", "preferences": updated.to_dict()}


# ── WebSocket endpoint ──

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket chat endpoint."""
    if not _is_websocket_authorized(websocket):
        await websocket.accept(subprotocol=_websocket_accept_subprotocol(websocket))
        await websocket.close(code=1008)
        return

    config = _state.bootstrap.config or load_config()

    # Create LLM adapter
    try:
        llm = _state.bootstrap.create_llm()
    except Exception as exc:
        await websocket.accept(subprotocol=_websocket_accept_subprotocol(websocket))
        await websocket.send_json(
            AgentEvent.error(f"LLM initialization failed: {exc}", recoverable=False).to_ws_message()
        )
        await websocket.close()
        return

    # Create session-level resources
    artifact_store = ArtifactStore()
    tool_registry = _state.bootstrap.create_tool_registry(artifact_store)
    permission_checker = _state.bootstrap.create_permission_checker()

    session, connection_generation = await _state.ws_manager.connect(
        websocket=websocket,
        llm=llm,
        artifact_store=artifact_store,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        config=config,
        skill_manager=_state.bootstrap.skill_manager,
        skill_executor=_state.bootstrap.skill_executor,
        rag_pipeline=_state.bootstrap.rag_pipeline,
        memory_manager=_state.bootstrap.file_memory,
        vector_memory=None,
    )

    try:
        await session.handle(connection_generation=connection_generation)
    finally:
        _state.ws_manager.disconnect(
            session.session_id,
            connection_generation=connection_generation,
        )
