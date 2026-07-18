from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from inspect import signature
from typing import Any

from backend.config import AppConfig, DATA_ROOT, PROJECT_ROOT, load_config
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
        # Legacy compatibility surface only. Agent memory no longer initializes
        # a default vector store; document ingestion owns its own vector index.
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
            from backend.skills.executor import SkillExecutor
            from backend.skills.loader import SkillLoader
            from backend.skills.manager import SkillManager

            loader = SkillLoader()
            self.skill_manager = SkillManager(
                loader=loader,
                usage_store_path=DATA_ROOT / "skills" / "usage.json",
            )
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
            self.mcp_manager = MCPServerManager(
                on_status_change=self._on_mcp_status_change,
                sampling_handler=self._handle_mcp_sampling,
                elicitation_handler=self._handle_mcp_elicitation,
            )
            mcp_start_task = asyncio.create_task(self.mcp_manager.start_all())
            started = await _with_startup_timeout("MCP manager", asyncio.shield(mcp_start_task), timeout=30.0)
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
            await _with_startup_timeout("Setup hook", hook_mgr.run_setup(trigger="startup"), timeout=5.0)
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
        try:
            if self.task_scheduler:
                try:
                    await self.task_scheduler.stop()
                except Exception as exc:
                    logger.debug("Task scheduler stop error (harmless): %s", exc)
            if self.mcp_manager:
                await self.mcp_manager.stop_all()
                logger.info("MCP manager stopped")
        finally:
            # The hook manager is process-global. Leaving a test/application
            # instance installed leaks hooks into the next bootstrap and can
            # execute stale or incomplete manager implementations.
            from backend.hooks.manager import set_hook_manager

            set_hook_manager(None)

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
                    None,
                    **kwargs,
                )
        except (TypeError, ValueError):
            pass
        return self._build_tool_registry(artifact_store, None)

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

    async def _handle_mcp_sampling(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle sampling/createMessage request from MCP server."""
        from backend.llm.base import LLMMessage

        logger.info("Received MCP sampling request: %s", params)
        mcp_messages = params.get("messages", [])

        # Parse MCP message schema to unified LLMMessage
        llm_messages: list[LLMMessage] = []
        for msg in mcp_messages:
            role = str(msg.get("role") or "user")
            content_field = msg.get("content")

            content_text = ""
            images = []

            blocks = []
            if isinstance(content_field, list):
                blocks = content_field
            elif isinstance(content_field, dict):
                blocks = [content_field]
            elif isinstance(content_field, str):
                content_text = content_field

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                b_type = str(block.get("type") or "").strip().lower()
                if b_type == "text":
                    content_text += str(block.get("text") or "")
                elif b_type == "image":
                    images.append({
                        "media_type": str(block.get("mimeType") or "image/png"),
                        "data": str(block.get("data") or ""),
                    })

            llm_messages.append(LLMMessage(
                role=role,
                content=content_text,
                images=images,
            ))

        system_prompt = str(params.get("systemPrompt") or "").strip()
        if system_prompt:
            llm_messages.insert(0, LLMMessage(role="system", content=system_prompt))

        model_preferences = params.get("modelPreferences") or {}
        model_hint = None
        hints = model_preferences.get("hints") or []
        if hints and isinstance(hints, list) and isinstance(hints[0], dict):
            model_hint = str(hints[0].get("name") or "")

        adapter = self.create_llm(model_override=model_hint)
        reply_text = await adapter.simple_chat(llm_messages)

        return {
            "model": getattr(adapter, "model_name", "default-model"),
            "stopReason": "stop",
            "role": "assistant",
            "content": {
                "type": "text",
                "text": reply_text,
            }
        }

    async def _handle_mcp_elicitation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle elicitation/create request from MCP server."""
        import uuid

        logger.info("Received MCP elicitation request: %s", params)
        if not self.ws_manager:
            return {"action": "cancel", "error": "WebSocket manager not initialized"}

        # Find active session
        session = None
        for s in self.ws_manager._sessions.values():
            if s._is_connected and getattr(s, "_active_run_task", None):
                session = s
                break
        if not session:
            for s in self.ws_manager._sessions.values():
                if s._is_connected:
                    session = s
                    break
        if not session:
            return {"action": "cancel", "error": "No active client session connected"}

        request_id = f"elicit_{uuid.uuid4().hex}"
        prompt = str(params.get("prompt") or "").strip()
        schema = params.get("schema") or {}

        # Build the payload
        if session._use_control_protocol:
            payload = {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "elicitation",
                    "tool_use_id": request_id,
                    "prompt": prompt,
                    "question": prompt,
                    "schema": schema,
                }
            }
        else:
            payload = {
                "type": "elicitation_request",
                "request_id": request_id,
                "prompt": prompt,
                "schema": schema,
            }

        # Register future for async wait-response
        future = asyncio.get_running_loop().create_future()
        session._pending_approvals[request_id] = future
        session._pending_approval_payloads[request_id] = payload

        # Send event to client
        await session._send_ws_payload(payload, log_context="mcp:elicitation")

        try:
            # Wait for user input from the desktop app (up to 5 minutes)
            result = await asyncio.wait_for(future, timeout=300)

            # The client responds via "answer" or "approval" command, which resolves
            # the future with the payload. Let's inspect result structure:
            answer = result.get("answer") or result.get("content") or ""
            return {
                "action": "submit",
                "response": {
                    "answer": answer
                }
            }
        except asyncio.TimeoutError:
            return {"action": "cancel", "error": "User response timed out"}
        except asyncio.CancelledError:
            return {"action": "cancel", "error": "Interaction cancelled"}
        finally:
            session._pending_approvals.pop(request_id, None)
            session._pending_approval_payloads.pop(request_id, None)
