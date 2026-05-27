"""
MiniCode Backend 入口（DESIGN.md §一 架构图 Backend (FastAPI)）。

提供两种接口：
  - POST /api/chat  — REST API（兼容 Phase 1，同步阻塞式）
  - WS   /ws        — WebSocket（流式，完整 Agent Loop）

启动时初始化：
  - MCP Server 管理器（从 .mcp.json 加载并启动）
  - Skills 发现与管理器
  - 文件记忆系统
  - 被动 RAG 流水线
"""

from __future__ import annotations

import asyncio
import hmac
import httpx
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.agent.context import ContextBuilder
from backend.agent.claude_md import load_project_guideline_bundle
from backend.agent.loop import run_agent_loop
from backend.agent.message import AgentEvent
from backend.attachments.store import AttachmentStore
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.commands.catalog import (
    get_builtin_command_catalog,
    get_enabled_composer_command_catalog,
)
from backend.config import (
    AppConfig,
    PROJECT_ROOT,
    get_anthropic_settings,
    get_available_models,
    get_custom_settings,
    get_llm_provider,
    get_llm_settings_payload,
    get_openai_settings,
    load_config,
    save_llm_settings,
)
from backend.conversations.models import utc_now_iso
from backend.documents.service import AttachmentRecord, ingest_uploaded_document
from backend.mcp.config_file import MCP_CONFIG_FILE, read_mcp_config, write_mcp_config
from backend.permissions.checker import PermissionChecker
from backend.skills.marketplace import install_marketplace_skill, list_curated_skills, list_extensions_marketplace, remove_user_skill
from backend.tools.agent_tools import AskUserTool, ReadArtifactTool, TaskTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from backend.tools.registry import ToolRegistry
from backend.tools.search_tools import GrepFilesTool, GlobFilesTool
from backend.workspace import create_workspace_router
from backend.workspace.state import get_active_workspace_root
from backend.ws.handler import WebSocketManager

logger = logging.getLogger(__name__)

RUNTIME_TOKEN_ENV = "MINICODE_RUNTIME_TOKEN"


def _runtime_token() -> str:
    return os.environ.get(RUNTIME_TOKEN_ENV, "").strip()


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _request_runtime_token(request: Request) -> str:
    return (
        request.headers.get("x-minicode-token", "")
        or request.query_params.get("minicode_token", "")
    ).strip()


def _is_runtime_authorized(request: Request) -> bool:
    expected = _runtime_token()
    if not expected:
        return True
    supplied = _request_runtime_token(request)
    return bool(supplied) and _constant_time_equal(supplied, expected)


def _websocket_runtime_token(websocket: WebSocket) -> str:
    # Browser WebSocket API cannot set custom headers, so we fall back to query
    # params for dev-mode browser connections. In Electron desktop mode the token
    # is injected via preload and never appears in the URL. Access logs must not
    # record query strings containing the token.
    return (
        websocket.headers.get("x-minicode-token", "")
        or websocket.query_params.get("minicode_token", "")
    ).strip()


def _is_websocket_authorized(websocket: WebSocket) -> bool:
    expected = _runtime_token()
    if not expected:
        return True
    supplied = _websocket_runtime_token(websocket)
    return bool(supplied) and _constant_time_equal(supplied, expected)

if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


from backend.llm.model_registry import (
    create_llm_adapter as _create_llm_adapter,
    create_session_llm as _create_session_llm,
)


# ── 全局状态 ──────────────────────────────────────────────
_ws_manager = WebSocketManager()
_bootstrap: AppBootstrap | None = None
_status_cache_payload: dict[str, Any] | None = None
_status_cache_expires_at = 0.0
_capability_cache_payload: dict[str, Any] | None = None
_capability_cache_expires_at = 0.0
_CAPABILITY_CACHE_TTL_SECONDS = 60

STATUS_CACHE_TTL_SECONDS = 5.0

# Prefer built Vite assets when they exist; otherwise proxy to the dev server.
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_SRC = PROJECT_ROOT / "frontend"
FRONTEND_DIR = FRONTEND_DIST if FRONTEND_DIST.is_dir() else FRONTEND_SRC
IS_PRODUCTION = FRONTEND_DIR == FRONTEND_DIST


def _get_attachment_store() -> AttachmentStore:
    """Create an attachment store from the current configured base dir."""
    return AttachmentStore()


async def _broadcast_mcp_status_change(_server_name: str, _status: Any) -> None:
    """Push the latest MCP status snapshot to every connected session."""
    await _ws_manager.broadcast_event(
        AgentEvent(
            type="mcp_status",
            data={"servers": get_mcp_status()},
        )
    )


