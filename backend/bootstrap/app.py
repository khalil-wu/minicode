from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from inspect import signature
from typing import Any

from backend.config import AppConfig, DATA_ROOT, PROJECT_ROOT, load_config
from backend.memory.file_memory import FileMemory
from backend.memory.manager import MemoryManager
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_STARTUP_STEP_TIMEOUT_SECONDS = 8.0
_STARTUP_TIMED_OUT = object()
_MCP_SAMPLING_MAX_MESSAGES = 16
_MCP_SAMPLING_MAX_TEXT_CHARS = 24_000
_MCP_SAMPLING_MAX_IMAGE_BYTES = 4 * 1024 * 1024


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
        self.memory_manager: Any | None = None
        self._status_cache_payload: dict[str, Any] | None = None
        self._status_cache_expires_at = 0.0
        self.task_scheduler: Any | None = None
        self._pr_monitor_task: asyncio.Task[None] | None = None
        self._sandbox_probe_task: asyncio.Task[bool] | None = None

    async def startup(self) -> None:
        from backend.permissions.profiles import refresh_native_os_sandbox

        # Capability discovery may invoke a container CLI. Warm its cache in
        # parallel with normal startup so session snapshots remain nonblocking.
        self._sandbox_probe_task = asyncio.create_task(
            asyncio.to_thread(refresh_native_os_sandbox)
        )
        self.config = load_config()

        try:
            self.file_memory = FileMemory()
            self.memory_manager = MemoryManager(self.file_memory)
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
            )
            self.skill_manager.discover()
            self.skill_executor = SkillExecutor(self.skill_manager)
            logger.info("Skills are ready")
        except Exception as exc:
            logger.warning("Skills init failed: %s", exc)

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
            self.task_scheduler = get_global_scheduler(on_fire=self._run_scheduled_task)
            self.task_scheduler.set_on_change(self._broadcast_scheduled_task_state)
            await self.task_scheduler.start()
            logger.info("Task scheduler is ready")
        except Exception as exc:
            logger.warning("Task scheduler init failed: %s", exc)

        self._pr_monitor_task = asyncio.create_task(self._run_pr_automation_monitor())

    async def _run_scheduled_task(self, task: Any, run: Any) -> dict[str, Any]:
        from backend.services.scheduled_task_runner import run_scheduled_task

        return await run_scheduled_task(task, run, bootstrap=self)

    async def _broadcast_scheduled_task_state(self) -> None:
        if self.task_scheduler is None:
            return
        sessions = list(getattr(self.ws_manager, "_sessions", {}).values())
        for session in sessions:
            if not getattr(session, "_is_connected", False):
                continue
            try:
                workspace_root = str(session._current_workspace_root() or "")
                payload = {
                    "type": "scheduler.list",
                    "tasks": self.task_scheduler.list_tasks(workspace_root=workspace_root),
                    "runs": self.task_scheduler.list_runs(workspace_root=workspace_root),
                }
                await session._send_ws_payload(payload, log_context="scheduler.list")
            except Exception:
                logger.debug("Failed to broadcast scheduler update", exc_info=True)

    async def _run_pr_automation_monitor(self) -> None:
        """Poll enabled workspaces so Auto-fix does not depend on an open panel."""
        try:
            while True:
                await asyncio.sleep(60)
                await self._poll_pr_automation_once()
        except asyncio.CancelledError:
            return

    async def _poll_pr_automation_once(self) -> None:
        """Poll each connected workspace once, even when several windows use it."""

        from pathlib import Path

        from backend.services.workspace_service import read_pr_automation
        from backend.ws.handlers.workspace import handle_git_pr_status

        sessions_by_workspace: dict[str, Any] = {}
        for session in list(getattr(self.ws_manager, "_sessions", {}).values()):
            if not getattr(session, "_is_connected", False):
                continue
            try:
                raw_root = session._current_workspace_root()
                if not raw_root:
                    continue
                workspace_key = str(Path(raw_root).expanduser().resolve(strict=False)).casefold()
                sessions_by_workspace.setdefault(workspace_key, session)
            except (OSError, RuntimeError, ValueError):
                logger.debug("PR automation ignored an invalid workspace", exc_info=True)

        for session in sessions_by_workspace.values():
            try:
                workspace_root = session._current_workspace_root()
                if not read_pr_automation(workspace_root).get("auto_fix"):
                    continue
                await handle_git_pr_status(session, {})
            except Exception:
                logger.debug("PR automation poll failed", exc_info=True)

    async def shutdown(self) -> None:
        try:
            if self._sandbox_probe_task is not None:
                if not self._sandbox_probe_task.done():
                    self._sandbox_probe_task.cancel()
                try:
                    await self._sandbox_probe_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.debug("Sandbox capability probe error (harmless): %s", exc)
                self._sandbox_probe_task = None
            if self._pr_monitor_task is not None:
                self._pr_monitor_task.cancel()
                try:
                    await self._pr_monitor_task
                except asyncio.CancelledError:
                    pass
                self._pr_monitor_task = None
            if self.task_scheduler:
                try:
                    await self.task_scheduler.stop()
                except Exception as exc:
                    logger.debug("Task scheduler stop error (harmless): %s", exc)
            if self.mcp_manager:
                await self.mcp_manager.stop_all()
                logger.info("MCP manager stopped")
            try:
                from backend.lsp.client import get_lsp_manager

                await get_lsp_manager().shutdown_all()
                logger.info("LSP manager stopped")
            except Exception as exc:
                logger.debug("LSP manager stop error (harmless): %s", exc)
            try:
                from backend.preview.launcher import stop_all_preview_launches

                await stop_all_preview_launches()
                logger.info("Preview processes stopped")
            except Exception as exc:
                logger.debug("Preview shutdown error (harmless): %s", exc)
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
                return self._build_tool_registry(artifact_store, **kwargs)
        except (TypeError, ValueError):
            pass
        return self._build_tool_registry(artifact_store)

    def create_permission_checker(self) -> PermissionChecker:
        config = self.config or self.refresh_config()
        return PermissionChecker(config.permissions)

    def create_llm(self, *, model_override: str | None = None) -> Any:
        config = self.config or self.refresh_config()
        return self._create_llm_adapter(config, model_override=model_override)

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

    def _resolve_mcp_request_session(
        self,
        params: dict[str, Any],
    ) -> tuple[Any | None, dict[str, Any]]:
        owner = params.get("_minicode_owner")
        if not isinstance(owner, dict) or self.ws_manager is None:
            return None, {}
        session_id = str(owner.get("session_id") or "").strip()
        conversation_id = str(owner.get("conversation_id") or "").strip()
        if not session_id or not conversation_id:
            return None, owner
        for session in self.ws_manager._sessions.values():
            if (
                str(getattr(session, "session_id", "") or "") == session_id
                and bool(getattr(session, "_is_connected", False))
            ):
                return session, owner
        return None, owner

    async def _await_mcp_owner_operation(
        self,
        operation: Awaitable[Any],
        owner: dict[str, Any],
        *,
        label: str,
        maximum_seconds: float,
    ) -> Any:
        """Await one server-initiated operation inside its owner turn fence."""

        operation_task = asyncio.ensure_future(operation)
        cancel_event = owner.get("cancel_event")
        cancel_wait: asyncio.Task[bool] | None = None
        if isinstance(cancel_event, asyncio.Event):
            cancel_wait = asyncio.create_task(cancel_event.wait())

        timeout = max(0.05, float(maximum_seconds))
        deadline = owner.get("deadline_monotonic")
        if isinstance(deadline, (int, float)):
            timeout = min(timeout, max(0.0, float(deadline) - time.monotonic()))

        try:
            waiters: set[asyncio.Future[Any]] = {operation_task}
            if cancel_wait is not None:
                waiters.add(cancel_wait)
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return operation_task.result()
            if cancel_wait is not None and cancel_wait in done:
                raise PermissionError(f"MCP {label} cancelled with its owning turn")
            raise TimeoutError(f"MCP {label} exceeded its owner deadline")
        finally:
            if not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
            if cancel_wait is not None:
                await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _approve_mcp_sampling(
        self,
        session: Any,
        owner: dict[str, Any],
        *,
        server_name: str,
        request_id: str,
        max_tokens: int,
        message_count: int,
        image_count: int,
        has_system_prompt: bool,
        prompt_preview: str,
        preview_truncated: bool,
    ) -> bool:
        import uuid

        approval_id = f"mcp_sampling_{uuid.uuid4().hex}"
        conversation_id = str(owner.get("conversation_id") or "").strip()
        args = {
            "server": server_name,
            "max_tokens": max_tokens,
            "message_count": message_count,
            "image_count": image_count,
            "has_system_prompt": has_system_prompt,
            # This is server-supplied content, never trusted host instruction.
            # The desktop must show it before the user authorizes a paid model
            # call on the server's behalf.
            "prompt_preview": prompt_preview,
            "prompt_preview_truncated": preview_truncated,
            "request_id": request_id,
        }
        if session._use_control_protocol:
            payload = {
                "type": "control_request",
                "request_id": approval_id,
                "conversation_id": conversation_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": f"mcp_sampling:{server_name}",
                    "input": args,
                    "tool_use_id": approval_id,
                    "source_tool": f"mcp:{server_name}",
                },
            }
        else:
            payload = {
                "type": "approval_request",
                "request_id": approval_id,
                "conversation_id": conversation_id,
                "tool_name": f"mcp_sampling:{server_name}",
                "args": args,
                "description": f"MCP server '{server_name}' requests a host model call.",
            }
        session._pending_approval_payloads[approval_id] = payload
        await session._send_ws_payload(payload, log_context="mcp:sampling-approval")
        result = await self._await_mcp_owner_operation(
            session._approval_handler(approval_id),
            owner,
            label="sampling approval",
            maximum_seconds=300.0,
        )
        return isinstance(result, dict) and result.get("action") == "approve"

    async def _handle_mcp_sampling(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle sampling/createMessage request from MCP server."""
        import base64
        import binascii

        from backend.llm.base import LLMAdapter, LLMMessage, UsageInfo

        session, owner = self._resolve_mcp_request_session(params)
        if session is None:
            raise PermissionError("MCP sampling request is not bound to an active session and conversation")
        server_name = str(params.get("_mcp_server_name") or "unknown").strip()
        request_id = str(params.get("_mcp_request_id") or "").strip()
        mcp_messages = params.get("messages", [])
        if not isinstance(mcp_messages, list) or not mcp_messages:
            raise ValueError("MCP sampling request contains no messages")
        if len(mcp_messages) > _MCP_SAMPLING_MAX_MESSAGES:
            raise ValueError(
                f"MCP sampling request exceeds {_MCP_SAMPLING_MAX_MESSAGES} messages"
            )
        try:
            requested_max_tokens = int(params.get("maxTokens") or 0)
        except (TypeError, ValueError):
            requested_max_tokens = 0
        if requested_max_tokens <= 0:
            raise ValueError("MCP sampling request must include a positive maxTokens")
        max_tokens = min(requested_max_tokens, 2048)
        system_prompt = str(params.get("systemPrompt") or "").strip()

        # MCP sampling's separate systemPrompt is not a host system message.
        # The protocol message roles are user/assistant only; accepting a
        # server-forged system role would let an extension change the priority
        # of an otherwise reviewed, untrusted request.
        llm_messages: list[LLMMessage] = []
        preview_lines: list[str] = []
        total_text_chars = len(system_prompt)
        image_count = 0
        if system_prompt:
            preview_lines.append(f"systemPrompt (untrusted): {system_prompt}")
        for index, msg in enumerate(mcp_messages, 1):
            if not isinstance(msg, dict):
                raise ValueError(f"MCP sampling message {index} must be an object")
            role = str(msg.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                raise ValueError(
                    f"MCP sampling message {index} has unsupported role {role!r}"
                )
            content_field = msg.get("content")
            content_text = ""
            images: list[dict[str, str]] = []
            blocks: list[Any]
            if isinstance(content_field, list):
                blocks = content_field
            elif isinstance(content_field, dict):
                blocks = [content_field]
            elif isinstance(content_field, str):
                content_text = content_field
                blocks = []
            else:
                raise ValueError(
                    f"MCP sampling message {index} content must be text or content blocks"
                )
            for block in blocks:
                if not isinstance(block, dict):
                    raise ValueError(f"MCP sampling message {index} has an invalid content block")
                block_type = str(block.get("type") or "").strip().lower()
                if block_type == "text":
                    content_text += str(block.get("text") or "")
                elif block_type == "image":
                    data = str(block.get("data") or "")
                    if not data:
                        raise ValueError(f"MCP sampling message {index} image has no data")
                    media_type = str(block.get("mimeType") or "").strip().lower()
                    if not media_type.startswith("image/"):
                        raise ValueError(
                            f"MCP sampling message {index} has invalid image MIME type"
                        )
                    try:
                        decoded_size = len(base64.b64decode(data, validate=True))
                    except (binascii.Error, ValueError) as exc:
                        raise ValueError(
                            f"MCP sampling message {index} image is not valid base64"
                        ) from exc
                    if decoded_size > _MCP_SAMPLING_MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"MCP sampling image exceeds {_MCP_SAMPLING_MAX_IMAGE_BYTES} bytes"
                        )
                    images.append({
                        "media_type": media_type,
                        "data": data,
                    })
                    image_count += 1
                else:
                    raise ValueError(
                        f"MCP sampling message {index} uses unsupported content type {block_type!r}"
                    )
            total_text_chars += len(content_text)
            if total_text_chars > _MCP_SAMPLING_MAX_TEXT_CHARS:
                raise ValueError(
                    f"MCP sampling text exceeds {_MCP_SAMPLING_MAX_TEXT_CHARS} characters"
                )
            preview_lines.append(
                f"{role}: {content_text}" + (f" [images: {len(images)}]" if images else "")
            )
            llm_messages.append(LLMMessage(role=role, content=content_text, images=images))

        # Text is already bounded above. Send all of it to the approval UI so
        # approval never authorizes prompt text that the user could not review.
        preview_text = "\n\n".join(preview_lines).strip()
        approved = await self._approve_mcp_sampling(
            session,
            owner,
            server_name=server_name,
            request_id=request_id,
            max_tokens=max_tokens,
            message_count=len(mcp_messages),
            image_count=image_count,
            has_system_prompt=bool(system_prompt),
            prompt_preview=preview_text,
            preview_truncated=False,
        )
        if not approved:
            raise PermissionError("MCP sampling request was rejected by the user")
        logger.info(
            "Approved MCP sampling request server=%s session=%s conversation=%s max_tokens=%s",
            server_name,
            owner.get("session_id"),
            owner.get("conversation_id"),
            max_tokens,
        )

        if system_prompt:
            llm_messages.insert(
                0,
                LLMMessage(
                    role="user",
                    content=(
                        f"[Untrusted instruction supplied by MCP server {server_name}]\n"
                        f"{system_prompt}"
                    ),
                ),
            )

        adapter = session.llm
        # ``simple_chat`` normally reports usage into the currently bound turn
        # bucket. Server callbacks run on the MCP reader task, so bind a local
        # bucket explicitly and commit its *actual* usage to the shared tree.
        # Reserving maxTokens here made the budget pessimistic and still missed
        # prompt tokens/cost; Codex-style accounting records provider usage.
        sampling_usage = UsageInfo()
        usage_token = LLMAdapter.bind_turn_usage(sampling_usage)
        try:
            reply_text = await self._await_mcp_owner_operation(
                adapter.simple_chat(llm_messages, max_tokens=max_tokens),
                owner,
                label="sampling model call",
                maximum_seconds=60.0,
            )
        finally:
            LLMAdapter.unbind_turn_usage(usage_token)

        rollout_budget = owner.get("rollout_budget")
        if rollout_budget is not None:
            rollout_budget.record_usage_total(
                f"mcp-sampling:{server_name}:{request_id}",
                sampling_usage,
            )

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

        session, owner = self._resolve_mcp_request_session(params)
        if not session:
            return {"action": "cancel", "error": "MCP elicitation has no active owning session"}

        request_id = f"elicit_{uuid.uuid4().hex}"
        prompt = str(params.get("prompt") or "").strip()
        schema = params.get("schema") or {}
        conversation_id = str(owner.get("conversation_id") or "").strip()

        # Build the payload
        if session._use_control_protocol:
            payload = {
                "type": "control_request",
                "request_id": request_id,
                "conversation_id": conversation_id,
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
                "conversation_id": conversation_id,
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
            result = await self._await_mcp_owner_operation(
                future,
                owner,
                label="elicitation",
                maximum_seconds=300.0,
            )

            # The client responds via "answer" or "approval" command, which resolves
            # the future with the payload. Let's inspect result structure:
            answer = result.get("answer") or result.get("content") or ""
            return {
                "action": "submit",
                "response": {
                    "answer": answer
                }
            }
        except TimeoutError:
            return {"action": "cancel", "error": "User response timed out"}
        except PermissionError as exc:
            return {"action": "cancel", "error": str(exc)}
        except asyncio.CancelledError:
            return {"action": "cancel", "error": "Interaction cancelled"}
        finally:
            session._pending_approvals.pop(request_id, None)
            session._pending_approval_payloads.pop(request_id, None)
