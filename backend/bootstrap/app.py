from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from inspect import signature
from typing import Any

from backend.config import AppConfig, load_config
from backend.memory.file_memory import FileMemory
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_STARTUP_STEP_TIMEOUT_SECONDS = 8.0
_STARTUP_TIMED_OUT = object()


async def _with_startup_timeout(label: str, awaitable: Awaitable[Any], timeout: float = _STARTUP_STEP_TIMEOUT_SECONDS) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError:
        logger.warning("%s init timed out after %.1fs; continuing without it", label, timeout)
        return _STARTUP_TIMED_OUT


async def _to_thread_with_timeout(label: str, func: Callable[[], Any], timeout: float = _STARTUP_STEP_TIMEOUT_SECONDS) -> Any:
    return await _with_startup_timeout(label, asyncio.to_thread(func), timeout=timeout)


class AppBootstrap:
    """Central composition root for shared backend services."""

    def __init__(
        self,
        *,
        build_tool_registry: Callable[..., ToolRegistry],
        build_status_payload: Callable[[], dict[str, Any]] | None = None,
        create_llm_adapter: Callable[..., Any],
        ws_manager: Any,
        on_mcp_status_change: Callable[[str, Any], Awaitable[None]],
        status_cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._build_tool_registry = build_tool_registry
        self._build_status_payload = build_status_payload
        self._create_llm_adapter = create_llm_adapter
        self.ws_manager = ws_manager
        self._on_mcp_status_change = on_mcp_status_change
        self._status_cache_ttl_seconds = status_cache_ttl_seconds

        self.config: AppConfig | None = None
        self.mcp_manager: Any | None = None
        self.skill_manager: Any | None = None
        self.skill_executor: Any | None = None
        self.file_memory: Any | None = None
        self.vector_memory: Any | None = None
        self.rag_pipeline: Any | None = None
        self._status_cache_payload: dict[str, Any] | None = None
        self._status_cache_expires_at = 0.0
        self.task_scheduler: Any | None = None

    async def startup(self) -> None:
        self.config = load_config()

        try:
            self.file_memory = FileMemory()
            logger.info("File memory is ready")
        except Exception as exc:
            logger.warning("File memory init failed: %s", exc)

        try:
            from backend.memory.vector_memory import VectorMemory

            self.vector_memory = await _to_thread_with_timeout("Vector memory", VectorMemory)
            if self.vector_memory is _STARTUP_TIMED_OUT:
                self.vector_memory = None
            elif self.vector_memory is not None:
                logger.info("Vector memory is ready")
        except Exception as exc:
            logger.warning("Vector memory init failed: %s", exc)

        try:
            from backend.skills.executor import SkillExecutor
            from backend.skills.loader import SkillLoader
            from backend.skills.manager import SkillManager

            loader = SkillLoader()
            self.skill_manager = SkillManager(loader=loader)
            self.skill_manager.discover()
            self.skill_executor = SkillExecutor(self.skill_manager)
            logger.info("Skills are ready")
        except Exception as exc:
            logger.warning("Skills init failed: %s", exc)

        try:
            from backend.rag.pipeline import RAGPipeline

            self.rag_pipeline = RAGPipeline()
            if self.rag_pipeline is None:
                raise TimeoutError("RAG pipeline init timed out")
            available = await _to_thread_with_timeout("RAG availability", self.rag_pipeline.is_available, timeout=5.0)
            if available is _STARTUP_TIMED_OUT:
                self.rag_pipeline = None
            elif available:
                try:
                    await _with_startup_timeout("RAG warmup", self.rag_pipeline.warmup_async(), timeout=5.0)
                except Exception as warmup_exc:
                    logger.debug("RAG warmup failed: %s", warmup_exc)
            else:
                self.rag_pipeline = None
            logger.info("RAG pipeline initialized")
        except Exception as exc:
            logger.warning("RAG init failed: %s", exc)

        try:
            from backend.mcp.manager import MCPServerManager

            self.mcp_manager = MCPServerManager(on_status_change=self._on_mcp_status_change)
            started = await _with_startup_timeout("MCP manager", self.mcp_manager.start_all(), timeout=30.0)
            if started is _STARTUP_TIMED_OUT:
                # 不销毁整个 MCP manager — 让慢速服务器（如 npx）在后台继续连接。
                # 后端已经可用，WebSocket 客户端会在服务器连接后收到 mcp.lifecycle 事件。
                logger.warning(
                    "MCP manager startup timed out; some servers may still be connecting in background"
                )
            else:
                logger.info("MCP manager initialized")
        except Exception as exc:
            logger.warning("MCP init failed: %s", exc)

        try:
            from backend.hooks.manager import HookManager, set_hook_manager
            from backend.config import SETTINGS_FILE
            from pathlib import Path
            import json

            settings_data: dict[str, Any] = {}
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings_data = json.load(f)
            project_root = Path(SETTINGS_FILE).resolve().parent if SETTINGS_FILE.exists() else None
            hook_mgr = HookManager.from_settings(settings_data, workspace_root=project_root)
            set_hook_manager(hook_mgr)
            hook_count = len(hook_mgr.pre_tool) + len(hook_mgr.post_tool)
            if hook_count:
                logger.info("Hooks loaded: %d pre_tool, %d post_tool", len(hook_mgr.pre_tool), len(hook_mgr.post_tool))
        except Exception as exc:
            logger.warning("Hooks init failed: %s", exc)

        try:
            from backend.tasks.scheduler import get_global_scheduler
            self.task_scheduler = get_global_scheduler()
            await self.task_scheduler.start()
            logger.info("Task scheduler is ready")
        except Exception as exc:
            logger.warning("Task scheduler init failed: %s", exc)

    async def shutdown(self) -> None:
        if self.task_scheduler:
            try:
                await self.task_scheduler.stop()
            except Exception:
                pass
        if self.mcp_manager:
            await self.mcp_manager.stop_all()
            logger.info("MCP manager stopped")

    def refresh_config(self) -> AppConfig:
        self.config = load_config()
        return self.config

    def create_tool_registry(self, artifact_store: Any) -> ToolRegistry:
        try:
            params = signature(self._build_tool_registry).parameters
            kwargs: dict[str, Any] = {}
            if "llm_provider" in params:
                kwargs["llm_provider"] = self.create_llm
            if "mcp_manager" in params:
                kwargs["mcp_manager"] = self.mcp_manager
            if kwargs:
                return self._build_tool_registry(
                    artifact_store,
                    self.vector_memory,
                    **kwargs,
                )
        except (TypeError, ValueError):
            pass
        return self._build_tool_registry(artifact_store, self.vector_memory)

    def create_permission_checker(self) -> PermissionChecker:
        config = self.config or self.refresh_config()
        return PermissionChecker(config.permissions)

    def create_llm(self, *, model_override: str | None = None) -> Any:
        config = self.config or self.refresh_config()
        try:
            return self._create_llm_adapter(config, model_override=model_override)
        except TypeError:
            return self._create_llm_adapter(config)

    def build_status_payload(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._status_cache_payload is not None and now < self._status_cache_expires_at:
            return self._status_cache_payload

        if self._build_status_payload is not None:
            self._status_cache_payload = self._build_status_payload()
        else:
            self._status_cache_payload = {
                "mcp": self.mcp_manager.get_all_status() if self.mcp_manager else [],
                "skills": self.skill_manager.list_all() if self.skill_manager else [],
                "memory": {
                    "available": self.file_memory is not None,
                    "files": self.file_memory.list_files() if self.file_memory else [],
                },
                "rag": self.rag_pipeline.stats if self.rag_pipeline else {"available": False},
                "capabilities": self.build_capability_snapshot(),
            }
        self._status_cache_expires_at = now + self._status_cache_ttl_seconds
        return self._status_cache_payload

    def get_mcp_status(self) -> list[dict[str, Any]]:
        if self.mcp_manager:
            return self.mcp_manager.get_all_status()
        return []

    def build_capability_snapshot(self) -> dict[str, Any]:
        from backend.artifact.store import ArtifactStore

        registry = self.create_tool_registry(ArtifactStore())
        return registry.build_snapshot()