def _build_tool_registry(
    artifact_store: ArtifactStore,
    vector_memory: Any | None = None,
    *,
    llm_provider: Any | None = None,
) -> ToolRegistry:
    """
    构建默认工具注册中心（DESIGN.md §8.2）。

    内置工具清单：
      read_file / write_file / edit_file / list_files — 文件操作
      grep_files    — 搜索
      run_command   — 命令执行
      ask_user      — 主动提问
      read_artifact — 读取 artifact
      read_memory / save_memory — 记忆操作

    MCP 工具会在连接后动态注册。
    """
    registry = ToolRegistry()

    # 文件工具
    registry.register(ReadFileTool(artifact_store))
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ListFilesTool())

    # 搜索工具
    registry.register(GrepFilesTool())
    registry.register(GlobFilesTool())

    # 命令工具
    registry.register(RunCommandTool(artifact_store))

    # Agent 辅助工具
    registry.register(AskUserTool())
    registry.register(
        ReadArtifactTool(
            artifact_store,
            attachment_store=_get_attachment_store(),
        )
    )
    registry.register(
        TaskTool(
            llm_provider=llm_provider,
            tool_registry_provider=lambda: registry,
            artifact_store=artifact_store,
            permission_checker_provider=lambda: PermissionChecker(load_config().permissions),
            agent_settings_provider=lambda: load_config().agent,
            token_budget_provider=lambda: load_config().token_budget,
        )
    )

    # ── 记忆工具（DESIGN.md §2.2）──
    file_memory = _bootstrap.file_memory if _bootstrap else None
    if file_memory:
        from backend.tools.memory_tools import (
            ReadMemoryTool, SaveMemoryTool,
            RecallMemoryTool, RememberMemoryTool,
        )

        registry.register(ReadMemoryTool(file_memory))
        registry.register(SaveMemoryTool(file_memory))
        if vector_memory is not None:
            registry.register(RecallMemoryTool(vector_memory))
            registry.register(RememberMemoryTool(vector_memory))

    # ── Web 工具（DESIGN.md §8.2）──
    from backend.tools.web_tools import WebFetchTool, WebSearchTool
    registry.register(WebFetchTool(artifact_store))
    registry.register(WebSearchTool(artifact_store))

    # ── AST 轻量代码分析工具（newplan.md §4.1）──
    from backend.tools.ast_tools import GoToDefinitionTool, FindReferencesTool
    registry.register(GoToDefinitionTool())
    registry.register(FindReferencesTool())

    # ── Git 工具 ──
    from backend.tools.git_tools import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitLogTool())
    registry.register(GitCommitTool())

    # ── 模糊搜索工具 ──
    from backend.tools.fuzzy_search_tool import FuzzySearchTool
    from pathlib import Path
    workspace_root = Path.cwd()
    registry.register(FuzzySearchTool(workspace_root))

    # ── Worktree 工具 ──
    from backend.tools.worktree_tools import (
        ListWorktreesTool,
        CreateWorktreeTool,
        RemoveWorktreeTool,
    )
    registry.register(ListWorktreesTool())
    registry.register(CreateWorktreeTool())
    registry.register(RemoveWorktreeTool())

    # ── Preview 工具 ──
    from backend.tools.preview_tool import PreviewServerTool
    registry.register(PreviewServerTool(workspace_root=str(workspace_root)))

    # ── MCP 资源工具 ──
    mcp_manager = _bootstrap.mcp_manager if _bootstrap else None
    from backend.tools.mcp_tools import ListMcpResourcesTool, ReadMcpResourceTool
    registry.register(ListMcpResourcesTool(mcp_manager))
    registry.register(ReadMcpResourceTool(mcp_manager, artifact_store))

    # ── 任务管理工具 ──
    from backend.tools.todo_tool import TodoWriteTool
    todo_tool = TodoWriteTool()
    registry.register(todo_tool)

    # ── 工具搜索（延迟发现）──
    from backend.tools.tool_search import ToolSearchTool
    tool_search = ToolSearchTool()
    registry.register(tool_search)

    # ── Skill 工具（DESIGN.md §5/8.2）──
    skill_manager = _bootstrap.skill_manager if _bootstrap else None
    from backend.tools.skill_tools import LoadSkillTool, UnloadSkillTool, ListSkillsTool
    registry.register(LoadSkillTool(skill_manager))
    registry.register(UnloadSkillTool(skill_manager))
    registry.register(ListSkillsTool(skill_manager))
    for command_definition in get_builtin_command_catalog():
        registry.register_command(command_definition["name"], command_definition)
    if skill_manager:
        try:
            for skill in skill_manager.list_all():
                if isinstance(skill, dict):
                    skill_name = str(skill.get("name", "")).strip()
                    if skill_name:
                        registry.register_skill(skill_name, skill)
        except Exception as exc:
            logger.debug("Skill metadata registration failed: %s", exc)

    # ── MCP 动态工具注册（DESIGN.md §6.3）──
    if mcp_manager:
        try:
            from backend.mcp.registry import MCPToolRegistry
            mcp_registry = MCPToolRegistry(registry, artifact_store)
            all_tools = mcp_manager.get_all_tools()
            for server_name, tools in all_tools.items():
                client = mcp_manager.get_client(server_name)
                if client:
                    count = mcp_registry.register_server_tools(
                        server_name, tools, client,
                    )
                    logger.info(
                        "MCP Server '%s' 注册了 %d 个工具",
                        server_name, count,
                    )
        except Exception as exc:
            logger.warning("MCP 工具注册失败: %s", exc)

    # ── 构建工具搜索索引 ──
    tool_search.update_index(registry.get_tools())

    return registry


# ── FastAPI 应用 ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle — delegates to AppBootstrap."""
    global _bootstrap

    _bootstrap = AppBootstrap(
        build_tool_registry=_build_tool_registry,
        build_status_payload=_build_status_payload,
        create_llm_adapter=_create_llm_adapter,
        ws_manager=_ws_manager,
        on_mcp_status_change=_broadcast_mcp_status_change,
        status_cache_ttl_seconds=STATUS_CACHE_TTL_SECONDS,
    )
    await _bootstrap.startup()
    logger.info("MiniCode Backend startup complete")
    try:
        yield
    finally:
        if _bootstrap is not None:
            await _bootstrap.shutdown()
        logger.info("MiniCode Backend shutdown complete")


app = FastAPI(
    title="MiniCode Agent API",
    description="AI 编程助手后端 — 支持 REST 和 WebSocket 两种接口",
    version="0.2.0",
    lifespan=lifespan,
)

# ── API 路由注册 ─────────────────────────────────────────────
def _configured_cors_origins() -> list[str]:
    configured = os.environ.get("MINICODE_CORS_ORIGINS", "")
    return list(dict.fromkeys(origin.strip() for origin in configured.split(",") if origin.strip()))


