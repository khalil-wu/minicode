from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import mcp_registry_version
from backend.agent.run_context import RunContext
from backend.agent.query_engine import QueryEngine
from backend.agent.message import AgentEvent, UserCommand
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain_receipt,
    _consume_task_result,
)
from backend.attachments.store import AttachmentStore
from backend.artifact.store import ArtifactStore
from backend.checkpoint import CheckpointManager
from backend.commands.registry import CommandRegistry
from backend.config import (
    AppConfig,
    get_available_models,
    get_llm_provider,
    get_models_source,
    load_config,
)
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE
from backend.conversations.public_projection import (
    project_public_conversation,
    project_public_conversation_summary,
)
from backend.conversations.repository import CONVERSATION_DATA_DIR, ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.profiles import (
    permission_profile_for_mode,
    sandbox_status_for,
    workspace_scope_for,
)
from backend.tasks.manager import TaskManager
from backend.terminal.session import TerminalSessionManager
from backend.terminal.manager import BackgroundCommandManager, BackgroundCommand
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import (
    SessionAgentRunnerMixin,
    _TURN_MESSAGE_SCOPED_EVENT_TYPES,
    _llm_adapter_cache_key,
)
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.command_handlers import SessionCommandHandlersMixin
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.event_outbox import EventOutbox
from backend.ws.fork_registry import ForkRegistry
from backend.ws.manager import _SESSION_MCP_MANAGER_UNSET
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import apply_stream_event
from backend.ws.turn_wait_state import TurnWaitState
from backend.ws.ui_agent_state_store import UiAgentStateStore
from backend.ws.utils import (
    build_effective_user_message,
    build_summary_from_transcript,
)

logger = logging.getLogger(__name__)
WS_EVENT_REPLAY_MAX = 1000

_NOTIFICATION_HOOK_EVENT_TYPES = frozenset(
    {
        "system_notice",
        "error",
        "approval_request",
        "approval.file_diff",
        "ask_user",
        "approval.cancelled",
        "idle_prompt",
        "auth_success",
        "elicitation_dialog",
        "elicitation_complete",
        "elicitation_response",
    }
)

CONVERSATION_SCOPED_EVENT_TYPES = {
    "artifact_content",
    "item.started",
    "agent_message.delta",
    "item.completed",
    "agent.run.started",
    "agent.run.completed",
    "agent.item",
    "user_message.queue.updated",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "tool_result",
    "agent.progress",
    "runtime.span",
    "task.update",
    "approval_request",
    "permission.decision",
    "approval.cancelled",
    "approval.file_diff",
    "ask_user",
    "context_usage",
    "context_compacted",
    "context_forked",
    "context_ledger",
    "context_side_query_result",
    "budget_update",
    "budget.warning",
    "command_output_chunk",
    "artifact.preview",
    "done",
    "stream_resume",
    "stream_event",
    "rate_limit",
    "session.state_changed",
    "conversation.hydration.updated",
    "conversation.compaction.updated",
    "conversation.summary.updated",
    "goal.updated",
    "turn.plan.updated",
    "turn.diff.updated",
    "subagent.start",
    "subagent.event",
    "subagent.progress",
    "subagent.done",
    "citation.add",
    "inspector.update",
    "control_request",
    "checkpoint.created",
    "checkpoint.list",
    "checkpoint.rewound",
    "checkpoint.run.list",
    "checkpoint.run.resume",
    "file.changed",
    "git.pr_status",
    "terminal.output",
    "terminal.exit",
    "terminal.created",
    "terminal.killed",
    "terminal.list",
    "terminal.snapshot",
    "terminal.resized",
    "background.started",
    "background.stalled",
    "background.completed",
    "workspace.imported",
    "system_notice",
    "parent.notifications",
    "preview.servers.updated",
    "preview.server.detected",
    "preview.server.stopped",
    "preview.navigated",
    "preview.refreshed",
    "preview.launch.config",
    "preview.launch.started",
    "preview.launch.stopped",
    "preview.server.ready",
    "preview.server.output",
    "preview.server.crashed",
    "preview.server.unhealthy",
    "preview.verified",
}


def _requires_conversation_owner(event_type: str, payload: dict[str, Any]) -> bool:
    """Return whether this concrete wire shape must carry a conversation owner.

    ``task.update`` has two intentional variants: turn-owned todo mutations and
    a session-wide runtime snapshot emitted by ``TaskManager``.  Treating the
    event name alone as conversation-scoped silently dropped the latter during
    restore and task lifecycle changes.  Keep strict owner enforcement for the
    todo form while allowing only the explicit ``session`` snapshot form to be
    global.
    """

    if event_type not in CONVERSATION_SCOPED_EVENT_TYPES:
        return False
    if event_type == "task.update" and isinstance(payload.get("session"), dict):
        return False
    return True

WORKSPACE_SCOPED_EVENT_TYPES = {
    "checkpoint.created",
    "checkpoint.list",
    "checkpoint.rewound",
    "checkpoint.run.list",
    "checkpoint.run.resume",
    "file.changed",
    "git.pr_status",
    "diff.git_working_tree",
    "diff.git_staged",
    "diff.git_stage_file",
    "diff.git_unstage_file",
    "diff.git_stage_all",
    "diff.git_unstage_all",
    "diff.git_revert_file",
    "workspace.imported",
    "preview.servers.updated",
    "preview.server.detected",
    "preview.server.stopped",
    "preview.navigated",
    "preview.refreshed",
    "preview.launch.config",
    "preview.launch.started",
    "preview.launch.stopped",
    "preview.server.ready",
    "preview.server.output",
    "preview.server.crashed",
    "preview.server.unhealthy",
    "preview.verified",
}

# Backward-compatible re-export for existing tests and callers that still
# import the helper from backend.ws.handler after the ws.utils extraction.
_build_effective_user_message = build_effective_user_message


