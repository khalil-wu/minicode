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

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.agent.context import ContextBuilder
from backend.agent.loop import run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, load_config, load_llm_settings
from backend.llm.openai_adapter import OpenAIAdapter
from backend.memory.file_memory import FileMemory
from backend.permissions.checker import PermissionChecker
from backend.tools.agent_tools import AskUserTool, ReadArtifactTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from backend.tools.registry import ToolRegistry
from backend.tools.search_tools import GrepFilesTool
from backend.ws.handler import WebSocketManager

logger = logging.getLogger(__name__)

# ── 全局状态 ──────────────────────────────────────────────
_config: AppConfig | None = None
_ws_manager = WebSocketManager()
_mcp_manager = None       # MCPServerManager 实例
_mcp_tool_registry = None # MCPToolRegistry 实例
_skill_manager = None     # SkillManager 实例
_skill_executor = None    # SkillExecutor 实例
_file_memory = None       # FileMemory 实例
_rag_pipeline = None      # RAGPipeline 实例


def _build_tool_registry(artifact_store: ArtifactStore) -> ToolRegistry:
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

    # 命令工具
    registry.register(RunCommandTool(artifact_store))

    # Agent 辅助工具
    registry.register(AskUserTool())
    registry.register(ReadArtifactTool(artifact_store))

    # ── 记忆工具（DESIGN.md §2.2）──
    if _file_memory:
        from backend.tools.memory_tools import ReadMemoryTool, SaveMemoryTool
        registry.register(ReadMemoryTool(_file_memory))
        registry.register(SaveMemoryTool(_file_memory))

    # ── MCP 动态工具注册（DESIGN.md §6.3）──
    if _mcp_manager:
        try:
            from backend.mcp.registry import MCPToolRegistry
            mcp_registry = MCPToolRegistry(registry, artifact_store)
            all_tools = _mcp_manager.get_all_tools()
            for server_name, tools in all_tools.items():
                client = _mcp_manager.get_client(server_name)
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

    return registry


# ── FastAPI 应用 ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    global _config, _mcp_manager, _mcp_tool_registry
    global _skill_manager, _skill_executor, _file_memory, _rag_pipeline

    _config = load_config()

    # ── 初始化文件记忆（DESIGN.md §2.2）──
    try:
        _file_memory = FileMemory()
        logger.info("文件记忆系统就绪")
    except Exception as exc:
        logger.warning("文件记忆初始化失败: %s", exc)

    # ── 初始化 Skills 系统（DESIGN.md §5）──
    try:
        from backend.skills.loader import SkillLoader
        from backend.skills.manager import SkillManager
        from backend.skills.executor import SkillExecutor

        loader = SkillLoader()
        _skill_manager = SkillManager(loader=loader)
        skills = _skill_manager.discover()
        _skill_executor = SkillExecutor(_skill_manager)
        logger.info("Skills 系统就绪，发现 %d 个 Skills", len(skills))
    except Exception as exc:
        logger.warning("Skills 系统初始化失败: %s", exc)

    # ── 初始化被动 RAG（DESIGN.md §4.1）──
    try:
        from backend.rag.pipeline import RAGPipeline
        _rag_pipeline = RAGPipeline()
        if _rag_pipeline.is_available():
            logger.info("被动 RAG 流水线就绪")
        else:
            logger.info("被动 RAG 未就绪（记忆库为空或 chromadb 未安装）")
    except Exception as exc:
        logger.warning("被动 RAG 初始化失败: %s", exc)

    # ── 初始化 MCP Server 管理器（DESIGN.md §6）──
    try:
        from backend.mcp.manager import MCPServerManager
        from backend.mcp.registry import MCPToolRegistry

        _mcp_manager = MCPServerManager()
        # MCP 工具注册在每个会话的 tool_registry 中，此处只初始化管理器
        # 启动所有配置的 MCP Server
        await _mcp_manager.start_all()
        logger.info(
            "MCP 管理器就绪，%d 个 Server 已连接",
            _mcp_manager.connected_count,
        )
    except Exception as exc:
        logger.warning("MCP 管理器初始化失败: %s", exc)

    logger.info("MiniCode Backend 启动完成")
    yield

    # ── 关闭清理 ──
    if _mcp_manager:
        await _mcp_manager.stop_all()
        logger.info("MCP Server 已全部关闭")
    logger.info("MiniCode Backend 关闭")


app = FastAPI(
    title="MiniCode Agent API",
    description="AI 编程助手后端 — 支持 REST 和 WebSocket 两种接口",
    version="0.2.0",
    lifespan=lifespan,
)

# ── 静态文件服务 ────────────────────────────────────────────
from backend.config import PROJECT_ROOT

FRONTEND_DIR = PROJECT_ROOT / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    """服务前端首页。"""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    REST 聊天接口（同步式，等待完整结果）。

    适用于简单对话和自动化测试。
    生产环境推荐使用 WebSocket 接口。
    """
    config = _config or load_config()

    # 创建会话级资源
    artifact_store = ArtifactStore()
    tool_registry = _build_tool_registry(artifact_store)
    permission_checker = PermissionChecker(config.permissions)

    # 创建 LLM 适配器
    try:
        llm = OpenAIAdapter(settings=config.llm)
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
    config = _config or load_config()

    # 创建 LLM 适配器
    try:
        llm = OpenAIAdapter(settings=config.llm)
    except Exception as exc:
        await websocket.accept()
        await websocket.send_json(
            AgentEvent.error(f"LLM 初始化失败: {exc}", recoverable=False).to_ws_message()
        )
        await websocket.close()
        return

    # 创建会话级资源
    artifact_store = ArtifactStore()
    tool_registry = _build_tool_registry(artifact_store)
    permission_checker = PermissionChecker(config.permissions)

    session = await _ws_manager.connect(
        websocket=websocket,
        llm=llm,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        config=config,
        skill_manager=_skill_manager,
        skill_executor=_skill_executor,
        rag_pipeline=_rag_pipeline,
        memory_manager=_file_memory,
    )

    try:
        await session.handle()
    finally:
        _ws_manager.disconnect(session.session_id)


# ── 健康检查 ──────────────────────────────────────────────

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """健康检查端点。"""
    result: dict[str, Any] = {
        "status": "ok",
        "version": "0.2.0",
        "active_sessions": _ws_manager.active_count,
    }

    # MCP 状态
    if _mcp_manager:
        result["mcp_servers"] = _mcp_manager.get_all_status()

    # Skills 状态
    if _skill_manager:
        result["skills_count"] = len(_skill_manager.list_all())

    # RAG 状态
    if _rag_pipeline:
        result["rag"] = _rag_pipeline.stats

    return result


@app.get("/api/status")
async def system_status() -> dict[str, Any]:
    """
    系统状态 API（供前端侧边栏使用）。

    返回 MCP Server、Skills、Memory、RAG 的实时状态。
    """
    return {
        "mcp": _mcp_manager.get_all_status() if _mcp_manager else [],
        "skills": _skill_manager.list_all() if _skill_manager else [],
        "memory": {
            "available": _file_memory is not None,
            "files": _file_memory.list_files() if _file_memory else [],
        },
        "rag": _rag_pipeline.stats if _rag_pipeline else {"available": False},
    }