def _build_cors_origins() -> list[str]:
    origins = _configured_cors_origins()
    runtime_frontend = os.environ.get("MINICODE_FRONTEND_URL") or os.environ.get("VITE_DEV_FRONTEND_ORIGIN")
    if runtime_frontend:
        origins.append(runtime_frontend.strip())
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
    if IS_PRODUCTION:
        return None
    return r"https?://(localhost|127\.0\.0\.1):(5[1-2][0-9]{2}|80[0-9]{2})"


@app.middleware("http")
async def _api_v1_path_alias(request: Request, call_next):
    """Rewrite /api/v1/* → /api/* so v1 callers reach the same handlers."""
    path = request.scope.get("path", "")
    if request.method != "OPTIONS" and path.startswith("/api/") and not _is_runtime_authorized(request):
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

from backend.api.projects import router as projects_router
from backend.api.git import router as git_router

app.include_router(projects_router)
app.include_router(git_router)

# ── 静态文件服务 ────────────────────────────────────────────
app.include_router(create_workspace_router(lambda: PROJECT_ROOT))

# 优先使用 Vite build 产物（dist/），开发模式下代理到 Vite dev server
if IS_PRODUCTION:
    # 生产模式：托管 dist/ 下的静态资源
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")
    if (FRONTEND_DIST / "fonts").is_dir():
        app.mount("/fonts", StaticFiles(directory=str(FRONTEND_DIST / "fonts")), name="fonts")
    logger.info("前端：生产模式，托管 dist/")
else:
    # 开发模式：代理前端资源请求到 Vite dev server
    _vite_dev_origin = os.environ.get("MINICODE_FRONTEND_URL") or os.environ.get("VITE_DEV_FRONTEND_ORIGIN") or "http://localhost:5173"
    _vite_client = httpx.AsyncClient(base_url=_vite_dev_origin, timeout=5.0)
    logger.info("前端：开发模式，代理到 Vite dev server (%s)", _vite_dev_origin)

    async def _proxy_vite_request(path: str) -> Response:
        try:
            resp = await _vite_client.get(path)
            content_type = resp.headers.get("content-type", "text/plain")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=content_type,
            )
        except Exception:
            return Response(
                content="Vite dev server 未启动，请运行 npm run dev",
                status_code=502,
            )

    @app.api_route("/src/{path:path}", methods=["GET"])
    async def proxy_vite_src(path: str):
        """代理 /src/* 请求到 Vite dev server。"""
        return await _proxy_vite_request(f"/src/{path}")

    @app.api_route("/@react-refresh", methods=["GET"])
    async def proxy_vite_react_refresh():
        """代理 React Refresh runtime 请求，避免开发态白屏。"""
        return await _proxy_vite_request("/@react-refresh")

    @app.api_route("/@id/{path:path}", methods=["GET"])
    @app.api_route("/@vite/{path:path}", methods=["GET"])
    @app.api_route("/@fs/{path:path}", methods=["GET"])
    @app.api_route("/node_modules/{path:path}", methods=["GET"])
    async def proxy_vite_other(request: Request, path: str):
        """代理 Vite 特殊路径请求。"""
        return await _proxy_vite_request(request.url.path)


_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#171717"/>'
    '<text x="16" y="23" text-anchor="middle" font-size="20" fill="#cc785c">◆</text>'
    "</svg>"
)


@app.get("/favicon.ico")
@app.get("/favicon.svg")
async def favicon():
    """MiniCode favicon（避免浏览器 404 日志）。"""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/")
async def index():
    """服务前端首页。"""
    if IS_PRODUCTION and (FRONTEND_DIST / "index.html").is_file():
        return FileResponse(str(FRONTEND_DIST / "index.html"))
    # 开发模式：代理到 Vite dev server
    if not IS_PRODUCTION:
        try:
            resp = await _vite_client.get("/")
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "text/html"))
        except Exception:
            # Vite 未启动时 fallback 到本地 index.html
            return FileResponse(str(FRONTEND_SRC / "index.html"))
    return FileResponse(str(FRONTEND_SRC / "index.html"))


# ── REST API（兼容 Phase 1）──────────────────────────────


class ChatRequest(BaseModel):
    """聊天请求。"""
    message: str = Field(min_length=1)
    max_iterations: int = Field(default=10, ge=1, le=50)


class ToolCallRecord(BaseModel):
    """工具调用记录。"""
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output: str | None = None
    artifact_id: str | None = None
    status: str = "success"


class ChatResponse(BaseModel):
    """聊天响应。"""
    reply: str
    stopped_reason: str
    iterations: int
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class UploadResponse(BaseModel):
    """Uploaded document metadata for the active session."""

    file_name: str
    doc_id: str
    artifact_id: str
    indexed_chunks: int
    attachment: dict[str, Any]


class OpenAISettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    reasoning_effort: str = "high"
    max_tokens: int = 8192
    wire_api: str = "responses"


class AnthropicSettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    max_tokens: int = 8192
    thinking_budget: int = 0


class CustomSettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    reasoning_effort: str = "high"
    max_tokens: int = 8192
    thinking_budget: int = 0
    wire_api: str = "chat"


class LLMSettingsUpdateRequest(BaseModel):
    provider: str = "openai"
    openai: OpenAISettingsPayload = Field(default_factory=OpenAISettingsPayload)
    anthropic: AnthropicSettingsPayload = Field(default_factory=AnthropicSettingsPayload)
    custom: CustomSettingsPayload = Field(default_factory=CustomSettingsPayload)
    confirm_sensitive_change: bool = False


class MCPConfigUpdateRequest(BaseModel):
    content: str = Field(min_length=1)
    reload: bool = True
    confirm_sensitive_change: bool = False