class WebSocketSession(
    SessionCommandHandlersMixin,
    SessionPermissionRuntimeMixin,
    SessionApprovalRuntimeMixin,
    SessionAgentRunnerMixin,
):
    """A websocket-bound runtime session with a persistent conversation store."""

    def __init__(
        self,
        session_id: str,
        websocket: WebSocket,
        llm: LLMAdapter,
        artifact_store: ArtifactStore,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        config: AppConfig,
        skill_manager: Any | None = None,
        skill_executor: Any | None = None,
        memory_manager: Any | None = None,
        mcp_manager: Any = _SESSION_MCP_MANAGER_UNSET,
        ws_manager: Any | None = None,
    ) -> None:
        self.session_id = session_id
        self.ws_manager = ws_manager
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self.event_outbox = EventOutbox(
            session_id=session_id,
            websocket=websocket,
            replay_root=Path(CONVERSATION_DATA_DIR).parent / "ws-event-log",
            replay_limit=WS_EVENT_REPLAY_MAX,
            cleanup_tasks=self._cleanup_tasks,
            has_active_run=self._has_active_run,
            requires_conversation_owner=_requires_conversation_owner,
            workspace_scoped_event_types=WORKSPACE_SCOPED_EVENT_TYPES,
        )
        self.session_lifecycle = SessionLifecycle(self)
        self.command_dispatcher = SessionCommandDispatcher(
            self,
            root_dir=Path(CONVERSATION_DATA_DIR).parent / "client-command-log",
        )
        self.llm = llm
        self.artifact_store = artifact_store
        self.tool_registry = tool_registry
        if mcp_manager is _SESSION_MCP_MANAGER_UNSET:
            from backend.api.routes_health import get_mcp_manager

            mcp_manager = get_mcp_manager()
        self.mcp_manager = mcp_manager
        # Snapshot of the MCP registry generation this tool_registry reflects.
        # Used to hot-reload MCP tools into a live session (see
        # refresh_tool_registry_if_mcp_changed) without restarting the backend.
        self._mcp_registry_version_snapshot = mcp_registry_version(mcp_manager)
        self._mcp_manager_snapshot_id = id(mcp_manager)
        self.permission_checker = permission_checker
        self.config = config
        self.skill_executor = skill_executor
        self.memory_manager = memory_manager
        self.turn_wait_state = TurnWaitState()
        self.ui_agent_state_store = UiAgentStateStore()
        self.approval_diff_cache: dict[str, dict[str, Any]] = {}
        self.last_agent_state: Any | None = None
        # Pi's ExtensionRunner/AgentSessionRuntime and Codex's thread runtime
        # are session-owner scoped.  A websocket can host several MiniCode
        # conversations, so each conversation owns its own registry and
        # extension generation instead of sharing the last-bound closures.
        self._conversation_tool_registries: dict[str, tuple[int, str, Any]] = {}
        self._extension_runtime_states: dict[str, dict[str, Any]] = {}
        self._extension_shutdown_requested = False
        self._extension_requested_shutdown_task: asyncio.Task[Any] | None = None
        self.last_pr_auto_fix_signature = ""
        self._interrupted = False
        self._interrupted_conversation_ids = self.turn_wait_state.interrupted_conversation_ids
        # Conversation lifecycle commands are received concurrently so slow
        # workspace activation cannot let a later delete/create/user message
        # overtake an earlier switch. Keep their observable order per session.
        self._conversation_lifecycle_lock = asyncio.Lock()
        self._conversation_projection_locks: dict[str, asyncio.Lock] = {}
        # HTTP uploads run outside the WebSocket command queue. Reserve their
        # conversation owner synchronously under one process lock so concurrent
        # files in the same paste/drop batch cannot each create a different
        # conversation before their bodies are read.
        self._attachment_upload_lock = threading.RLock()
        # Notification hooks observe already-committed wire events. They are
        # owned by the session but must never delay live projection.
        self._notification_hook_tasks: set[asyncio.Task[Any]] = set()
        # Streaming reconnection support
        self._conversation_streams: dict[str, dict[str, Any]] = {}
        self._resolve_llm_provider = get_llm_provider
        self._resolve_available_models = get_available_models
        self._resolve_models_source = get_models_source
        self._llm_adapter_cache: dict[tuple[Any, ...], LLMAdapter] = {}
        self._llm_close_tasks: set[asyncio.Task[Any]] = set()
        self._llm_adapter_leases: dict[int, set[asyncio.Task[Any]]] = {}
        self._retired_llm_adapters: dict[int, LLMAdapter] = {}
        self.provider = self._resolve_llm_provider()
        self.available_models = list(self._resolve_available_models(self.provider))
        self.models_source = self._resolve_models_source(self.provider)
        self.selected_model = getattr(config.llm, "model", "").strip()
        self._last_llm_state_payload: dict[str, Any] | None = None
        self._last_model_catalog_error = ""
        # Preserve an explicit configured model even when discovery no longer
        # advertises it. The run admission boundary must report that mismatch;
        # selecting the first catalog entry would change user intent silently.
        self._llm_adapter_cache[
            _llm_adapter_cache_key(
                config=config,
                provider=self.provider,
                model=self.selected_model,
            )
        ] = llm
        self._model_override_active = False
        self._provider_override_active = False

        if skill_manager is not None:
            from backend.skills.loader import SkillLoader
            from backend.skills.manager import SkillManager

            self.skill_manager = SkillManager(
                loader=SkillLoader(project_root=None),
            )
            self.skill_manager.discover()
        else:
            self.skill_manager = None

        self.context_builder = ContextBuilder(
            token_budget=config.token_budget,
            agent_settings=config.agent,
            skill_executor=skill_executor,
            memory_manager=memory_manager,
            llm=llm,
            skill_manager=self.skill_manager,
        )
        self.attachment_store = AttachmentStore()
        self.checkpoint_manager = CheckpointManager()
        self.conversation_repo = ConversationRepository(CONVERSATION_DATA_DIR)
        self.run_manager = SessionRunManager(self)
        # The WebSocket host owns transport/session concerns only. QueryEngine
        # owns the loop implementation and the complete setup/run/finalize
        # lifecycle, so no host-level loop injection can bypass that boundary.
        self.query_engine = QueryEngine()
        from backend.agent.diagnostic_store import DiagnosticPayloadStore

        self.diagnostic_store = DiagnosticPayloadStore()
        self.task_manager = TaskManager(
            on_change=self.session_lifecycle.schedule_task_runtime_update
        )
        self.permission_context = self.permission_checker.build_context(
            mode=DEFAULT_CONVERSATION_PERMISSION_MODE,
            workspace_scope="computer",
            source="websocket",
        )
        self.command_registry = CommandRegistry()
        self.conversation_runtime = ConversationRuntime(
            conversation_repo=self.conversation_repo,
            context_builder=self.context_builder,
            build_summary_from_transcript=build_summary_from_transcript,
            projection_lock_for=self._conversation_projection_lock,
        )
        self._register_command_handlers()
        # Wait for the client to restore or select the preferred conversation.
        # self._create_fresh_active_conversation()

        self.terminal_manager = TerminalSessionManager()
        self.active_terminal_session_id: str | None = None
        self.background_manager = BackgroundCommandManager(
            on_completed=self.session_lifecycle.on_background_command_completed,
            on_started=self.session_lifecycle.on_background_command_started,
            session_id=session_id,
        )
        self.background_manager._on_stalled = self.session_lifecycle.on_background_command_stalled
        self.fork_registry = ForkRegistry(
            session_id=session_id,
            root_dir=Path(CONVERSATION_DATA_DIR).parent / "session-forks",
        )
        self.session_lifecycle.start_file_watcher()

    @property
    def cleanup_tasks(self) -> set[asyncio.Task[Any]]:
        return self._cleanup_tasks

    def conversation_lifecycle_lock(self) -> asyncio.Lock:
        return self._conversation_lifecycle_lock

    @property
    def connection_generation(self) -> int:
        return self.event_outbox.connection_generation

    @property
    def ws(self) -> WebSocket:
        return self.event_outbox.websocket

    @ws.setter
    def ws(self, websocket: WebSocket) -> None:
        self.event_outbox.websocket = websocket

    @property
    def is_connected(self) -> bool:
        return self.event_outbox.connected

    def mark_disconnected(self) -> None:
        self.event_outbox.mark_disconnected()

    def attach_websocket(self, websocket: WebSocket) -> tuple[WebSocket, int]:
        previous, generation = self.event_outbox.attach_websocket(websocket)
        self.run_manager.recheck_watched_parent_notifications()
        return previous, generation

    @property
    def active_conversation_id(self) -> str | None:
        return self.conversation_runtime.active_conversation_id

    @active_conversation_id.setter
    def active_conversation_id(self, value: str | None) -> None:
        self.conversation_runtime.active_conversation_id = value
        if value:
            self.run_manager.watch_conversation_notifications(value)

    def _create_fresh_active_conversation(self) -> None:
        self.conversation_runtime.create_fresh_active_conversation()
        if self.active_conversation_id:
            self.run_manager.watch_conversation_notifications(
                self.active_conversation_id
            )
        self.sync_permission_mode_with_active_conversation(source="conversation.create")

    def _ensure_active_conversation(
        self, preferred_id: str | None = None
    ) -> None:
        self.conversation_runtime.ensure_active_conversation(preferred_id)
        if self.active_conversation_id:
            self.run_manager.watch_conversation_notifications(
                self.active_conversation_id
            )
        self.sync_permission_mode_with_active_conversation(source="conversation.ensure")

    @property
    def active_conversation(self):
        return self.conversation_runtime.active_conversation

    def _pending_approval_runtime_items(self) -> list[dict[str, Any]]:
        wait_state = self.turn_wait_state
        pending_payloads = wait_state.pending_approval_payloads
        request_ids = list(dict.fromkeys([
            *wait_state.pending_approvals.keys(),
            *wait_state.pending_user_input.keys(),
            *wait_state.pending_elicitations.keys(),
            *wait_state.provider_oauth_pending.keys(),
            *pending_payloads.keys(),
            *wait_state.pending_approval_responses.keys(),
        ]))
        items: list[dict[str, Any]] = []
        for request_id in request_ids:
            payload = dict(pending_payloads.get(request_id) or {})
            # Every prompt reaches the client as one ``control_request``. A
            # request id without a payload is a waiter registered before its
            # request was built, so it is reported as pending without a subtype.
            item: dict[str, Any] = {
                "request_id": request_id,
                "type": str(payload.get("type") or "approval_pending"),
            }
            conversation_id = str(payload.get("conversation_id") or "").strip()
            if conversation_id:
                item["conversation_id"] = conversation_id
            request = payload.get("request")
            if isinstance(request, dict):
                item["subtype"] = str(request.get("subtype") or "").strip()
                tool_name = str(request.get("tool_name") or "").strip()
                if tool_name:
                    item["tool_name"] = tool_name
            items.append(item)
        return items

    def runtime_snapshot(self) -> dict[str, Any]:
        task_summary = self.task_manager.summary()
        running_tasks = [
            task.to_dict()
            for task in self.task_manager.list()
            if task.status in {"pending", "running"}
        ]
        running_tasks.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        # Explicit Skills belong to an AgentState turn, never to the session.
        invoked_skill_names: list[str] = []
        pending_approvals = self._pending_approval_runtime_items()
        fork_records = self.fork_registry.list(
            parent_conversation_id=str(self.active_conversation_id or ""),
        )
        queued_user_messages = self.run_manager.queued_user_message_snapshot()
        pending_turn_inputs = self.run_manager.pending_turn_input_snapshot()
        active_stream_conversation_ids = sorted(
            str(conversation_id)
            for conversation_id, task in self.run_manager.run_tasks.items()
            if conversation_id and task is not None and not task.done()
        )
        active = self.active_conversation
        if active is not None and (
            getattr(active, "archived", False)
            or getattr(active, "conversation_type", "main") != "main"
        ):
            active = None
            self.active_conversation_id = None
        workspace_root = self.session_lifecycle.workspace_root_for_conversation()
        workspace_scope = workspace_scope_for(
            workspace_root=getattr(active, "workspace_root", "") if active is not None else "",
            worktree_path=getattr(active, "worktree_path", "") if active is not None else "",
        )
        permission_payload = self._runtime_permission_payload(workspace_scope=workspace_scope)
        if (
            active is not None
            and workspace_scope == "computer"
            and self.permission_context.mode == DEFAULT_CONVERSATION_PERMISSION_MODE
        ):
            permission_payload = {
                **permission_payload,
                "profile": "auto",
                "sandbox_status": sandbox_status_for("auto"),
            }
        return {
            "session_id": self.session_id,
            "parent_session_id": None,
            "active_conversation_id": self.active_conversation_id,
            "workspace_root": str(workspace_root) if workspace_root is not None else None,
            "active_conversation": project_public_conversation_summary(active)
            if active is not None
            else None,
            "active_task_id": self.run_manager.active_task_id,
            "active_stream_conversation_ids": active_stream_conversation_ids,
            "selected_model": self.selected_model or None,
            "invoked_skill_names": invoked_skill_names,
            "permission_mode": self.permission_context.mode,
            "permission_profile": permission_payload["profile"],
            "permission_source": self.permission_context.source,
            "approval_policy": self.permission_context.approval_policy,
            "sandbox_mode": self.permission_context.sandbox_mode,
            "managed_requirements_source": self.permission_context.requirements_source or None,
            "workspace_scope": workspace_scope,
            "sandbox_status": permission_payload["sandbox_status"],
            "mcp": self._mcp_summary(),
            "capabilities": self.runtime_capability_summary(permission_payload=permission_payload),
            "task_summary": task_summary,
            "running_tasks": running_tasks[:5],
            "pending_approval_count": len(pending_approvals),
            "pending_approvals": pending_approvals[:5],
            "forks": [record.to_dict() for record in fork_records[-20:]],
            "queued_user_messages": queued_user_messages,
            "pending_turn_inputs": pending_turn_inputs,
            "websocket_replay": self.event_outbox.runtime_snapshot(),
        }

    def _runtime_permission_payload(
        self,
        *,
        workspace_scope: str | None = None,
    ) -> dict[str, Any]:
        profile = permission_profile_for_mode(self.permission_context.mode)
        sandbox_status = self.session_lifecycle.sandbox_capability_payload or {
            "policy_configured": True,
            "probe_status": "pending",
            "enforcement": "unknown",
            "backend_available": None,
            "backend": "unknown",
            "filesystem_isolated": None,
            "network_isolated": None,
            "deny_read_isolated": None,
            "protected_paths_isolated": None,
            "fail_closed": True,
            "unavailable_action": "await_probe",
            "reason": "Sandbox capability probe has not completed",
        }
        return {
            "mode": self.permission_context.mode,
            "profile": profile,
            "source": self.permission_context.source,
            "approval_policy": self.permission_context.approval_policy,
            "sandbox_mode": self.permission_context.sandbox_mode,
            "managed_requirements_source": self.permission_context.requirements_source or None,
            "workspace_scope": workspace_scope or getattr(self.permission_context, "workspace_scope", "computer"),
            "sandbox_status": sandbox_status,
        }

    def runtime_capability_summary(
        self,
        *,
        permission_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compact per-session capability contract for runtime snapshots."""
        self.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
        permission = permission_payload or self._runtime_permission_payload()
        registry = self.tool_registry
        summary_error: dict[str, str] | None = None
        try:
            registry_for_conversation = getattr(
                self,
                "_conversation_tool_registry",
                None,
            )
            if callable(registry_for_conversation) and self.active_conversation_id:
                registry = registry_for_conversation(self.active_conversation_id)
            summary = registry.build_capability_summary(
                permission_checker=self.permission_checker,
                permission_context=self.permission_context,
            )
        except Exception as exc:
            logger.debug("session %s capability summary failed: %s", self.session_id, exc)
            summary_error = {
                "type": "capability_summary_failed",
                "detail": type(exc).__name__,
            }
            summary = {
                "tools_total": 0,
                "direct_tools": 0,
                "core_tools": 0,
                "deferred_tools": 0,
                "hidden_tools": 0,
                "mcp_proxy_tools": 0,
                "commands": 0,
                "skills": 0,
                "mcp_resource_bridge": False,
                "deferred_bridge": False,
                "skill_catalog": False,
                "status": "error",
                "error": summary_error,
            }
        return {
            "version": getattr(registry, "version", 0),
            "summary": summary,
            "permission": permission,
            "mcp_registry_version": self._mcp_registry_version_snapshot,
            "provider_capabilities": self._provider_capabilities_payload(),
            **({"status": "error", "error": summary_error} if summary_error else {}),
        }

    def _provider_capabilities_payload(self) -> dict[str, Any]:
        try:
            from backend.llm.capabilities import capabilities_for_adapter

            return capabilities_for_adapter(self.llm).to_dict()
        except Exception as exc:
            logger.debug("session %s provider capability snapshot failed: %s", self.session_id, exc)
            return {}

    def runtime_capability_snapshot(self) -> dict[str, Any]:
        """Full per-session capability contract, including current permissions."""
        from backend.commands.catalog import get_enabled_composer_command_catalog
        from backend.feature_flags import feature_flags_payload

        self.refresh_tool_registry_if_mcp_changed()
        registry = self.tool_registry
        try:
            registry_for_conversation = getattr(
                self,
                "_conversation_tool_registry",
                None,
            )
            if callable(registry_for_conversation) and self.active_conversation_id:
                registry = registry_for_conversation(self.active_conversation_id)
            snapshot = registry.build_snapshot(
                permission_checker=self.permission_checker,
                permission_context=self.permission_context,
                mcp_registry_version=self._mcp_registry_version_snapshot,
            )
        except Exception as exc:
            logger.warning(
                "session %s capability snapshot failed: %s",
                self.session_id,
                exc,
            )
            snapshot = {
                "version": getattr(registry, "version", 0),
                "tools": [],
                "commands": [],
                "skills": [],
                "summary": {
                    "tools_total": 0,
                    "direct_tools": 0,
                    "core_tools": 0,
                    "deferred_tools": 0,
                    "hidden_tools": 0,
                    "mcp_proxy_tools": 0,
                    "commands": 0,
                    "skills": 0,
                    "status": "error",
                    "error": {
                        "type": "capability_snapshot_failed",
                        "detail": type(exc).__name__,
                    },
                },
                "status": "error",
                "error": {
                    "type": "capability_snapshot_failed",
                    "detail": type(exc).__name__,
                },
            }
        extension_commands = self.command_registry.list_extension_slash_commands(
            scope_id=self.active_conversation_id
        )
        active_conversation = (
            self.conversation_repo.get_conversation(self.active_conversation_id)
            if self.active_conversation_id
            else None
        )
        active_workspace_root = (
            self.session_lifecycle.workspace_root_for_conversation(active_conversation)
            if active_conversation is not None
            else None
        )
        snapshot["composer_commands"] = [
            *extension_commands,
            *get_enabled_composer_command_catalog(
                active_workspace_root,
                resolve_active_workspace=False,
            ),
        ]
        snapshot["feature_flags"] = feature_flags_payload()
        if self.skill_manager is not None and not snapshot.get("skills"):
            list_all = getattr(self.skill_manager, "list_all", None)
            skills = list_all() if callable(list_all) else []
            if skills:
                snapshot["skills"] = skills
                snapshot["summary"] = {
                    **dict(snapshot.get("summary") or {}),
                    "skills": len(skills),
                    "skill_catalog": True,
                }
        snapshot["permission"] = self._runtime_permission_payload()
        snapshot["mcp_registry_version"] = self._mcp_registry_version_snapshot
        snapshot["provider_capabilities"] = self._provider_capabilities_payload()
        return snapshot

    def runtime_capabilities_payload(self, *, source: str = "session") -> dict[str, Any]:
        return {
            "type": "runtime.capabilities",
            "session_id": self.session_id,
            "source": source,
            "capabilities": self.runtime_capability_snapshot(),
        }

    def _mcp_summary(self) -> dict[str, Any]:
        """Compact MCP summary for the runtime snapshot: counts + short statuses.

        Kept intentionally small (no tools/errors/full dicts) so the snapshot
        stays light. Reads the in-memory manager status; empty when no bootstrap.
        """
        try:
            from backend.api.routes_health import get_mcp_status

            servers = get_mcp_status() or []
        except Exception:  # pragma: no cover - manager unavailable / not started
            servers = []
        return {
            "connected": sum(1 for s in servers if s.get("status") == "connected"),
            "failed": sum(1 for s in servers if s.get("phase") == "failed"),
            "auth_required": sum(1 for s in servers if s.get("phase") in {"auth_required", "expired"}),
            "servers": [
                {
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "phase": s.get("phase"),
                    "auth_status": s.get("auth_status"),
                }
                for s in servers
            ],
        }

    def _has_active_run(self) -> bool:
        """True when an agent run is currently in flight in this session."""
        return self.run_manager.has_active_run()

    def running_agent_task_for(self, conversation_id: str) -> asyncio.Task[None] | None:
        return self.run_manager.running_task_for(conversation_id)

    def _register_agent_run(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[None],
        task_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        self.run_manager.register(
            conversation_id=conversation_id,
            task=task,
            task_id=task_id,
            cancel_event=cancel_event,
            active_conversation_id=self.active_conversation_id,
        )

    def _cleanup_agent_run(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[None],
        task_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        had_inflight_user_message = self.run_manager.has_inflight_user_message(
            conversation_id
        )
        queue_owned_run = self.run_manager.is_queue_owned_run(task_id)
        cleanup_succeeded = False
        try:
            self.run_manager.cleanup(
                conversation_id=conversation_id,
                task=task,
                task_id=task_id,
                cancel_event=cancel_event,
            )
            cleanup_succeeded = True
        finally:
            if queue_owned_run:
                self.run_manager.finish_queue_owned_run(
                    task_id,
                    succeeded=cleanup_succeeded,
                )
        # A queued dispatch settles its durable claim in the dispatch task's
        # finally block. Claiming the next item before that settle would make
        # the durable queue reject it as a concurrent inflight message. The
        # dispatch owner kicks the queue after settle; ordinary runs still
        # advance the queue directly here.
        if not had_inflight_user_message and not queue_owned_run:
            self.schedule_next_queued_user_message(conversation_id)
        self.run_manager.recheck_parent_notification_wake(conversation_id)

    async def start_agent_run(
        self,
        content: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Schedule one agent run through the session's canonical run manager.

        The WebSocket command path and local host adapters must share exactly
        one scheduling boundary.  Keeping task registration, cancellation
        bookkeeping, and queued-message cleanup here prevents an integration
        transport from accidentally creating a second agent loop or leaving a
        run invisible to ``turn/interrupt``/session shutdown.

        ``metadata`` is owned by the caller and is copied before the runner can
        add lifecycle fields.  Local protocol adapters may provide a stable
        ``run_id`` so their response turn id matches the first streamed event;
        ordinary WebSocket commands do not set it.
        """
        target_conversation_id = str(conversation_id or "").strip()
        if not target_conversation_id:
            raise ValueError("conversation_id is required")

        run_context = RunContext(
            turn_input_queue=self.run_manager.turn_input_queue(target_conversation_id),
        )
        run_cancel_event = asyncio.Event()
        run_metadata = dict(metadata or {})
        admission_restored = bool(run_metadata.get("_turn_admission_restored"))
        admission_required = not bool(run_metadata.get("_parent_notification_only"))
        admission_future: asyncio.Future[None] | None = None
        if admission_required and not admission_restored:
            admission_future = asyncio.get_running_loop().create_future()
            run_metadata["_turn_admission_future"] = admission_future
        with self.event_outbox.bind_connection_generation(None):
            managed_run = self.task_manager.create(
                "agent.run",
                self._run_agent(
                    content,
                    attachments=list(attachments or []),
                    conversation_id=target_conversation_id,
                    metadata=run_metadata,
                    cancel_event=run_cancel_event,
                    run_context=run_context,
                ),
            )
            # Terminal delivery is fenced to this concrete run. Task creation
            # does not yield control, so the runner observes this id before its
            # first coroutine step.
            run_metadata["_run_task_id"] = str(managed_run.id)

        try:
            self._register_agent_run(
                conversation_id=target_conversation_id,
                task=managed_run.task,
                task_id=managed_run.id,
                cancel_event=run_cancel_event,
            )
            if run_metadata.get("_queued_user_message_dispatch"):
                self.run_manager.mark_queue_owned_run(
                    target_conversation_id,
                    str(managed_run.id),
                )
        except RuntimeError as exc:
            # The conversation already owns a live run. The task exists but is
            # unregistered, so nothing else can ever reach it — tear it down
            # here rather than leaking a second loop into this conversation.
            logger.error(
                "Refusing a second agent run for conversation %s: %s",
                target_conversation_id,
                exc,
            )
            run_cancel_event.set()
            await cancel_and_drain_receipt(
                [managed_run.task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"rejected duplicate run for {target_conversation_id}",
                owner=self._cleanup_tasks,
            )
            busy_error = AgentEvent.error(
                str(exc),
                recoverable=True,
                error_type="conversation_busy",
                error_code="agent.busy",
            )
            busy_error.data["conversation_id"] = target_conversation_id
            await self.send_event(busy_error)
            raise

        async def _wait_and_cleanup() -> None:
            # A task that returns without the runner's delivery fence is not a
            # successful run: it means an early-return/setup path escaped the
            # canonical terminal projection.  Treat that integrity gap as a
            # failure unless cancellation or an exception gives us a more
            # specific outcome.
            terminal_status = "failed"
            terminal_reason = "terminal_delivery_missing"

            async def _ensure_durable_terminal() -> Any | None:
                nonlocal terminal_reason
                runtime = run_context.agent_runtime
                run_id = str(run_metadata.get("run_id") or "").strip()
                get_run = getattr(runtime, "get_run", None)
                record = get_run(run_id) if run_id and callable(get_run) else None
                if record is None:
                    return None
                if str(getattr(record, "status", "")) != "running":
                    return record
                commit_terminal = getattr(runtime, "commit_terminal", None)
                if not callable(commit_terminal):
                    terminal_reason = "terminal_commit_unavailable"
                    return None
                try:
                    committed = commit_terminal(
                        run_id,
                        terminal_status,
                        summary=terminal_reason or terminal_status,
                        terminal_reason=terminal_reason or terminal_status,
                        error=terminal_reason if terminal_status == "failed" else "",
                    )
                except Exception as exc:
                    terminal_reason = "terminal_commit_failed"
                    logging.error(
                        "Scheduler could not durably close run %s: %s",
                        run_id,
                        exc,
                        exc_info=True,
                    )
                    commit_error = AgentEvent.error(
                        "MiniCode could not durably commit the run terminal state.",
                        recoverable=False,
                        error_type="terminal_commit_failed",
                        error_code="runtime.scheduler_terminal_commit_failed",
                    )
                    commit_error.data.update({
                        "conversation_id": target_conversation_id,
                        "run_id": run_id,
                        "terminal_commit_failed": True,
                    })
                    await self.send_event(commit_error)
                    return None
                return committed

            try:
                if managed_run.task is not None:
                    await managed_run.task
            except asyncio.CancelledError:
                terminal_status = "cancelled"
                terminal_reason = "cancelled"
            except Exception as exc:
                terminal_status = "failed"
                terminal_reason = "runtime"
                logging.error("Chat run failed: %s", exc, exc_info=True)
                error_event = AgentEvent.error(
                    f"Chat run failed: {exc}",
                    recoverable=False,
                    error_type="runtime",
                )
                error_event.data["conversation_id"] = target_conversation_id
                await self.send_event(error_event)
            finally:
                # Terminal delivery for a durably admitted run belongs to
                # QueryEngine's terminal transaction, and the pre-admission
                # setup zone is closed by the runner's own startup_rejected /
                # startup_failed / startup_cancelled branches.  This net covers
                # only the remaining gap: an admitted run whose delivery marker
                # never landed.  It is guarded by that marker so ordinary runs
                # are never duplicated.
                is_complete = getattr(self.run_manager, "is_delivery_complete", None)
                delivery_complete = bool(
                    callable(is_complete)
                    and is_complete(target_conversation_id, str(managed_run.id))
                )
                if not delivery_complete:
                    durable_record = await _ensure_durable_terminal()
                    if durable_record is not None:
                        durable_status = str(
                            getattr(durable_record, "status", "") or ""
                        ).strip().lower()
                        if durable_status in {
                            "completed",
                            "partial",
                            "failed",
                            "cancelled",
                            "interrupted",
                        }:
                            terminal_status = (
                                "cancelled"
                                if durable_status == "interrupted"
                                else durable_status
                            )
                        durable_reason = str(
                            getattr(durable_record, "terminal_reason", "")
                            or getattr(durable_record, "summary", "")
                            or getattr(durable_record, "error", "")
                            or ""
                        ).strip()
                        if durable_reason:
                            terminal_reason = durable_reason
                        # The runner may durably commit before losing its
                        # delivery marker. Re-project the exact record instead
                        # of fabricating a local failure terminal.
                        await self.send_event(
                            AgentEvent.agent_run_completed(durable_record)
                        )
                    durable_terminal = durable_record is not None
                    done_delivered = False
                    try:
                        if not durable_terminal and not str(run_metadata.get("run_id") or "").strip():
                            # Validation rejected the command before durable
                            # admission. There is no run to complete and no
                            # terminal event should be fabricated for one.
                            self._cleanup_agent_run(
                                conversation_id=target_conversation_id,
                                task=managed_run.task,
                                task_id=managed_run.id,
                                cancel_event=run_cancel_event,
                            )
                            return
                        done_event = AgentEvent.done(
                            status=terminal_status,
                            reason=terminal_reason or terminal_status,
                        )
                        done_event.data["conversation_id"] = target_conversation_id
                        stream_state = getattr(self, "_conversation_streams", {}).get(
                            target_conversation_id
                        )
                        message_id = str(
                            (stream_state or {}).get("message_id")
                            or run_metadata.get("assistant_message_id")
                            or ""
                        ).strip()
                        if message_id:
                            done_event.data["message_id"] = message_id
                        await self.send_event(done_event)
                        done_delivered = True

                        mark_terminal = getattr(self.run_manager, "mark_terminal_status", None)
                        if callable(mark_terminal):
                            mark_terminal(target_conversation_id, terminal_status)
                        mark_delivery = getattr(self.run_manager, "mark_delivery_complete", None)
                        if callable(mark_delivery):
                            mark_delivery(target_conversation_id, str(managed_run.id))

                        await self.send_event(
                            AgentEvent.session_state_changed(
                                state="idle",
                                conversation_id=target_conversation_id,
                                reason=terminal_reason or terminal_status,
                            )
                        )
                    except Exception:
                        logging.exception(
                            "Failed to emit fallback terminal events for conversation %s",
                            target_conversation_id,
                        )
                    if not done_delivered:
                        logging.error(
                            "Fallback terminal delivery remained incomplete for conversation %s",
                            target_conversation_id,
                        )
                self._cleanup_agent_run(
                    conversation_id=target_conversation_id,
                    task=managed_run.task,
                    task_id=managed_run.id,
                    cancel_event=run_cancel_event,
                )

        with self.event_outbox.bind_connection_generation(None):
            cleanup_task = asyncio.create_task(_wait_and_cleanup())
            self.command_dispatcher.track_command_task(cleanup_task)
        if admission_future is not None:
            done, _pending = await asyncio.wait(
                {admission_future, managed_run.task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if admission_future in done:
                await admission_future
            else:
                delivery_complete = self.run_manager.is_delivery_complete(
                    target_conversation_id,
                    str(managed_run.id),
                )
                if not delivery_complete:
                    await managed_run.task
                    raise RuntimeError(
                        "Agent run exited before the user turn was durably admitted"
                    )
        return str(managed_run.id)

    def schedule_next_queued_user_message(self, conversation_id: str) -> None:
        if (
            not conversation_id
            or self.running_agent_task_for(conversation_id)
            or self.run_manager.is_queue_steering(conversation_id)
        ):
            return
        if not self.run_manager.begin_queue_dispatch(conversation_id):
            return
        command = self.run_manager.dequeue_user_message(conversation_id)
        if command is None:
            self.run_manager.finish_queue_dispatch(conversation_id)
            return
        command.data["_queued_user_message_dispatch"] = True

        async def _dispatch() -> None:
            settled = False
            dispatch_succeeded = False

            def settle(*, succeeded: bool) -> None:
                nonlocal settled
                if settled:
                    return
                settled = True
                self.run_manager.finish_user_message_dispatch(
                    conversation_id,
                    command,
                    succeeded=succeeded,
                )

            try:
                await self.send_event(
                    AgentEvent.user_message_queue_updated(
                        status="dequeued",
                        conversation_id=conversation_id,
                        message_id=str(command.data.get("assistant_message_id") or ""),
                        user_message_id=str(command.data.get("user_message_id") or ""),
                        target_message_id=str(command.data.get("assistant_message_id") or ""),
                        turn_mode="follow_up",
                    )
                )
                dispatch_succeeded = await self.command_dispatcher._handle_command(command)
                settle(succeeded=dispatch_succeeded)
            finally:
                if not settled:
                    settle(succeeded=False)
                run_released = await self.run_manager.wait_for_queue_owned_run(
                    conversation_id
                )
                self.run_manager.finish_queue_dispatch(conversation_id)
                if (
                    dispatch_succeeded
                    and run_released
                    and not self.running_agent_task_for(conversation_id)
                ):
                    self.schedule_next_queued_user_message(conversation_id)
                self.run_manager.recheck_parent_notification_wake(
                    conversation_id
                )

        task = asyncio.create_task(_dispatch())
        self.command_dispatcher.track_command_task(task)

    async def cancel_agent_runs(
        self,
        *,
        conversation_id: str | None = None,
        reason: str = "run_cancelled",
    ) -> bool:
        """Cancel one conversation run, or every run owned by this session."""
        return await self.run_manager.cancel(conversation_id=conversation_id, reason=reason)

    def refresh_tool_registry_if_mcp_changed(self, *, allow_when_busy: bool = True) -> bool:
        """Rebuild this session's tool registry when the MCP registry changed.

        A WS session holds a single ``tool_registry``; bumping
        ``mcp_registry_version`` only invalidates the schema cache, so a newly
        connected/removed MCP server stays invisible until the registry itself is
        rebuilt. This compares the live MCP registry version against the session
        snapshot and, on a change, rebuilds via ``bootstrap.create_tool_registry``
        (which re-registers the currently connected MCP proxies) reusing this
        session's existing ``artifact_store``.

        The rebuild always yields a NEW registry object, so an in-flight run that
        already captured the previous registry is never disturbed. MCP status
        hooks pass ``allow_when_busy=False`` to defer to the next idle refresh
        rather than swap the registry while a run is active.

        Returns True iff a rebuild happened.
        """
        workspace_root = self.session_lifecycle.workspace_root_for_conversation()
        manager = self._mcp_manager_for_workspace(workspace_root)
        current_version = mcp_registry_version(manager)
        manager_snapshot_id = id(manager)
        if (
            current_version == self._mcp_registry_version_snapshot
            and manager_snapshot_id == getattr(self, "_mcp_manager_snapshot_id", 0)
        ):
            return False
        if not allow_when_busy and self._has_active_run():
            return False

        from backend.api import _state

        bootstrap = getattr(_state, "bootstrap", None)
        if bootstrap is None:
            # No bootstrap (e.g. before lifespan startup): leave the snapshot
            # stale so a later call retries once bootstrap is available.
            return False
        try:
            self.tool_registry = bootstrap.create_tool_registry(
                self.artifact_store,
                workspace_root=workspace_root,
                config=load_config(cwd=workspace_root),
                mcp_manager=manager,
            )
        except Exception as exc:  # pragma: no cover - never break a run/inspect
            logger.warning("Failed to rebuild tool registry after MCP change: %s", exc)
            return False
        self._mcp_registry_version_snapshot = current_version
        self._mcp_manager_snapshot_id = manager_snapshot_id
        return True

    async def cancel_pending_approvals(
        self,
        *,
        reason: str,
        conversation_id: str | None = None,
    ) -> list[str]:
        return await self._cancel_pending_approvals(
            reason=reason,
            conversation_id=conversation_id,
        )

    def load_active_conversation_snapshot(
        self,
        conversation_id: str,
        snapshot: dict[str, Any] | None,
        *,
        notify: bool = False,
        defer_start: bool = False,
    ) -> bool:
        return self.conversation_runtime.load_active_conversation_snapshot(
            conversation_id,
            snapshot,
            notify=notify,
            defer_start=defer_start,
            on_hydration_complete=self._on_conversation_hydration_complete,
        )

    def start_active_conversation_hydration(self, conversation_id: str) -> bool:
        return self.conversation_runtime.start_hydration(conversation_id)

    async def _on_conversation_hydration_complete(self, conversation_id: str) -> None:
        await self.send_payload(
            {
                "type": "conversation.hydration.updated",
                "conversation_id": conversation_id,
                "is_hydrating": False,
            },
            log_context="conversation.hydration.updated",
        )

    def _prepare_retry_from_message(
        self,
        *,
        conversation: Any,
        retry_from_message_id: str,
    ) -> dict[str, Any] | None:
        return self.conversation_runtime.rewind_to_user_turn(
            conversation=conversation,
            retry_from_message_id=retry_from_message_id,
        )

    async def send_payload(
        self,
        payload: dict[str, Any],
        *,
        connection_generation: int | None = None,
        log_context: str,
        envelope: bool = True,
    ) -> bool:
        return await self.event_outbox.send_payload(
            payload,
            connection_generation=connection_generation,
            log_context=log_context,
            envelope=envelope,
        )

    async def send_conversation_list(self) -> None:
        list_with_revision = getattr(
            self.conversation_repo,
            "list_conversations_with_revision",
            None,
        )
        inventory_instance_id: str | None = None
        inventory_revision: int | None = None
        if callable(list_with_revision):
            versioned_listing = list_with_revision()
            if isinstance(versioned_listing, tuple) and len(versioned_listing) == 3:
                raw_instance_id, raw_revision, summaries = versioned_listing
                if not isinstance(raw_instance_id, str) or not raw_instance_id.strip():
                    raise ValueError(
                        "list_conversations_with_revision returned an invalid inventory instance id"
                    )
                if (
                    isinstance(raw_revision, bool)
                    or not isinstance(raw_revision, int)
                    or raw_revision < 0
                    or raw_revision > 9_007_199_254_740_991
                ):
                    raise ValueError(
                        "list_conversations_with_revision returned an invalid inventory revision"
                    )
                inventory_instance_id = raw_instance_id.strip()
                inventory_revision = raw_revision
            elif isinstance(versioned_listing, tuple) and len(versioned_listing) == 2:
                # Compatibility for older repository doubles. They cannot
                # identify a durable inventory epoch, so keep the projection
                # explicitly legacy instead of inventing an empty instance.
                _legacy_revision, summaries = versioned_listing
            else:
                raise ValueError("list_conversations_with_revision returned an invalid result")
        else:
            summaries = self.conversation_repo.list_conversations()
        conversations = [
            item.to_dict()
            for item in summaries
            if getattr(item, "conversation_type", "main") == "main"
        ]
        snapshot_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        active = self.active_conversation
        if active is not None and (
            getattr(active, "archived", False)
            or getattr(active, "conversation_type", "main") != "main"
        ):
            active = None
            self.active_conversation_id = None
        payload = {
            "type": "conversation.list",
            "conversation_id": self.active_conversation_id,
            "active_conversation_id": self.active_conversation_id,
            "conversations": conversations,
            "active_conversation": project_public_conversation(active) if active is not None else None,
            "session": self.runtime_snapshot(),
            "snapshot_at": snapshot_at,
        }
        if inventory_instance_id is not None and inventory_revision is not None:
            payload["inventory_instance_id"] = inventory_instance_id
            payload["inventory_revision"] = inventory_revision
        await self.send_payload(
            payload,
            log_context="conversation.list",
        )

    async def send_event(self, event: AgentEvent) -> None:
        # Raw provider frames are an in-process SDK/extension surface. The
        # renderer receives typed text, tool, citation, usage, and Inspector
        # projections instead; forwarding ``sdk_only`` frames would put raw
        # reasoning, tool arguments, or provider bodies in browser memory even
        # when the UI deliberately ignores them.
        if event.type == "stream_event" and bool(
            event.data.get("sdk_only", True)
        ):
            return
        # Notices are transcript state, not session-global toasts. Producers in
        # the agent loop already stamp their immutable run owner; command-side
        # notices use the server-owned active conversation at the transport
        # boundary so a replay cannot attach them to whichever conversation the
        # renderer happens to have open later.
        if event.type == "system_notice" and not str(
            event.data.get("conversation_id") or ""
        ).strip():
            active_conversation_id = str(self.active_conversation_id or "").strip()
            if active_conversation_id:
                event.data["conversation_id"] = active_conversation_id
        if event.type == "command.result":
            command_id = self.event_outbox.client_command_id
            details = event.data.get("data")
            result_data = dict(details) if isinstance(details, dict) else {}
            if command_id:
                result_data.setdefault("client_command_id", command_id)
            conversation_id = str(
                event.data.get("conversation_id")
                or result_data.get("conversation_id")
                or self.active_conversation_id
                or ""
            ).strip()
            if conversation_id:
                event.data.setdefault("conversation_id", conversation_id)
                result_data.setdefault("conversation_id", conversation_id)
            if result_data:
                event.data["data"] = result_data
        # Raw provider/tool traces are retained server-side and fetched only
        # when the user opens a concrete Inspector entry. This keeps the live
        # chat stream and client store compact without losing diagnostics.
        if event.type == "inspector.update" and not event.data.get("diagnostics_loaded"):
            target_kind = str(event.data.get("target_kind") or "message")
            target_id = str(event.data.get("target_id") or "").strip()
            diagnostic_payload = event.data.get("payload")
            if target_id and isinstance(diagnostic_payload, dict):
                event.data["payload"] = self.diagnostic_store.put(
                    target_kind,
                    target_id,
                    diagnostic_payload,
                    conversation_id=str(event.data.get("conversation_id") or ""),
                )
        elif event.type == "item.completed":
            # ``tool_calls`` is adapter-local metadata consumed by Pi before
            # the event reaches this transport boundary. Sending it again to
            # the renderer duplicates tool arguments that already have typed
            # tool_call events and creates a second, unused projection path.
            event.data.pop("tool_calls", None)
            provider_raw = event.data.get("provider_raw")
            if isinstance(provider_raw, dict) and provider_raw:
                item = event.data.get("item")
                item_id = str(item.get("id") or "") if isinstance(item, dict) else ""
                trace_id = str(
                    provider_raw.get("trace_id")
                    or f"{event.data.get('message_id') or event.data.get('conversation_id') or item_id or 'provider'}:provider:item"
                )
                event.data["provider_raw"] = self.diagnostic_store.put(
                    "provider",
                    trace_id,
                    provider_raw,
                    conversation_id=str(event.data.get("conversation_id") or ""),
                )
        elif event.type == "done":
            provider_raw = event.data.get("providerRaw")
            if isinstance(provider_raw, dict) and provider_raw:
                trace_id = str(
                    provider_raw.get("trace_id")
                    or f"{event.data.get('message_id') or event.data.get('conversation_id') or 'provider'}:provider:done"
                )
                event.data["providerRaw"] = self.diagnostic_store.put(
                    "provider",
                    trace_id,
                    provider_raw,
                    conversation_id=str(event.data.get("conversation_id") or ""),
                )
        target_conversation_id = str(event.data.get("conversation_id") or "").strip()
        if (
            _requires_conversation_owner(event.type, event.data)
            and not target_conversation_id
        ):
            logger.warning(
                "Dropping conversation-scoped event without conversation_id: type=%s session=%s keys=%s",
                event.type,
                self.session_id,
                sorted(event.data.keys()),
            )
            return
        target_workspace_root = str(event.data.get("workspace_root") or "").strip()
        if event.type in WORKSPACE_SCOPED_EVENT_TYPES and not target_workspace_root:
            logger.warning(
                "Dropping workspace-scoped event without workspace_root: type=%s session=%s keys=%s",
                event.type,
                self.session_id,
                sorted(event.data.keys()),
            )
            return
        apply_stream_event(
            getattr(self, "_conversation_streams", {}),
            target_conversation_id,
            event.type,
            event.data,
        )

        payload = self._build_ws_payload(event)
        if target_conversation_id:
            payload.setdefault("conversation_id", target_conversation_id)
            if event.type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                stream_state = getattr(self, "_conversation_streams", {}).get(target_conversation_id)
                message_id = str((stream_state or {}).get("message_id") or "").strip()
                if message_id:
                    payload.setdefault("message_id", message_id)
        if event.type in {"approval_request", "ask_user"}:
            request_id = str(
                payload.get("request_id")
                or payload.get("tool_call_id")
                or event.data.get("request_id")
                or event.data.get("tool_call_id")
                or ""
            ).strip()
            if request_id:
                self.turn_wait_state.pending_approval_payloads[request_id] = dict(payload)

        await self.send_payload(payload, log_context=f"event:{event.type}")
        if event.type not in _NOTIFICATION_HOOK_EVENT_TYPES:
            return
        # Notification is observational work. It must not hold up the
        # authoritative WebSocket projection or make a started child appear
        # only after it has completed. Keep the task session-owned so shutdown
        # can cancel and drain it with explicit cleanup accounting.
        hook_task = asyncio.create_task(
            self._run_notification_hook_for_event(event, dict(payload)),
            name=f"notification-hook:{event.type}",
        )
        self._notification_hook_tasks.add(hook_task)
        hook_task.add_done_callback(self._notification_hook_tasks.discard)
        hook_task.add_done_callback(_consume_task_result)

    async def _run_notification_hook_for_event(self, event: AgentEvent, payload: dict[str, Any]) -> None:
        from backend.hooks.runtime import run_notification_hook_for_event

        await run_notification_hook_for_event(event_type=event.type, payload=payload)

    def _build_ws_payload(self, event: AgentEvent) -> dict[str, Any]:
        if event.type == "approval_request":
            return self.build_approval_request_payload(event)

        if event.type == "ask_user":
            request_id = str(event.data.get("tool_call_id", "")).strip()
            question = str(event.data.get("question", "")).strip()
            request: dict[str, Any] = {
                "subtype": "elicitation",
                "tool_use_id": request_id,
                "prompt": question,
                "question": question,
            }
            for key in ("options", "choices", "allowed_values"):
                values = event.data.get(key)
                if isinstance(values, list):
                    request[key] = list(values)
            schema = event.data.get("schema")
            if isinstance(schema, dict):
                request["schema"] = dict(schema)
            return {
                "type": "control_request",
                "request_id": request_id,
                "request": request,
            }

        return event.to_ws_message()