class SkillInstallRequest(BaseModel):
    skill_name: str = Field(min_length=1)


class LLMModelsRefreshResponse(BaseModel):
    provider: str
    provider_id: str
    models: list[str] = Field(default_factory=list)
    selected_model: str = ""
    source: str = "preset"
    source_message: str = ""
    generated_at: float = Field(default_factory=time.time)


_LATEST_PROVIDER_MODELS: dict[str, list[str]] = {
    "openai_official": ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4.1", "gpt-4o"],
    "lucen": ["gpt-5.4"],
    "deepseek": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
    "qwen": [
        "qwen3-coder-next",
        "qwen3-next-80b-a3b-thinking",
        "qwen3-next-80b-a3b-instruct",
        "qwen3.5-flash",
        "qwen3-235b-a22b-thinking-2507",
        "qwen3-235b-a22b-instruct-2507",
        "qwen3-32b",
        "qwen3-14b",
    ],
    "zhipu": ["glm-5.1", "glm-5", "glm-4.7"],
    "anthropic_off": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
    "openrouter": ["openai/gpt-5.2", "openai/gpt-4o", "deepseek/deepseek-chat", "google/gemini-2.5-pro"],
    "siliconflow": [
        "Pro/deepseek-ai/DeepSeek-R1",
        "Pro/deepseek-ai/DeepSeek-V3.2",
        "deepseek-ai/DeepSeek-V3.2",
        "Qwen/Qwen2.5-72B-Instruct",
    ],
    "custom_openai": [],
}

_CUSTOM_GATEWAY_FALLBACK_MODELS = [
    "claude-sonnet-4.6",
    "claude-sonnet-4-6",
    "claude-opus-4.6",
    "claude-opus-4-6",
    "gpt-5.4",
    "gpt-5.4-mini",
]


def _normalize_provider_value(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "anthropic":
        return "anthropic"
    if normalized in {"custom", "deepseek", "openrouter", "qwen", "moonshot", "together", "groq"}:
        return "custom"
    return "openai"


def _resolve_openai_provider_id(base_url: str) -> str:
    host = urlsplit(base_url).netloc.lower()
    if "lucen.cc" in host:
        return "lucen"
    if "api.openai.com" in host:
        return "openai_official"
    if "api.deepseek.com" in host:
        return "deepseek"
    if "dashscope" in host or "aliyuncs.com" in host:
        return "qwen"
    if "bigmodel.cn" in host:
        return "zhipu"
    if "openrouter.ai" in host:
        return "openrouter"
    if "siliconflow.cn" in host:
        return "siliconflow"
    return "custom_openai"


def _is_chat_model_id(model_id: str) -> bool:
    value = model_id.strip().lower()
    if not value:
        return False
    blocked = (
        "embedding",
        "moderation",
        "transcrib",
        "tts",
        "speech",
        "image",
        "dall",
        "realtime",
        "computer-use",
        "search-preview",
    )
    return not any(token in value for token in blocked)


def _extract_model_ids(payload: Any) -> list[str]:
    raw_items = payload.get("data", []) if isinstance(payload, dict) else []
    model_items: list[tuple[str, float | None, int]] = []
    for index, item in enumerate(raw_items):
        model_id = ""
        created_at: float | None = None
        if isinstance(item, dict):
            model_id = str(item.get("id", "")).strip()
            raw_created = item.get("created", item.get("created_at"))
            if isinstance(raw_created, (int, float)):
                created_at = float(raw_created)
        if model_id and _is_chat_model_id(model_id):
            model_items.append((model_id, created_at, index))

    if any(created_at is not None for _, created_at, _ in model_items):
        model_items.sort(
            key=lambda entry: (entry[1] is not None, entry[1] or 0.0, -entry[2]),
            reverse=True,
        )

    models: list[str] = []
    for model_id, _, _ in model_items:
        if model_id not in models:
            models.append(model_id)
    return models


def _build_openai_models_url(base_url: str) -> str:
    root = base_url.strip().rstrip("/")
    if not root:
        return ""

    parsed = urlsplit(root)
    if not parsed.scheme or not parsed.netloc:
        return f"{root}/models"

    path = parsed.path.rstrip("/")
    if path.endswith("/models"):
        next_path = path
    elif not path:
        next_path = "/v1/models"
    else:
        next_path = f"{path}/models"

    return urlunsplit((parsed.scheme, parsed.netloc, next_path, "", ""))


def _merge_models(models: list[str], current_model: str) -> list[str]:
    merged: list[str] = []
    current = current_model.strip()
    for model in models:
        if model and model not in merged:
            merged.append(model)
    if current and current not in merged:
        merged.append(current)
    return merged


def _is_anthropic_model_id(model: str) -> bool:
    value = model.strip().lower()
    return value.startswith("claude-")


def _select_refreshed_model(provider_id: str, models: list[str], current_model: str) -> str:
    current = current_model.strip()
    if provider_id in {"anthropic_off", "custom_anthropic"}:
        if _is_anthropic_model_id(current):
            return current
        return next((model for model in models if _is_anthropic_model_id(model)), models[0] if models else "")
    return current or (models[0] if models else "")


def _manual_models_from_payload(payload: Any) -> list[str]:
    available = getattr(payload, "available_models", [])
    return [model.strip() for model in available if isinstance(model, str) and model.strip()]


def _merge_model_sources(*sources: list[str]) -> list[str]:
    merged: list[str] = []
    for source in sources:
        for model in source:
            value = model.strip() if isinstance(model, str) else ""
            if value and value not in merged:
                merged.append(value)
    return merged


def _persist_refreshed_models(provider: str, models: list[str], current_model: str) -> None:
    if not models:
        return

    payload = get_llm_settings_payload()
    provider_key = "custom" if provider == "custom" else provider
    section = payload.get(provider_key)
    if not isinstance(section, dict):
        return

    next_section = dict(section)
    next_section["available_models"] = _merge_models(models, current_model)
    if current_model:
        next_section["model"] = current_model

    save_payload = {
        "provider": provider,
        "openai": payload.get("openai", {}),
        "anthropic": payload.get("anthropic", {}),
        "custom": payload.get("custom", {}),
    }
    save_payload[provider_key] = next_section
    save_llm_settings(save_payload)
    if _bootstrap is not None:
        _bootstrap.config = load_config()
    _invalidate_status_cache()


def _invalidate_status_cache() -> None:
    global _status_cache_payload, _status_cache_expires_at
    global _capability_cache_payload, _capability_cache_expires_at
    _status_cache_payload = None
    _status_cache_expires_at = 0.0
    _capability_cache_payload = None
    _capability_cache_expires_at = 0.0
    if _bootstrap is not None:
        _bootstrap._status_cache_payload = None
        _bootstrap._status_cache_expires_at = 0.0


async def _fetch_openai_compatible_models(base_url: str, api_key: str) -> list[str]:
    if not base_url.strip() or not api_key.strip():
        return []
    models_url = _build_openai_models_url(base_url)
    if not models_url:
        return []
    proxy_url = os.getenv("LLM_PROXY_URL", "").strip() or os.getenv("MINICODE_LLM_PROXY_URL", "").strip() or os.getenv("OPENAI_PROXY_URL", "").strip()
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        proxy=proxy_url or None,
        trust_env=not bool(proxy_url),
    ) as client:
        response = await client.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
        )
        response.raise_for_status()
        return _extract_model_ids(response.json())


async def _fetch_anthropic_models(base_url: str, api_key: str) -> list[str]:
    if not api_key.strip():
        return []
    endpoint = base_url.rstrip("/") if base_url.strip() else "https://api.anthropic.com/v1"
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        response = await client.get(
            f"{endpoint}/models",
            headers={
                "x-api-key": api_key.strip(),
                "anthropic-version": "2023-06-01",
            },
        )
        response.raise_for_status()
        return _extract_model_ids(response.json())


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    REST 聊天接口（同步式，等待完整结果）。

    适用于简单对话和自动化测试。
    生产环境推荐使用 WebSocket 接口。
    """
    config = _bootstrap.config or load_config()

    # 创建会话级资源
    artifact_store = ArtifactStore()
    tool_registry = _bootstrap.create_tool_registry(artifact_store)
    permission_checker = _bootstrap.create_permission_checker()

    # 创建 LLM 适配器
    try:
        llm = _bootstrap.create_llm()
    except Exception as exc:
        return ChatResponse(
            reply=f"LLM 初始化失败: {exc}",
            stopped_reason="api_error",
            iterations=0,
            tool_calls=[],
        )

    # 运行 Agent Loop，收集所有事件
    reply_parts: list[str] = []
    tool_records: list[ToolCallRecord] = []
    stopped_reason = "completed"
    iterations = 0

    state = AgentState(
        user_message=request.message,
        max_iterations=request.max_iterations,
    )

    async for event in run_agent_loop(
        user_message=request.message,
        llm=llm,
        tool_registry=tool_registry,
        artifact_store=artifact_store,
        permission_checker=permission_checker,
        agent_settings=config.agent,
        token_budget=config.token_budget,
        state=state,
        vector_memory=_bootstrap.vector_memory,
    ):
        if event.type == "text_chunk":
            reply_parts.append(event.data.get("content", ""))

        elif event.type == "tool_result":
            tool_records.append(
                ToolCallRecord(
                    tool_name=event.data.get("name", "unknown"),
                    tool_output=event.data.get("summary", ""),
                    artifact_id=event.data.get("artifact_id"),
                )
            )

        elif event.type == "error":
            error_type = event.data.get("error_type", "api")
            stopped_reason = error_type
            reply_parts.append(event.data.get("message", ""))

        elif event.type == "done":
            stopped_reason = "completed"

    # 从 state 获取更精确的信息
    iterations = state.iterations
    if state.stopped_reason:
        stopped_reason = state.stopped_reason

    # 构建工具调用记录（从 state 获取更完整的信息）
    final_tool_calls = []
    for tc in state.tool_calls:
        final_tool_calls.append(
            ToolCallRecord(
                tool_name=tc.tool_name,
                tool_input=tc.tool_input,
                tool_output=tc.tool_output,
                artifact_id=tc.artifact_id,
                status=tc.status,
            )
        )

    reply = "".join(reply_parts) or state.reply or "（无回复）"

    return ChatResponse(
        reply=reply,
        stopped_reason=stopped_reason,
        iterations=iterations,
        tool_calls=final_tool_calls,
    )


# ── WebSocket 接口 ────────────────────────────────────────

@app.post("/api/uploads", response_model=UploadResponse)
async def upload_document(
    session_id: str = Query(..., min_length=1),
    file: UploadFile = File(...),
) -> UploadResponse:
    """Upload a document into the active WebSocket session."""
    session = _ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{session_id}' is not connected.",
        )

    raw_content = await file.read()
    try:
        result = ingest_uploaded_document(
            file_name=file.filename or "upload.txt",
            raw_content=raw_content,
            artifact_store=session.artifact_store,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to ingest uploaded document '%s': %s", file.filename, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to ingest uploaded document.",
        ) from exc
    finally:
        await file.close()

    if not session.active_conversation_id:
        session._ensure_active_conversation()

    if session.active_conversation_id:
        attachment = result.attachment.to_dict()
        _get_attachment_store().save(
            artifact_id=result.artifact_id,
            content=result.full_text,
            metadata={
                "conversation_id": session.active_conversation_id,
                "attachment": attachment,
            },
        )

    return UploadResponse(
        file_name=result.file_name,
        doc_id=result.doc_id,
        artifact_id=result.artifact_id,
        indexed_chunks=result.indexed_chunks,
        attachment=result.attachment.to_dict(),
    )


@app.get("/api/llm/settings")
async def get_llm_settings_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=30"
    return get_llm_settings_payload()


@app.put("/api/llm/settings")
async def update_llm_settings_api(request: LLMSettingsUpdateRequest) -> dict[str, Any]:
    if not request.confirm_sensitive_change:
        raise HTTPException(
            status_code=409,
            detail="LLM settings changes require explicit confirmation.",
        )
    saved = save_llm_settings(request.model_dump())
    if _bootstrap is not None:
        _bootstrap.config = load_config()
    return saved


@app.post("/api/llm/models/refresh", response_model=LLMModelsRefreshResponse)
async def refresh_llm_models_api(request: LLMSettingsUpdateRequest) -> LLMModelsRefreshResponse:
    provider = _normalize_provider_value(request.provider)

    if provider == "anthropic":
        current = get_anthropic_settings()
        api_key = request.anthropic.api_key.strip() or current["api_key"]
        base_url = request.anthropic.base_url.strip() or current["base_url"]
        current_model = request.anthropic.model.strip() or current["model"]
        provider_id = "anthropic_off"
        models: list[str] = []
        source = "preset"
        source_message = ""
        try:
            models = await _fetch_anthropic_models(base_url, api_key)
        except Exception as exc:
            logger.warning("刷新 Anthropic 模型列表失败: %s", exc)
        if models:
            source = "live"
            source_message = "已从 Anthropic /models 实时拉取可用模型。"
        else:
            models = list(_LATEST_PROVIDER_MODELS[provider_id])
            source_message = (
                "未配置 API Key，当前显示内置兜底模型。"
                if not api_key.strip()
                else "实时拉取失败或返回为空，当前显示内置兜底模型。"
            )
        selected_model = _select_refreshed_model(provider_id, models, current_model)
        final_models = _merge_models(models, selected_model)
        return LLMModelsRefreshResponse(
            provider=provider,
            provider_id=provider_id,
            models=final_models,
            selected_model=selected_model,
            source=source,
            source_message=source_message,
        )

    provider_key = "custom" if provider == "custom" else "openai"
    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    api_key = incoming.api_key.strip() or str(current.get("api_key", "")).strip()
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    current_model = incoming.model.strip() or str(current.get("model", "")).strip()
    wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    provider_id = "custom_anthropic" if provider == "custom" and wire_api == "anthropic" else _resolve_openai_provider_id(base_url)
    models = []
    source = "preset"
    source_message = ""
    manual_models = _manual_models_from_payload(incoming)
    if provider_id == "custom_anthropic":
        manual_models = [model for model in manual_models if _is_anthropic_model_id(model)]
    preset_models = list(_LATEST_PROVIDER_MODELS.get(provider_id, _LATEST_PROVIDER_MODELS.get("anthropic_off", []) if provider_id == "custom_anthropic" else []))
    custom_fallback_models = (
        list(_CUSTOM_GATEWAY_FALLBACK_MODELS)
        if provider == "custom" and provider_id == "custom_openai"
        else []
    )
    try:
        if provider_id == "custom_anthropic":
            models = await _fetch_anthropic_models(base_url, api_key)
        else:
            models = await _fetch_openai_compatible_models(base_url, api_key)
    except Exception as exc:
        logger.warning("刷新 %s 模型列表失败: %s", provider_id, exc)
    if models:
        source = "live"
        models = _merge_model_sources(models, manual_models)
        source_message = "已从当前 Provider 的 /models 接口实时拉取，并按发布时间优先排序。"
    else:
        models = _merge_model_sources(preset_models, manual_models, custom_fallback_models)
        source_message = (
            "未配置 API Key，当前显示内置兜底模型。"
            if not api_key.strip()
            else (
                "实时拉取失败或返回为空，当前显示内置列表。"
                if preset_models
                else "实时拉取失败或返回为空，当前保留手动列表。"
            )
        )
    selected_model = _select_refreshed_model(provider_id, models, current_model)
    final_models = _merge_models(models, selected_model)
    if source == "live":
        _persist_refreshed_models(provider, final_models, selected_model)
    return LLMModelsRefreshResponse(
        provider=provider,
        provider_id=provider_id,
        models=final_models,
        selected_model=selected_model,
        source=source,
        source_message=source_message,
    )


@app.get("/api/mcp/config")
async def get_mcp_config_api(response: Response) -> dict[str, Any]:
    """Read the local .mcp.json file for the Settings center."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return read_mcp_config(MCP_CONFIG_FILE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read MCP config: {exc}") from exc


@app.put("/api/mcp/config")
async def update_mcp_config_api(request: MCPConfigUpdateRequest, response: Response) -> dict[str, Any]:
    """Validate, backup, save, and optionally reload the local .mcp.json file."""
    response.headers["Cache-Control"] = "no-store"
    if not request.confirm_sensitive_change:
        raise HTTPException(
            status_code=409,
            detail="MCP config changes require explicit confirmation.",
        )

    try:
        result = write_mcp_config(request.content, MCP_CONFIG_FILE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save MCP config: {exc}") from exc

    _invalidate_status_cache()

    if request.reload and _bootstrap is not None and _bootstrap.mcp_manager is not None:
        await _bootstrap.mcp_manager.stop_all()
        await _bootstrap.mcp_manager.start_all()

    return {
        **result,
        "mcp": get_mcp_status(),
    }


@app.get("/api/skills/marketplace")
async def get_skills_marketplace_api(response: Response) -> dict[str, Any]:
    """Compatibility endpoint for clients that only know about Skill marketplace entries."""
    response.headers["Cache-Control"] = "no-store"
    installed = set()
    if _bootstrap is not None and _bootstrap.skill_manager is not None:
        installed = {str(skill.get("name")) for skill in _bootstrap.skill_manager.list_all() if isinstance(skill, dict)}
    payload = await list_extensions_marketplace(installed_names=installed)
    return {
        "skills": payload["skills"],
        "source_status": {"openai_skills": payload["source_status"]["openai_skills"]},
        "generated_at": payload["generated_at"],
    }


@app.get("/api/extensions/marketplace")
async def get_extensions_marketplace_api(response: Response) -> dict[str, Any]:
    """List real Skills and MCP marketplace entries from upstream catalogs with safe fallbacks."""
    response.headers["Cache-Control"] = "no-store"
    installed = set()
    if _bootstrap is not None and _bootstrap.skill_manager is not None:
        installed = {str(skill.get("name")) for skill in _bootstrap.skill_manager.list_all() if isinstance(skill, dict)}
    return await list_extensions_marketplace(installed_names=installed)


@app.post("/api/skills/install")
async def install_skill_api(request: SkillInstallRequest, response: Response) -> dict[str, Any]:
    """Install an OpenAI curated Skill as a real SKILL.md file, then refresh Skill discovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await install_marketplace_skill(request.skill_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to install Skill: {exc}") from exc

    skills: list[dict[str, Any]] = []
    if _bootstrap is not None and _bootstrap.skill_manager is not None:
        _bootstrap.skill_manager.discover()
        skills = _bootstrap.skill_manager.list_all()
    _invalidate_status_cache()

    return {
        **result,
        "skills": skills,
    }


# ── UI Preferences API ────────────────────────────────────────

@app.delete("/api/skills/{skill_name}")
async def remove_skill_api(skill_name: str, response: Response) -> dict[str, Any]:
    """Remove a user-installed Skill directory, then refresh Skill discovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = remove_user_skill(skill_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to remove Skill: {exc}") from exc

    skills: list[dict[str, Any]] = []
    if _bootstrap is not None and _bootstrap.skill_manager is not None:
        _bootstrap.skill_manager.deactivate(result["skill"]["name"])
        _bootstrap.skill_manager.discover()
        skills = _bootstrap.skill_manager.list_all()
    _invalidate_status_cache()

    return {
        **result,
        "skills": skills,
    }


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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket 聊天接口（DESIGN.md §10 协议）。

    前端 → 后端：
      { type: "user_message",  content: string }
      { type: "approval",      tool_call_id: string, action: "approve"|"reject" }
      { type: "interrupt" }
      { type: "load_skill",    skill_name: string }

    后端 → 前端：
      { type: "text_chunk",       content: string }
      { type: "tool_call",        id: string, name: string, args: object }
      { type: "tool_result",      id: string, summary: string, artifact_id?: string }
      { type: "approval_request", tool_call_id: string, tool_name: string, ... }
      { type: "done",             usage: { input_tokens, output_tokens } }
      { type: "error",            message: string, recoverable: boolean }
    """
    if not _is_websocket_authorized(websocket):
        await websocket.accept()
        await websocket.close(code=1008)
        return

    config = _bootstrap.config or load_config()

    # 创建 LLM 适配器
    try:
        llm = _bootstrap.create_llm()
    except Exception as exc:
        await websocket.accept()
        await websocket.send_json(
            AgentEvent.error(f"LLM 初始化失败: {exc}", recoverable=False).to_ws_message()
        )
        await websocket.close()
        return

    # 创建会话级资源
    artifact_store = ArtifactStore()
    tool_registry = _bootstrap.create_tool_registry(artifact_store)
    permission_checker = _bootstrap.create_permission_checker()

    session, connection_generation = await _ws_manager.connect(
        websocket=websocket,
        llm=llm,
        artifact_store=artifact_store,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        config=config,
        skill_manager=_bootstrap.skill_manager,
        skill_executor=_bootstrap.skill_executor,
        rag_pipeline=_bootstrap.rag_pipeline,
        memory_manager=_bootstrap.file_memory,
        vector_memory=_bootstrap.vector_memory,
    )

    try:
        await session.handle(connection_generation=connection_generation)
    finally:
        _ws_manager.disconnect(
            session.session_id,
            connection_generation=connection_generation,
        )


# ── 健康检查 ──────────────────────────────────────────────

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """健康检查端点。"""
    result: dict[str, Any] = {
        "status": "ok",
        "version": "0.2.0",
        "active_sessions": _ws_manager.active_count,
    }

    if _bootstrap is not None:
        # MCP 状态
        if _bootstrap.mcp_manager:
            result["mcp_servers"] = _bootstrap.mcp_manager.get_all_status()

        # Skills 状态
        if _bootstrap.skill_manager:
            result["skills_count"] = len(_bootstrap.skill_manager.list_all())

        # RAG 状态
        if _bootstrap.rag_pipeline:
            result["rag"] = _bootstrap.rag_pipeline.stats

    return result


@app.get("/api/status")
async def system_status(response: Response) -> dict[str, Any]:
    """
    系统状态 API（供前端侧边栏使用）。

    返回 MCP Server、Skills、Memory、RAG 的实时状态。
    """
    response.headers["Cache-Control"] = "public, max-age=30"
    return {
        **_get_cached_status_payload(),
        "llm": _build_llm_status_payload(),
    }


@app.get("/api/doctor")
async def doctor_status(response: Response) -> dict[str, Any]:
    """Aggregate desktop workbench diagnostics for the right-side Doctor panel."""
    response.headers["Cache-Control"] = "no-store"
    return _build_doctor_payload()


@app.get("/api/guidelines")
async def project_guidelines_status(
    response: Response,
    workspace_dir: str | None = Query(default=None),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    bundle = load_project_guideline_bundle(workspace_dir=workspace_dir)
    return bundle.to_dict()


def _get_cached_status_payload() -> dict[str, Any]:
    if _bootstrap is not None:
        return _bootstrap.build_status_payload()

    global _status_cache_payload, _status_cache_expires_at

    now = time.monotonic()
    if _status_cache_payload is not None and now < _status_cache_expires_at:
        return _status_cache_payload

    _status_cache_payload = _build_status_payload()
    _status_cache_expires_at = now + STATUS_CACHE_TTL_SECONDS
    return _status_cache_payload


def _build_status_payload() -> dict[str, Any]:
    mcp_mgr = _bootstrap.mcp_manager if _bootstrap else None
    skill_mgr = _bootstrap.skill_manager if _bootstrap else None
    file_mem = _bootstrap.file_memory if _bootstrap else None
    rag = _bootstrap.rag_pipeline if _bootstrap else None
    return {
        "mcp": mcp_mgr.get_all_status() if mcp_mgr else [],
        "skills": skill_mgr.list_all() if skill_mgr else [],
        "runtime": _ws_manager.runtime_snapshot(),
        "memory": {
            "available": file_mem is not None,
            "files": file_mem.list_files() if file_mem else [],
        },
        "rag": rag.stats if rag else {"available": False},
        "capabilities": _build_capability_status_payload(),
    }


def _build_capability_status_payload() -> dict[str, Any]:
    global _capability_cache_payload, _capability_cache_expires_at
    import time
    now = time.monotonic()
    if _capability_cache_payload is not None and now < _capability_cache_expires_at:
        return _capability_cache_payload
    try:
        vec_mem = _bootstrap.vector_memory if _bootstrap else None
        registry = _build_tool_registry(ArtifactStore(), vec_mem)
        snapshot = registry.build_snapshot()
        snapshot["composer_commands"] = get_enabled_composer_command_catalog()
        _capability_cache_payload = snapshot
        _capability_cache_expires_at = now + _CAPABILITY_CACHE_TTL_SECONDS
        return snapshot
    except Exception as exc:
        logger.warning("Capability snapshot build failed: %s", exc)
        result = {
            "version": 0,
            "tools": [],
            "commands": [],
            "skills": [],
            "composer_commands": get_enabled_composer_command_catalog(),
        }
        _capability_cache_payload = result
        _capability_cache_expires_at = now + 5
        return result


def _build_llm_status_payload() -> dict[str, Any]:
    llm_settings = get_llm_settings_payload()
    current_model = str(llm_settings.get("active_model", "")).strip()
    return {
        "provider": get_llm_provider(),
        "current_model": current_model,
        "active_model": current_model,
        "available_models": get_available_models(),
    }


def _build_doctor_payload() -> dict[str, Any]:
    workspace_root = get_active_workspace_root(PROJECT_ROOT).resolve()
    git_status = _build_git_doctor_payload(workspace_root)
    preview_processes = _build_preview_doctor_payload()
    runtime = _ws_manager.runtime_snapshot()
    llm = _build_llm_status_payload()
    llm_settings = get_llm_settings_payload()
    active_provider = str(llm_settings.get("provider") or llm.get("provider") or "")
    provider_payload = llm_settings.get(active_provider)
    if isinstance(provider_payload, dict):
        llm["has_api_key"] = bool(provider_payload.get("has_api_key"))
        llm["base_url"] = provider_payload.get("base_url") or ""

    return {
        "backend": {
            "status": "ok",
            "version": "0.2.0",
            "active_sessions": _ws_manager.active_count,
            "runtime": runtime,
        },
        "llm": llm,
        "mcp": get_mcp_status(),
        "workspace": {
            "root": str(workspace_root),
            "exists": workspace_root.exists(),
            "writable": os.access(workspace_root, os.W_OK),
        },
        "git": git_status,
        "preview": {
            "count": len(preview_processes),
            "processes": preview_processes,
            "url": next((item.get("url") for item in preview_processes if item.get("url")), ""),
        },
        "terminal": {
            "active_sessions": _ws_manager.active_count,
            "runtime": "desktop-or-websocket",
        },
    }


def _build_git_doctor_payload(workspace_root: Any) -> dict[str, Any]:
    try:
        import subprocess

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace_root,
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=3,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=workspace_root,
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=5,
        )
        changes = [line for line in status_result.stdout.splitlines() if line.strip()]
        return {
            "available": branch_result.returncode == 0 or status_result.returncode == 0,
            "branch": branch_result.stdout.strip(),
            "changed": len(changes),
            "clean": len(changes) == 0,
            "error": branch_result.stderr.strip() or status_result.stderr.strip(),
        }
    except Exception as exc:
        return {"available": False, "branch": "", "changed": 0, "clean": False, "error": str(exc)}


def _build_preview_doctor_payload() -> list[dict[str, Any]]:
    try:
        from backend.preview import running_preview_processes

        return [process.to_dict() for process in running_preview_processes()]
    except Exception as exc:
        logger.warning("Preview doctor payload failed: %s", exc)
        return []


def get_mcp_status() -> list[dict[str, Any]]:
    """获取 MCP Server 状态列表（供 WS handler 推送 mcp_status 事件）。"""
    if _bootstrap is not None:
        return _bootstrap.get_mcp_status()
    return []


def get_mcp_manager():
    """获取 MCP Server Manager 实例（供 WS command handler 管理 MCP 服务器）。"""
    if _bootstrap is not None:
        return _bootstrap.mcp_manager
    return None
