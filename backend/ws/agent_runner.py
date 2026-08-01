"""
Agent run logic mixin extracted from ws/handler.py.

SessionAgentRunnerMixin provides the _run_agent method which orchestrates
LLM refresh, query engine submission, cost tracking, transcript persistence,
and conversation summary/facts updates.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QuerySubmission
from backend.agent.runtime import default_runtime
from backend.agent.turn_state import AgentTurnState
from backend.config import get_available_models, get_llm_provider, load_config
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.utils import (
    build_conversation_summary,
    extract_turn_facts,
    merge_conversation_facts,
)
from backend.ws.stream_state import (
    create_stream_state,
    upsert_pending_tool_call,
)

_FAILED_TOOL_STATUSES = {"error", "failed", "blocked"}
UI_AGENT_STATE_SNAPSHOT_KEY = "ui_agent_state"
UI_AGENT_STATE_REVISION_KEY = "_ui_agent_state_revision"
_UI_AGENT_STATE_DEBOUNCE_SECONDS = 0.08
_PLAN_STATUSES = {"draft", "accepted", "executing", "completed", "cancelled"}
_PLAN_STEP_STATUSES = {"pending", "running", "done", "skipped", "failed"}
_TODO_STATUSES = {"pending", "in_progress", "completed", "blocked"}
_SUBAGENT_STATUSES = {"pending", "running", "blocked", "done", "partial", "cancelled", "error"}
_PROGRESS_STAGES = {"status", "planning", "tool", "approval", "verification", "final"}
_PROGRESS_STATUSES = {"running", "completed", "failed", "info"}
LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY = "llm_stateful_continuation"
logger = logging.getLogger(__name__)
_TURN_MESSAGE_SCOPED_EVENT_TYPES = {
    "item.started",
    "agent_message.delta",
    "item.completed",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "command_output_chunk",
    "tool_result",
    "agent.run.started",
    "agent.run.completed",
    "agent.item",
    "agent.progress",
    "runtime.span",
    "tool_use_summary",
    "task.update",
    "approval_request",
    "approval.file_diff",
    "ask_user",
    "citation.add",
    "artifact.preview",
    "done",
    "error",
    "stream_resume",
}
_TOOL_TURN_SCOPED_EVENT_TYPES = {
    "tool_call",
    "tool_output_delta",
    "command_output_chunk",
    "tool_result",
    "runtime.span",
}


def _failed_tool_call_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if str(record.get("status") or "").strip().lower() in _FAILED_TOOL_STATUSES
    ]


def _reply_attachments_from_tool_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect verified deliverables for transcript restoration after restart."""
    attachments: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for record in records:
        output_files = record.get("outputFiles") or record.get("output_files")
        if not isinstance(output_files, list):
            continue
        for item in output_files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            path_key = path.casefold()
            if not path or path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            try:
                size = max(0, int(item.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            attachments.append({
                "path": path,
                "size": size,
                "is_image": bool(item.get("isImage", item.get("is_image", False))),
            })
    return attachments


def _project_agent_message_event(
    turn_state: AgentTurnState,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_type == "item.started":
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if item.get("type") == "agent_message":
            turn_state.start_agent_message(str(item.get("id") or "agent-message"))
    elif event_type == "agent_message.delta":
        turn_state.append_agent_message_delta(
            str(data.get("item_id") or "agent-message"),
            str(data.get("delta") or ""),
        )
    elif event_type == "item.completed":
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if item.get("type") == "agent_message":
            turn_state.complete_agent_message(
                item,
                finish_reason=str(data.get("finish_reason") or ""),
                provider_raw=(
                    data.get("provider_raw")
                    if isinstance(data.get("provider_raw"), dict)
                    else None
                ),
            )


def _empty_ui_agent_state() -> dict[str, Any]:
    return {"plan": None, "todos": [], "subagents": [], "agentProgress": []}


def _reset_ui_agent_state_snapshot(snapshot: Any) -> dict[str, Any]:
    next_snapshot = dict(snapshot or {}) if isinstance(snapshot, dict) else {}
    next_snapshot[UI_AGENT_STATE_SNAPSHOT_KEY] = _empty_ui_agent_state()
    return next_snapshot


def _client_assistant_message_id(metadata: dict[str, Any] | None) -> str:
    raw = str((metadata or {}).get("assistant_message_id") or "").strip()
    if not raw:
        return ""
    if len(raw) > 128:
        return ""
    if not all(char.isalnum() or char in {"_", "-", ":", "."} for char in raw):
        return ""
    return raw


def _consume_previous_turn_aborted(session: Any, conversation_id: str = "") -> bool:
    conversation_id = str(conversation_id or "").strip()
    interrupted_conversations = getattr(session, "_interrupted_conversation_ids", None)
    if isinstance(interrupted_conversations, set) and conversation_id:
        if conversation_id in interrupted_conversations:
            interrupted_conversations.discard(conversation_id)
            return True
        return False
    previous_turn_aborted = bool(getattr(session, "_interrupted", False))
    session._interrupted = False
    return previous_turn_aborted


_LLM_ADAPTER_IDENTITY_FIELDS = (
    "api_key",
    "base_url",
    "reasoning_effort",
    "responses_reasoning_summary",
    "max_tokens",
    "wire_api",
    "responses_stateful_continuation",
    "prompt_cache_retention",
)


def _llm_settings_identity(value: Any) -> tuple[tuple[str, str], ...]:
    """Stable subset of LLM settings that changes adapter wire behavior.

    The effective model is already a separate cache-key dimension. Keeping it
    out of this subset lets a session preserve Responses continuation when the
    saved default model changes but the active run still uses the same explicit
    model override.
    """
    if value is None:
        return ()
    items: list[tuple[str, str]] = []
    for key in _LLM_ADAPTER_IDENTITY_FIELDS:
        raw = value.get(key) if isinstance(value, dict) else getattr(value, key, "")
        if key == "base_url":
            normalized = str(raw or "").strip().rstrip("/")
        elif key == "wire_api":
            normalized = str(raw or "").strip().lower()
        elif isinstance(raw, str):
            normalized = raw.strip()
        else:
            normalized = raw
        items.append((key, repr(normalized)))
    return tuple(items)


def _llm_adapter_cache_key(
    *,
    config: Any,
    provider: str,
    model: str,
) -> tuple[Any, ...]:
    agent = getattr(config, "agent", None)
    fallback_providers = tuple(getattr(agent, "fallback_providers", ()) or ())
    return (
        str(provider or "").strip(),
        str(model or "").strip(),
        _llm_settings_identity(getattr(config, "llm", None)),
        fallback_providers,
    )


def _clear_session_llm_cache(session: Any) -> None:
    cache = getattr(session, "_llm_adapter_cache", None)
    if isinstance(cache, dict):
        adapters = list({id(adapter): adapter for adapter in cache.values()}.values())
        cache.clear()
        close_tasks = getattr(session, "_llm_close_tasks", None)
        if not isinstance(close_tasks, set):
            close_tasks = set()
            setattr(session, "_llm_close_tasks", close_tasks)
        for adapter in adapters:
            close = getattr(adapter, "aclose", None)
            if not callable(close):
                continue
            task = asyncio.create_task(close())
            close_tasks.add(task)
            task.add_done_callback(close_tasks.discard)


def _get_or_create_session_llm(
    session: Any,
    *,
    config: Any,
    provider: str,
    model: str,
) -> Any:
    cache = getattr(session, "_llm_adapter_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(session, "_llm_adapter_cache", cache)
        except Exception:
            cache = {}
    key = _llm_adapter_cache_key(config=config, provider=provider, model=model)
    if key in cache:
        return cache[key]

    from backend.llm.model_registry import create_session_llm

    adapter = create_session_llm(config, model_override=model or None)
    cache.clear()
    cache[key] = adapter
    return adapter


def _iter_llm_stateful_continuation_targets(llm: Any) -> list[Any]:
    """Return adapters that can import/export provider continuation state."""
    targets: list[Any] = []
    seen: set[int] = set()

    def visit(candidate: Any) -> None:
        if candidate is None:
            return
        marker = id(candidate)
        if marker in seen:
            return
        seen.add(marker)
        if callable(getattr(candidate, "export_stateful_continuation", None)) or callable(
            getattr(candidate, "import_stateful_continuation", None)
        ):
            targets.append(candidate)
        adapters = getattr(candidate, "adapters", None)
        try:
            nested = adapters() if callable(adapters) else adapters
        except Exception:
            nested = None
        if isinstance(nested, (list, tuple)):
            for item in nested:
                visit(item)

    visit(llm)
    return targets


def _import_llm_stateful_continuation_from_snapshot(llm: Any, snapshot: Any) -> int:
    if not isinstance(snapshot, dict):
        return 0
    payload = snapshot.get(LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY)
    if not isinstance(payload, dict):
        return 0
    entries = payload.get("adapters")
    if isinstance(entries, list):
        payloads = [entry.get("payload") for entry in entries if isinstance(entry, dict)]
    else:
        payloads = [payload]

    restored = 0
    for target in _iter_llm_stateful_continuation_targets(llm):
        importer = getattr(target, "import_stateful_continuation", None)
        if not callable(importer):
            continue
        for item in payloads:
            if not isinstance(item, dict):
                continue
            try:
                restored += int(importer(item) or 0)
            except Exception:
                logger.debug("Failed to import LLM stateful continuation", exc_info=True)
    return restored


def _export_llm_stateful_continuation_snapshot(llm: Any) -> dict[str, Any]:
    adapters: list[dict[str, Any]] = []
    for target in _iter_llm_stateful_continuation_targets(llm):
        exporter = getattr(target, "export_stateful_continuation", None)
        if not callable(exporter):
            continue
        try:
            payload = exporter()
        except Exception:
            logger.debug("Failed to export LLM stateful continuation", exc_info=True)
            continue
        if isinstance(payload, dict) and payload:
            adapters.append({"adapter": type(target).__name__, "payload": payload})
    if not adapters:
        return {}
    return {"version": 1, "adapters": adapters[:4]}


def _coerce_ui_agent_state(value: Any) -> dict[str, Any]:
    state = _empty_ui_agent_state()
    if not isinstance(value, dict):
        return state
    if isinstance(value.get("plan"), dict) or value.get("plan") is None:
        state["plan"] = value.get("plan")
    if isinstance(value.get("todos"), list):
        state["todos"] = list(value["todos"])
    if isinstance(value.get("subagents"), list):
        state["subagents"] = list(value["subagents"])
    if isinstance(value.get("agentProgress"), list):
        state["agentProgress"] = list(value["agentProgress"])
    return state


def _normalized_plan_status(value: Any) -> str:
    status = str(value or "").strip()
    return status if status in _PLAN_STATUSES else "executing"


def _normalized_plan_step(step: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None
    title = str(step.get("title") or "").strip()
    if not title:
        return None
    status = str(step.get("status") or "pending").strip()
    return {
        "id": str(step.get("id") or f"step-{index}"),
        "title": title,
        **({"detail": str(step.get("detail"))} if step.get("detail") is not None else {}),
        "status": status if status in _PLAN_STEP_STATUSES else "pending",
    }


def _normalized_todo(todo: Any) -> dict[str, Any] | None:
    if not isinstance(todo, dict):
        return None
    todo_id = str(todo.get("id") or todo.get("todo_id") or "").strip()
    content = str(todo.get("content") or "").strip()
    status = str(todo.get("status") or "").strip()
    if not todo_id or not content or status not in _TODO_STATUSES:
        return None
    return {
        "id": todo_id,
        "content": content,
        "activeForm": str(todo.get("activeForm") or todo.get("active_form") or content),
        "status": status,
    }


def _upsert_by_id(items: list[Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    item_id = str(item.get("id") or "").strip()
    normalized_items = [existing for existing in items if isinstance(existing, dict)]
    for index, existing in enumerate(normalized_items):
        if str(existing.get("id") or "").strip() == item_id:
            normalized_items[index] = {**existing, **item}
            return normalized_items
    return [*normalized_items, item]


def _subagent_summary(data: dict[str, Any]) -> str:
    for key in ("summary", "prompt", "current_activity", "detail"):
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        return value
    return ""


def _ui_agent_state_for_event(current: Any, event_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
    state = _coerce_ui_agent_state(current)

    if event_type == "plan_updated":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            return None
        steps = [
            normalized
            for index, step in enumerate(raw_steps)
            if (normalized := _normalized_plan_step(step, index)) is not None
        ]
        state["plan"] = {
            "planId": str(data.get("plan_id") or "plan"),
            "status": _normalized_plan_status(data.get("status")),
            "currentStep": int(data.get("current_step") or 0),
            "steps": steps,
        }
        return state

    if event_type == "plan_step_updated":
        plan = state.get("plan")
        if not isinstance(plan, dict):
            return None
        if data.get("plan_id") and str(plan.get("planId") or "") != str(data.get("plan_id")):
            return None
        steps = list(plan.get("steps") or [])
        index = data.get("step_index")
        if not isinstance(index, int):
            step_id = str(data.get("step_id") or "")
            index = next((i for i, step in enumerate(steps) if isinstance(step, dict) and str(step.get("id") or "") == step_id), -1)
        if index < 0 or index >= len(steps) or str(data.get("status") or "") not in _PLAN_STEP_STATUSES:
            return None
        existing = steps[index] if isinstance(steps[index], dict) else {}
        steps[index] = {
            **existing,
            **({"title": str(data.get("title"))} if data.get("title") is not None else {}),
            **({"detail": str(data.get("detail"))} if data.get("detail") is not None else {}),
            "status": str(data.get("status")),
        }
        state["plan"] = {
            **plan,
            "steps": steps,
            "currentStep": int(data.get("current_step") if isinstance(data.get("current_step"), int) else index),
            "status": "executing" if plan.get("status") not in {"completed", "cancelled"} else plan.get("status"),
        }
        return state

    if event_type == "task.update":
        if isinstance(data.get("session"), dict):
            return None
        if isinstance(data.get("todos"), list):
            state["todos"] = [
                todo
                for raw in data["todos"]
                if (todo := _normalized_todo(raw)) is not None
            ]
            return state
        todo = _normalized_todo(data)
        if todo is None:
            return None
        state["todos"] = _upsert_by_id(list(state.get("todos") or []), todo)
        return state

    if event_type == "subagent.start":
        subagent_id = str(data.get("subagent_id") or "").strip()
        if not subagent_id:
            return None
        state["subagents"] = _upsert_by_id(list(state.get("subagents") or []), {
            "id": subagent_id,
            "role": str(data.get("role") or "subagent"),
            "status": "running",
            "summary": str(data.get("prompt") or ""),
            **({"parentRunId": str(data.get("parent_run_id"))} if data.get("parent_run_id") is not None else {}),
            **({"turnId": str(data.get("turn_id"))} if data.get("turn_id") else {}),
        })[-20:]
        return state

    if event_type == "subagent.progress":
        subagent_id = str(data.get("subagent_id") or "").strip()
        if not subagent_id:
            return None
        existing_subagent = next(
            (item for item in state.get("subagents", [])
             if isinstance(item, dict) and str(item.get("id") or "") == subagent_id),
            None,
        )
        if isinstance(existing_subagent, dict) and str(existing_subagent.get("status") or "") in {
            "done", "partial", "cancelled", "error",
        }:
            return state
        summary = _subagent_summary(data)
        state["subagents"] = _upsert_by_id(list(state.get("subagents") or []), {
            "id": subagent_id,
            "role": "subagent",
            "status": "running",
            **({"summary": summary} if summary else {}),
            **({"iteration": data.get("iteration")} if isinstance(data.get("iteration"), int) else {}),
            **({"maxIterations": data.get("max_iterations")} if isinstance(data.get("max_iterations"), int) else {}),
            **({"currentTool": str(data.get("tool_name"))} if data.get("tool_name") is not None else {}),
            **({"detail": str(data.get("detail"))} if data.get("detail") is not None else {}),
            **({"currentActivity": str(data.get("current_activity"))} if data.get("current_activity") is not None else {}),
            **({"waitingOn": str(data.get("waiting_on"))} if data.get("waiting_on") is not None else {}),
            **({"lastProgressAt": data.get("last_progress_at")} if isinstance(data.get("last_progress_at"), int) else {}),
        })[-20:]
        return state

    if event_type == "subagent.done":
        subagent_id = str(data.get("subagent_id") or "").strip()
        if not subagent_id:
            return None
        raw_status = str(data.get("status") or "completed").strip().lower()
        event_error = data.get("error") if isinstance(data.get("error"), str) else ""
        status = (
            "partial" if raw_status == "partial"
            else "cancelled" if raw_status in {"cancelled", "interrupted"}
            else "error" if event_error.strip() or raw_status in {"error", "failed"}
            else "done"
        )
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        record = data.get("record") if isinstance(data.get("record"), dict) else {}
        result_content = str(result.get("content") or data.get("summary") or "").strip()
        result_error = str(result.get("error") or event_error or "").strip()
        state["subagents"] = _upsert_by_id(list(state.get("subagents") or []), {
            "id": subagent_id,
            "role": str(record.get("agent_type") or record.get("role") or "subagent"),
            "status": status if status in _SUBAGENT_STATUSES else "done",
            "summary": str(event_error or data.get("summary") or ""),
            "resultAvailable": bool(result_content or result_error),
            **({"resultContent": result_content} if result_content else {}),
            **({"resultError": result_error} if result_error else {}),
            **({"durationMs": data.get("duration_ms")} if isinstance(data.get("duration_ms"), int) else {}),
            **({"iteration": data.get("iterations")} if isinstance(data.get("iterations"), int) else {}),
            **({"toolCallCount": data.get("tool_call_count")} if isinstance(data.get("tool_call_count"), int) else {}),
            **({"terminationReason": str(data.get("termination_reason"))} if data.get("termination_reason") is not None else {}),
            **({"terminationInitiator": str(data.get("initiator"))} if data.get("initiator") is not None else {}),
            **({"checkpointId": str(record.get("checkpoint_id"))} if record.get("checkpoint_id") else {}),
            **({"objective": str(record.get("objective"))} if record.get("objective") else {}),
            **({"parentRunId": str(record.get("parent_run_id"))} if record.get("parent_run_id") else {}),
            **({"turnId": str(data.get("turn_id"))} if data.get("turn_id") else {}),
        })[-20:]
        return state

    if event_type == "agent.progress":
        stage = str(data.get("stage") or "").strip()
        status = str(data.get("status") or "").strip()
        progress_id = str(data.get("id") or "").strip()
        message = str(data.get("message") or "").strip()
        if (
            not progress_id
            or not message
            or stage not in _PROGRESS_STAGES
            or status not in _PROGRESS_STATUSES
            or data.get("visibility") == "debug"
        ):
            return None
        existing_progress = next(
            (item for item in state.get("agentProgress", [])
             if isinstance(item, dict) and str(item.get("id") or "") == progress_id),
            None,
        )
        if (
            isinstance(existing_progress, dict)
            and str(existing_progress.get("status") or "") in {"completed", "failed"}
            and status not in {"completed", "failed"}
        ):
            return state
        entry = {
            "type": "progress",
            "id": progress_id,
            "stage": stage,
            **({"phase": str(data.get("phase"))} if data.get("phase") is not None else {}),
            "status": status,
            "message": message,
            **({"label": str(data.get("label"))} if data.get("label") is not None else {}),
            **({"summary": str(data.get("summary"))} if data.get("summary") is not None else {}),
            **({"visibility": str(data.get("visibility"))} if data.get("visibility") is not None else {}),
            **({"detail": str(data.get("detail"))} if data.get("detail") is not None else {}),
            **({"toolCallId": str(data.get("tool_call_id"))} if data.get("tool_call_id") is not None else {}),
            **({"toolName": str(data.get("tool_name"))} if data.get("tool_name") is not None else {}),
            **({"groupId": str(data.get("group_id"))} if data.get("group_id") is not None else {}),
            **({"stepId": str(data.get("step_id"))} if data.get("step_id") is not None else {}),
            **({"count": data.get("count")} if isinstance(data.get("count"), int) else {}),
            **({"iterationId": str(data.get("iteration_id"))} if data.get("iteration_id") is not None else {}),
            "timestamp": int(time.time() * 1000),
        }
        state["agentProgress"] = _upsert_by_id(list(state.get("agentProgress") or []), entry)[-80:]
        return state

    return None


def _merge_ui_agent_state_into_snapshot(snapshot: dict[str, Any], source_snapshot: Any) -> dict[str, Any]:
    if isinstance(source_snapshot, dict) and UI_AGENT_STATE_SNAPSHOT_KEY in source_snapshot:
        snapshot[UI_AGENT_STATE_SNAPSHOT_KEY] = source_snapshot[UI_AGENT_STATE_SNAPSHOT_KEY]
        if UI_AGENT_STATE_REVISION_KEY in source_snapshot:
            snapshot[UI_AGENT_STATE_REVISION_KEY] = source_snapshot[UI_AGENT_STATE_REVISION_KEY]
    return snapshot


def _clear_turn_scoped_tool_state(tool_registry: Any, conversation_id: str) -> None:
    if not conversation_id:
        return
    get_tool = getattr(tool_registry, "get_tool", None)
    if not callable(get_tool):
        return
    todo_tool = get_tool("todo_write")
    clear_todos = getattr(todo_tool, "clear_session_todos", None)
    if callable(clear_todos):
        clear_todos(conversation_id)


class SessionAgentRunnerMixin:
    """Agent run logic for WebSocketSession.

    Depends on session attributes: ws, query_engine, conversation_repo,
    context_builder, permission_checker, permission_context, config,
    llm, artifact_store, tool_registry, skill_manager,
    _approval_handler, _active_task_id, _interrupted, etc.
    """

    async def _flush_ui_agent_state_now(self, conversation_id: str) -> None:
        pending = getattr(self, "_ui_agent_state_pending", {})
        tasks = getattr(self, "_ui_agent_state_tasks", {})
        current_task = asyncio.current_task()
        scheduled = tasks.get(conversation_id)
        if scheduled is not None and scheduled is not current_task:
            try:
                await scheduled
            except asyncio.CancelledError:
                pass

        item = pending.pop(conversation_id, None)
        if item is not None:
            revision, state = item
            await asyncio.to_thread(
                self.conversation_repo.patch_context_snapshot,
                conversation_id,
                {UI_AGENT_STATE_SNAPSHOT_KEY: state},
                revision=revision,
                revision_key=UI_AGENT_STATE_REVISION_KEY,
            )

        if tasks.get(conversation_id) is current_task:
            tasks.pop(conversation_id, None)
        if conversation_id in pending and conversation_id not in tasks:
            tasks[conversation_id] = asyncio.create_task(
                self._flush_ui_agent_state_after_delay(conversation_id)
            )

    async def _flush_ui_agent_state_after_delay(self, conversation_id: str) -> None:
        try:
            await asyncio.sleep(_UI_AGENT_STATE_DEBOUNCE_SECONDS)
            await self._flush_ui_agent_state_now(conversation_id)
        except asyncio.CancelledError:
            raise

    def _persist_ui_agent_state_event(
        self,
        conversation_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if not conversation_id:
            return
        cache = getattr(self, "_ui_agent_state_cache", None)
        pending = getattr(self, "_ui_agent_state_pending", None)
        tasks = getattr(self, "_ui_agent_state_tasks", None)
        if not isinstance(cache, dict):
            cache = self._ui_agent_state_cache = {}
        if not isinstance(pending, dict):
            pending = self._ui_agent_state_pending = {}
        if not isinstance(tasks, dict):
            tasks = self._ui_agent_state_tasks = {}

        cached = cache.get(conversation_id)
        if isinstance(cached, tuple) and len(cached) == 2:
            revision, current_state = cached
        else:
            record = self.conversation_repo.get_conversation(conversation_id)
            snapshot = dict(getattr(record, "context_snapshot", {}) or {}) if record is not None else {}
            revision = int(snapshot.get(UI_AGENT_STATE_REVISION_KEY) or 0)
            current_state = snapshot.get(UI_AGENT_STATE_SNAPSHOT_KEY)
        next_state = _ui_agent_state_for_event(
            current_state,
            event_type,
            data,
        )
        if next_state is None:
            return
        revision = max(revision + 1, time.time_ns())
        cache[conversation_id] = (revision, next_state)
        pending[conversation_id] = (revision, next_state)
        task = tasks.get(conversation_id)
        if task is None or task.done():
            tasks[conversation_id] = asyncio.create_task(
                self._flush_ui_agent_state_after_delay(conversation_id)
            )

    async def _run_agent(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        target_conversation_id = conversation_id or self.active_conversation_id or ""
        if not target_conversation_id:
            self._ensure_active_conversation()
            target_conversation_id = self.active_conversation_id or ""
        locks = getattr(self, "_conversation_run_locks", None)
        lock = None
        if isinstance(locks, dict) and target_conversation_id:
            lock = locks.setdefault(target_conversation_id, asyncio.Lock())
        async with (lock or self._agent_run_lock):
            await self._run_agent_locked(
                user_message,
                attachments=attachments,
                conversation_id=target_conversation_id or None,
                metadata=metadata,
                cancel_event=cancel_event,
            )

    async def _run_agent_locked(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        run_interrupted = False
        run_cancel_event = cancel_event or asyncio.Event()
        # Hot-reload any MCP tools connected since this session was created, so
        # this run sees them. Rebuilds a new registry object before the run
        # captures self.tool_registry below; in-flight runs keep their own.
        self.refresh_tool_registry_if_mcp_changed()
        target_conversation_id = conversation_id or self.active_conversation_id or ""
        if not target_conversation_id:
            self._ensure_active_conversation()
            target_conversation_id = self.active_conversation_id or ""
        conversation = self.conversation_repo.get_conversation(target_conversation_id)
        if conversation is None:
            await emit_conversation_not_found(self, target_conversation_id)
            return
        previous_turn_aborted = _consume_previous_turn_aborted(self, conversation.id)
        is_active_conversation_run = conversation.id == self.active_conversation_id
        await self._flush_ui_agent_state_now(conversation.id)
        getattr(self, "_ui_agent_state_cache", {}).pop(conversation.id, None)
        run_context_snapshot = _reset_ui_agent_state_snapshot(conversation.context_snapshot)
        self.conversation_repo.save_context_snapshot(conversation.id, run_context_snapshot)
        _clear_turn_scoped_tool_state(self.tool_registry, conversation.id)

        # Track active streaming metadata for reconnection recovery. Prefer
        # the client-created assistant placeholder id so late events from an
        # old turn cannot attach to a newer assistant message.
        assistant_message_id = _client_assistant_message_id(metadata) or f"assistant_{uuid.uuid4().hex[:8]}"
        stream_state = create_stream_state(conversation.id, assistant_message_id)
        getattr(self, "_conversation_streams", {})[conversation.id] = stream_state

        # Each run uses a local LLM/provider/model/config snapshot to avoid overwriting
        # session fields that concurrent runs might be reading
        try:
            run_config = load_config()
            provider_resolver = getattr(self, "_resolve_llm_provider", get_llm_provider)
            models_resolver = getattr(self, "_resolve_available_models", get_available_models)
            models_source_resolver = getattr(self, "_resolve_models_source", None)
            run_provider = provider_resolver()
            run_available_models = list(models_resolver(run_provider))
            run_models_source = models_source_resolver(run_provider) if models_source_resolver else ""

            # Determine model for this run
            config_model = getattr(run_config.llm, "model", "").strip()
            if run_provider != self.provider:
                # Provider changed: use config model, not any previous override
                run_model = config_model
            elif not self._model_override_active:
                # No override: track config changes
                run_model = config_model
            else:
                # Override active: keep using selected_model
                run_model = self.selected_model

            if run_available_models and run_model and run_model not in run_available_models:
                run_model = ""
            if not run_model and run_available_models:
                run_model = run_available_models[0]

            run_llm = _get_or_create_session_llm(
                self,
                config=run_config,
                provider=run_provider,
                model=run_model,
            )

            # Only update session fields if this is the active conversation run
            # This keeps the UI in sync without breaking concurrent background runs
            if is_active_conversation_run:
                self.config = run_config
                self.provider = run_provider
                self.available_models = run_available_models
                self.models_source = run_models_source
                self.selected_model = run_model
                self.llm = run_llm
                self.context_builder._llm = run_llm
        except Exception as exc:
            classification = classify_llm_error(exc)
            error_message = sanitize_llm_error_message(exc, classification)
            await self._send_event(
                AgentEvent(
                    type="error",
                    data={
                        "message": error_message,
                        "recoverable": not classification.fatal,
                        "error_type": classification.error_type,
                        "provider_error_type": classification.provider_error_type,
                        "conversation_id": conversation.id,
                        "message_id": assistant_message_id,
                    },
                )
            )
            done_event = AgentEvent.done(status="failed", reason="llm_initialization_failed")
            done_event.data["conversation_id"] = conversation.id
            done_event.data["message_id"] = assistant_message_id
            await self._send_event(done_event)
            await self._send_event(AgentEvent.session_state_changed(
                state="idle",
                conversation_id=conversation.id,
                reason="failed",
            ))
            run_manager = getattr(self, "_run_manager", None)
            mark_terminal_status = getattr(run_manager, "mark_terminal_status", None)
            if callable(mark_terminal_status):
                mark_terminal_status(conversation.id, "failed")
            getattr(self, "_conversation_streams", {}).pop(conversation.id, None)
            return

        run_workspace_root = self._workspace_root_for_conversation(conversation)
        run_memory_manager = getattr(self, "memory_manager", None)
        if run_workspace_root is not None:
            from backend.memory.file_memory import FileMemory
            from backend.memory.manager import MemoryManager

            run_memory_manager = MemoryManager(FileMemory.for_workspace(run_workspace_root))

        run_context_builder = ContextBuilder(
            token_budget=run_config.token_budget,
            agent_settings=run_config.agent,
            skill_executor=getattr(self, "skill_executor", None),
            memory_manager=run_memory_manager,
            llm=run_llm,
            skill_manager=self.skill_manager,
        )
        run_context_builder.load_snapshot(run_context_snapshot)
        _import_llm_stateful_continuation_from_snapshot(run_llm, run_context_snapshot)
        normalized_attachments = list(attachments or [])
        run_metadata = dict(metadata or {})
        if previous_turn_aborted:
            run_metadata.setdefault("previous_turn_aborted", True)
        run_metadata.setdefault("assistant_message_id", assistant_message_id)
        run_metadata.setdefault("agent_runtime", default_runtime())
        run_workspace_context = self._workspace_context_for_conversation(conversation)
        run_metadata.setdefault("workspace_context", run_workspace_context)
        run_metadata.setdefault("conversation_id", conversation.id)
        run_metadata.setdefault("cost_session_id", self.session_id)
        run_metadata.setdefault("requires_explicit_workspace", True)
        mcp_manager = getattr(self, "mcp_manager", None)
        iter_connected_mcp = getattr(mcp_manager, "iter_connected_clients", None)
        connected_mcp_servers: list[str] = []
        if callable(iter_connected_mcp):
            try:
                connected_mcp_servers = [
                    str(name)
                    for name, _client in iter_connected_mcp()
                    if str(name).strip()
                ]
            except Exception as exc:
                logger.debug("Unable to snapshot connected MCP servers: %s", exc)
        run_metadata.setdefault("connected_mcp_servers", connected_mcp_servers)
        run_permission_context = self._permission_context_for_conversation(
            conversation,
            source="agent.run",
        )

        async def _set_run_permission_mode(mode: str, *, source: str = "agent.run") -> None:
            if mode != "plan":
                return
            updated = self.conversation_repo.update_permission_mode(conversation.id, mode)
            if updated is not None:
                self._set_permission_context_mode(mode, source=source)
                await self._emit_permission_mode_updated()
                send_conversations = getattr(self, "_send_conversation_list", None)
                if callable(send_conversations):
                    result = send_conversations()
                    if asyncio.iscoroutine(result):
                        await result
            # ``PermissionContext`` is an immutable turn snapshot.  EnterPlanMode
            # updates the live tool context itself; mutating this frozen snapshot
            # used to raise ``FrozenInstanceError`` after the plan-mode switch.
            # Keep the callback side-effect free after persistence/UI updates.

        def _live_run_permission_context():
            """Return the session's current permission state for this run."""
            if self.active_conversation_id == conversation.id:
                return self.permission_context
            current = self.conversation_repo.get_conversation(conversation.id)
            return self._permission_context_for_conversation(current, source="agent.run.live")

        run_metadata.setdefault("permission_mode_setter", _set_run_permission_mode)
        run_metadata.setdefault("permission_context_provider", _live_run_permission_context)
        run_manager = getattr(self, "_run_manager", None)
        turn_input_queue = getattr(run_manager, "turn_input_queue", None)
        if callable(turn_input_queue):
            run_metadata.setdefault("turn_input_queue", turn_input_queue(conversation.id))

        def _persist_consumed_turn_input(item: Any) -> None:
            append_transcript = getattr(self.conversation_repo, "append_transcript_message", None)
            if not callable(append_transcript):
                return
            context_refs = [
                {"kind": "skill", "name": value["name"], "path": value["path"]}
                for value in getattr(item, "selected_skills", ())
            ]
            context_refs.extend(
                {
                    "kind": "plugin",
                    "name": value["config_name"],
                    "config_name": value["config_name"],
                    "path": value["path"],
                }
                for value in getattr(item, "selected_plugins", ())
            )
            append_transcript(
                conversation.id,
                {
                    "id": str(getattr(item, "user_message_id", "") or f"user_{getattr(item, 'message_id', '')}"),
                    "role": "user",
                    "content": str(getattr(item, "content", "") or ""),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "attachments": [dict(value) for value in getattr(item, "attachments", ())],
                    **({"context_refs": context_refs} if context_refs else {}),
                    "steered": True,
                    "steer_target_message_id": str(getattr(item, "target_message_id", "") or ""),
                },
            )

        run_metadata.setdefault("persist_consumed_turn_input", _persist_consumed_turn_input)

        def _acknowledge_consumed_turn_input(item: Any) -> None:
            acknowledge = getattr(run_manager, "acknowledge_turn_input", None)
            if not callable(acknowledge):
                return
            acknowledge(conversation.id, getattr(item, "original_command", None))

        run_metadata.setdefault(
            "acknowledge_consumed_turn_input",
            _acknowledge_consumed_turn_input,
        )

        selected_skills = run_metadata.get("selected_skills")
        selected_plugins = run_metadata.get("selected_plugins")
        persisted_context_refs: list[dict[str, str]] = []
        if isinstance(selected_skills, list):
            persisted_context_refs.extend(
                {
                    "kind": "skill",
                    "name": str(item.get("name") or ""),
                    "path": str(item.get("path") or ""),
                }
                for item in selected_skills
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
        if isinstance(selected_plugins, list):
            persisted_context_refs.extend(
                {
                    "kind": "plugin",
                    "name": str(item.get("config_name") or item.get("name") or ""),
                    "config_name": str(item.get("config_name") or item.get("name") or ""),
                    "path": str(item.get("path") or ""),
                }
                for item in selected_plugins
                if isinstance(item, dict)
                and str(item.get("config_name") or item.get("name") or "").strip()
            )

        self.conversation_repo.append_transcript_message(
            conversation.id,
            {
                "id": str(run_metadata.get("user_message_id") or f"user_{uuid.uuid4().hex[:8]}"),
                "role": "user",
                "content": user_message,
                "timestamp": datetime.now(UTC).isoformat(),
                "attachments": normalized_attachments,
                **({"context_refs": persisted_context_refs} if persisted_context_refs else {}),
            },
        )

        from backend.llm.cost_tracker import CostTracker
        tracker = CostTracker.get_instance()
        start_time = time.monotonic()

        # Attach workspace context to agent state so ContextBuilder can inject it
        from backend.agent.state import AgentState
        agent_state = AgentState(
            user_message=user_message,
            max_iterations=run_config.agent.max_iterations,
        )
        if isinstance(selected_skills, list):
            agent_state.prompt_context["selected_skills"] = [
                dict(item) for item in selected_skills if isinstance(item, dict)
            ]
        if isinstance(selected_plugins, list):
            from backend.services.plugin_settings_service import resolve_enabled_plugin_mentions
            plugin_injections = resolve_enabled_plugin_mentions(
                [item for item in selected_plugins if isinstance(item, dict)],
                connected_mcp_servers=connected_mcp_servers,
            )
            if plugin_injections:
                agent_state.prompt_context["plugin_injections"] = plugin_injections
        agent_state.workspace_context = run_workspace_context
        agent_state.attachments = normalized_attachments
        agent_state.checkpoint_manager = getattr(self, "checkpoint_manager", None)
        agent_state.conversation_id = conversation.id
        goal = dict(getattr(conversation, "goal", {}) or {})
        goal_text = str(goal.get("text") or "").strip()
        if goal_text:
            goal_status = str(goal.get("status") or "active").strip().lower()
            if goal_status == "paused":
                agent_state.task_summary = (
                    f"Conversation goal is paused: {goal_text}\n"
                    "Do not proactively continue this goal unless the user asks to resume or work on it."
                )
            else:
                agent_state.task_summary = (
                    f"Current conversation goal: {goal_text}\n"
                    "Treat user turns as progress toward this goal unless the user clearly changes direction."
                )
        self._last_agent_state = agent_state

        conv_id = conversation.id

        async def _stream_callback(
            line: str,
            stream: str = "stdout",
            tool_call_id: str = "",
        ) -> None:
            payload: dict[str, Any] = {
                "conversation_id": conv_id,
                "message_id": assistant_message_id,
                "turn_id": assistant_message_id,
                "content": line,
                "stream": stream if stream in {"stdout", "stderr"} else "stdout",
            }
            if tool_call_id:
                payload["id"] = tool_call_id
                payload["tool_call_id"] = tool_call_id
                turn_state.record_tool_output_delta({
                    "id": tool_call_id,
                    "output": line,
                    "stream": payload["stream"],
                })
                await _persist_partial_turn()
            await self._send_event(AgentEvent(type="command_output_chunk", data=payload))

        async def _emit_runtime_event(event_type: str, data: dict[str, Any]) -> None:
            payload = dict(data)
            payload.setdefault("conversation_id", conversation.id)
            if event_type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                payload.setdefault("message_id", assistant_message_id)
                # Use the canonical turn_id (= run_id, captured into stream_state
                # from agent.run.started) so callback/runtime.span events group
                # with the Path-A tool_call/tool_result events the EventEnvelope
                # stamps. Fall back to assistant_message_id only before run.started.
                payload.setdefault("turn_id", stream_state.get("turn_id") or assistant_message_id)
                # Stamp task_id from the same source as the EventEnvelope so
                # callback-emitted events are not missing the multi-agent task id.
                payload.setdefault(
                    "task_id",
                    getattr(self, "_conversation_run_task_ids", {}).get(
                        conversation.id, getattr(self, "_active_task_id", "") or ""
                    ),
                )
            self._persist_ui_agent_state_event(conversation.id, event_type, payload)
            if event_type == "runtime.span":
                span_id = str(payload.get("span_id") or "").strip()
                span_event = str(payload.get("event") or "")
                span_status = str(payload.get("status") or "")
                if span_id and (span_event.endswith(".started") or span_status == "running"):
                    active_runtime_spans[span_id] = dict(payload)
                elif span_id and (
                    span_event.endswith((".completed", ".failed", ".cancelled", ".interrupted"))
                    or span_status in {"completed", "failed", "cancelled", "interrupted"}
                ):
                    active_runtime_spans.pop(span_id, None)
            _project_agent_message_event(turn_state, event_type, payload)
            if event_type == "agent.item" or (
                event_type == "agent.progress"
                and str(payload.get("status") or "").strip().lower()
                in {"completed", "failed", "partial", "cancelled", "interrupted"}
            ):
                await _persist_partial_turn(force=True)
            await self._send_event(AgentEvent(type=event_type, data=payload))

        assistant_artifacts: list[dict[str, Any]] = []
        usage_payload: dict[str, int] | None = None
        run_failed_message = ""
        done_event_sent = False
        query_terminal_status = ""
        query_terminal_reason = ""
        query_done_payload: dict[str, Any] = {}
        query_run_completed_payload: dict[str, Any] = {}
        active_runtime_spans: dict[str, dict[str, Any]] = {}
        assistant_message_id = str(stream_state.get("message_id") or assistant_message_id)

        def _now_ms() -> int:
            return int(time.time() * 1000)

        turn_state = AgentTurnState(now_ms=_now_ms)
        turn_started_at_ms = _now_ms()
        awaiting_user_input = False
        partial_persist_lock = asyncio.Lock()
        last_partial_persisted_at = 0.0

        async def _persist_partial_turn(*, force: bool = False) -> None:
            """Checkpoint the current typed turn without writing every token."""
            nonlocal last_partial_persisted_at
            upsert = getattr(self.conversation_repo, "upsert_transcript_message", None)
            if not callable(upsert):
                return
            async with partial_persist_lock:
                now = time.monotonic()
                # Codex durably journals streamed response items. MiniCode's
                # transcript stores the current projection instead, so cap
                # delta-driven rewrites while always committing lifecycle
                # boundaries such as tool results and approval waits.
                if not force and now - last_partial_persisted_at < 1.0:
                    return
                snapshot = turn_state.finalize(terminal_status="partial")
                if not snapshot.blocks and not assistant_artifacts:
                    return
                partial_message: dict[str, Any] = {
                    "id": assistant_message_id,
                    "role": "assistant",
                    "content": snapshot.content,
                    "timestamp": datetime.fromtimestamp(turn_started_at_ms / 1000, UTC).isoformat(),
                    "terminal_status": "partial",
                    "termination_reason": "run_in_progress",
                    "duration_ms": max(0, _now_ms() - turn_started_at_ms),
                    "usage": snapshot.usage,
                    "blocks": snapshot.blocks,
                }
                if snapshot.tool_calls:
                    partial_message["tool_calls"] = snapshot.tool_calls
                if assistant_artifacts:
                    partial_message["artifacts"] = list(assistant_artifacts)
                if snapshot.citations:
                    partial_message["citations"] = snapshot.citations
                try:
                    await asyncio.to_thread(upsert, conversation.id, partial_message)
                    last_partial_persisted_at = time.monotonic()
                except Exception:
                    logger.exception(
                        "Failed to persist partial assistant transcript projection for conversation %s",
                        conversation.id,
                    )
                    return
                try:
                    saved_snapshot = run_context_builder.export_snapshot()
                    llm_stateful_snapshot = _export_llm_stateful_continuation_snapshot(run_llm)
                    if llm_stateful_snapshot:
                        saved_snapshot[LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY] = llm_stateful_snapshot
                    else:
                        saved_snapshot.pop(LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY, None)
                    latest_conversation = self.conversation_repo.get_conversation(conversation.id)
                    _merge_ui_agent_state_into_snapshot(
                        saved_snapshot,
                        getattr(latest_conversation, "context_snapshot", None),
                    )
                    await asyncio.to_thread(
                        self.conversation_repo.save_context_snapshot,
                        conversation.id,
                        saved_snapshot,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist partial context snapshot for conversation %s",
                        conversation.id,
                    )

        async def _maybe_emit_source_citation(data: dict[str, Any]) -> None:
            citation = turn_state.record_source_citation(data)
            if citation is None:
                return
            await self._send_event(AgentEvent(type="citation.add", data={
                "conversation_id": conversation.id,
                "message_id": assistant_message_id,
                **citation,
            }))

        async def _emit_no_final_reply_summary_if_needed() -> None:
            nonlocal run_failed_message
            nonlocal query_terminal_status, query_terminal_reason
            if query_terminal_status in {"partial", "failed", "cancelled", "interrupted"}:
                return
            if awaiting_user_input:
                return
            tool_records = turn_state.tool_call_records()
            current_reply = turn_state.content().strip()
            # Text routing is structural. Never reinterpret or erase a provider
            # answer because its wording resembles process narration.
            if current_reply:
                return
            failed_tools = bool(_failed_tool_call_records(tool_records))
            query_terminal_status = "failed" if failed_tools or not tool_records else "partial"
            if not query_terminal_reason:
                query_terminal_reason = "missing_final_answer"
            if not run_failed_message:
                run_failed_message = (
                    "Tool calls failed before the model produced a final response."
                    if failed_tools
                    else "The model ended without producing a final response."
                )
                turn_state.record_error({"message": run_failed_message})

        async def _cancel_child_subagents_for_run(reason: str) -> None:
            run_id = str(run_metadata.get("run_id") or "").strip()
            runtime = run_metadata.get("agent_runtime")
            cancel_children = getattr(runtime, "cancel_child_subagent_tasks", None)
            if not run_id or not callable(cancel_children):
                return
            try:
                cancel_children(run_id, reason=reason)
            except Exception:
                logger.debug("Failed to cancel child subagents for run %s", run_id, exc_info=True)

        def _terminal_delivery_complete() -> bool:
            checker = getattr(run_manager, "is_delivery_complete", None)
            return bool(callable(checker) and checker(conversation.id))

        async def _send_done_once(status: str = "completed", reason: str = "") -> None:
            nonlocal done_event_sent
            if done_event_sent or _terminal_delivery_complete():
                done_event_sent = True
                return
            # Include accumulated usage so error/budget termination paths
            # (which don't emit their own done event from the loop) still
            # report correct token usage to the client.
            u = usage_payload or {}
            done_event = AgentEvent.done(
                status=status,
                reason=reason,
                duration_ms=round((time.monotonic() - start_time) * 1000),
                input_tokens=int(u.get("input_tokens", 0) or 0),
                output_tokens=int(u.get("output_tokens", 0) or 0),
                cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
                cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
                cache_deleted_input_tokens=int(u.get("cache_deleted_input_tokens", 0) or 0),
                reasoning_output_tokens=int(u.get("reasoning_output_tokens", 0) or 0),
            )
            for key, value in query_done_payload.items():
                if key not in {"conversation_id", "message_id", "status", "reason"}:
                    done_event.data[key] = value
            done_event.data["conversation_id"] = conversation.id
            done_event.data["message_id"] = assistant_message_id
            await self._send_event(done_event)
            done_event_sent = True

        from backend.ws.reasoning_batcher import ReasoningEventBatcher

        reasoning_batcher = ReasoningEventBatcher()

        async def _flush_pending_reasoning() -> None:
            pending = reasoning_batcher.flush_if_pending()
            if pending is not None:
                await self._send_event(pending)
                await _persist_partial_turn()

        try:
            # Emit session.state_changed: working
            await self._send_event(AgentEvent.session_state_changed(
                state="working",
                conversation_id=conversation.id,
                reason="agent_run_started",
            ))
            async for event in self.query_engine.submit(
                QuerySubmission(
                    user_message=user_message,
                    session=AgentSession(
                        llm=run_llm,
                        tool_registry=self.tool_registry,
                        artifact_store=self.artifact_store,
                        permission_checker=self.permission_checker,
                        agent_settings=run_config.agent,
                        token_budget=run_config.token_budget,
                        context_builder=run_context_builder,
                        approval_handler=self._approval_handler,
                    ),
                    state=agent_state,
                    runtime=AgentLoopSessionContext(
                        skill_manager=self.skill_manager,
                        permission_context=run_permission_context,
                        workspace_root=run_workspace_root,
                        session_id=self.session_id,
                        task_id=getattr(self, "_conversation_run_task_ids", {}).get(conversation.id, self._active_task_id or ""),
                        task_manager=self.task_manager,
                        background_manager=getattr(self, "background_manager", None),
                        terminal_manager=getattr(self, "terminal_manager", None),
                        emit_event=_emit_runtime_event,
                        metadata=run_metadata,
                        stream_callback=_stream_callback,
                        cancel_event=run_cancel_event,
                    ),
                )
            ):
                if event.type == "context_compacted":
                    self.conversation_repo.update_compaction(
                        conversation.id,
                        "compacted",
                        str(event.data.get("summary", "")),
                    )
                    await self._send_ws_payload(
                        {
                            "type": "conversation.compaction.updated",
                            "conversation_id": conversation.id,
                            "state": "compacted",
                            "summary": event.data.get("summary", ""),
                        },
                        log_context="conversation.compaction.updated",
                    )
                    event.data.setdefault("conversation_id", conversation.id)
                    await self._send_event(event)
                    continue

                if event.type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                    event.data.setdefault("message_id", assistant_message_id)
                    # All turn-scoped events now carry turn_id.  The
                    # QueryEngine's EventEnvelope already stamps turn_id
                    # (= run_id) on these events; setdefault preserves that
                    # and only falls back to assistant_message_id when the
                    # envelope hasn't stamped yet (e.g. before run.started).
                    event.data.setdefault("turn_id", assistant_message_id)
                    event_turn_id = str(event.data.get("turn_id") or "").strip()
                    if event_turn_id:
                        stream_state["turn_id"] = event_turn_id

                if event.type in {"thinking_delta", "thinking"}:
                    awaiting_user_input = False
                    thinking_chunk = str(event.data.get("content", ""))
                    thinking_metadata = {
                        key: event.data[key]
                        for key in ("source", "visibility", "is_raw_provider_reasoning", "provider_reasoning_type", "phase")
                        if key in event.data
                    }
                    turn_state.append_thinking(thinking_chunk, thinking_metadata)
                    for reasoning_event in reasoning_batcher.push(event):
                        await self._send_event(reasoning_event)
                        await _persist_partial_turn()
                    continue

                # Reasoning must never be reordered across text, tool, progress,
                # error, or terminal boundaries.
                await _flush_pending_reasoning()

                if event.type in {"item.started", "agent_message.delta", "item.completed"}:
                    awaiting_user_input = False
                    _project_agent_message_event(turn_state, event.type, event.data)
                elif event.type == "image_chunk":
                    image_data = str(event.data.get("image_data") or "").strip()
                    media_type = str(event.data.get("media_type") or "image/png").strip() or "image/png"
                    if image_data:
                        artifact_id = self.artifact_store.save(
                            image_data,
                            source="generated_image",
                            type="image",
                            preview_lines=1,
                            conversation_id=conversation.id,
                            workspace_root=str(run_workspace_root or ""),
                        )
                        artifact = {
                            "artifact_id": artifact_id,
                            "artifactId": artifact_id,
                            "kind": "image",
                            "summary": "Generated image",
                            "bytes": len(image_data),
                            "media_type": media_type,
                            "mediaType": media_type,
                            "url": f"data:{media_type};base64,{image_data}",
                        }
                        assistant_artifacts.append(artifact)
                        await self._send_ws_payload(
                            {
                                "type": "artifact.preview",
                                "conversation_id": conv_id,
                                "message_id": assistant_message_id,
                                "artifact_id": artifact_id,
                                "kind": "image",
                                "summary": "Generated image",
                                "bytes": len(image_data),
                                "media_type": media_type,
                                "url": f"data:{media_type};base64,{image_data}",
                            },
                            log_context="artifact.preview",
                        )
                    # The provider image has been converted into a typed
                    # artifact event.  Do not also forward it as answer text.
                    continue
                elif event.type == "tool_call":
                    awaiting_user_input = False
                    record = turn_state.record_tool_call(event.data)
                    if record is not None:
                        tool_id = str(record.get("id") or "")
                        upsert_pending_tool_call(
                            stream_state,
                            tool_id,
                            record,
                        )
                elif event.type == "tool_output_delta":
                    # Preserve incremental tool output so restored transcripts keep command previews.
                    turn_state.record_tool_output_delta(event.data)
                elif event.type == "tool_result":
                    awaiting_user_input = False
                    tool_id = str(event.data.get("id") or "").strip()
                    if tool_id:
                        turn_state.record_tool_result(event.data)
                    await _maybe_emit_source_citation(event.data)
                elif event.type == "agent.progress":
                    turn_state.record_progress(event.data)
                elif event.type == "agent.item":
                    turn_state.record_process_item(event.data)
                elif event.type == "agent.run.completed":
                    # Buffer the durable completion so lifecycle, transcript,
                    # and DONE share one immutable terminal status.
                    query_run_completed_payload = dict(event.data)
                elif event.type == "done":
                    query_done_payload = dict(event.data)
                    usage_payload = turn_state.record_done(event.data)
                    raw_terminal_status = str(event.data.get("status") or "").strip().lower()
                    if raw_terminal_status in {"completed", "partial", "failed", "cancelled", "interrupted"}:
                        query_terminal_status = raw_terminal_status
                    query_terminal_reason = str(event.data.get("reason") or "").strip()
                    provider_raw = event.data.get("providerRaw")
                    request_summary = provider_raw.get("request_summary") if isinstance(provider_raw, dict) else None
                    usage_provider = str(
                        (request_summary.get("wire_api") if isinstance(request_summary, dict) else "")
                        or (provider_raw.get("provider") if isinstance(provider_raw, dict) else "")
                        or run_provider
                    )
                    tracker.record_usage(
                        input_tokens=usage_payload.get("input_tokens", 0),
                        output_tokens=usage_payload.get("output_tokens", 0),
                        cache_creation_input_tokens=usage_payload.get("cache_creation_input_tokens", 0),
                        cache_read_input_tokens=usage_payload.get("cache_read_input_tokens", 0),
                        reasoning_output_tokens=usage_payload.get("reasoning_output_tokens", 0),
                        elapsed_sec=time.monotonic() - start_time,
                        model_id=getattr(run_llm, "_model", None) or getattr(getattr(run_llm, "_settings", None), "model", None),
                        provider=usage_provider,
                        session_id=self.session_id,
                        input_includes_cache_read=bool(
                            usage_payload.get("input_includes_cache_read", True)
                        ),
                    )
                    await _emit_no_final_reply_summary_if_needed()
                elif event.type == "error":
                    run_failed_message = turn_state.record_error(event.data)
                    # Emit rate_limit event if this is a rate-limit error
                    provider_error_type = str(event.data.get("provider_error_type") or "")
                    if provider_error_type in ("rate_limit", "busy"):
                        await self._send_event(AgentEvent.rate_limit(
                            provider=str(event.data.get("provider_error_type") or ""),
                            error_type=provider_error_type,
                            message=str(event.data.get("message") or ""),
                            recoverable=bool(event.data.get("recoverable", True)),
                        ))
                elif event.type in {"approval_request", "ask_user"}:
                    awaiting_user_input = True

                if event.type in {
                    "tool_call",
                    "tool_result",
                    "agent.item",
                    "item.completed",
                    "error",
                    "approval_request",
                    "ask_user",
                }:
                    await _persist_partial_turn(force=True)
                elif event.type in {"agent_message.delta", "tool_output_delta"}:
                    await _persist_partial_turn()

                event.data.setdefault("conversation_id", conversation.id)
                if event.type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                    event.data.setdefault("message_id", assistant_message_id)
                if event.type in {"agent.run.completed", "done"}:
                    continue
                await self._send_event(event)
            await _flush_pending_reasoning()
        except asyncio.CancelledError:
            run_interrupted = True
            interrupted_conversations = getattr(self, "_interrupted_conversation_ids", None)
            if isinstance(interrupted_conversations, set):
                interrupted_conversations.add(conversation.id)
            else:
                self._interrupted = True
            run_cancel_event.set()
            await _cancel_child_subagents_for_run("parent_cancelled")
        except Exception as exc:
            run_failed_message = f"Chat run failed: {exc}"
            await _cancel_child_subagents_for_run("parent_failed")
            await self._send_event(
                AgentEvent(
                    type="error",
                    data={
                        "message": run_failed_message,
                        "recoverable": False,
                        "error_type": "runtime",
                        "conversation_id": conversation.id,
                        "message_id": assistant_message_id,
                    },
                )
            )
        finally:
            # Clear streaming metadata
            getattr(self, "_conversation_streams", {}).pop(conversation.id, None)

            # Determine terminal status BEFORE using it (was previously
            # referenced before assignment, causing UnboundLocalError when
            # the finally block ran after an exception path).
            terminal_status = (
                "cancelled" if run_interrupted
                else "cancelled" if query_terminal_status == "interrupted"
                else query_terminal_status
                if query_terminal_status
                else "failed"
                if run_failed_message
                else "completed"
            )

            turn_snapshot = turn_state.finalize(terminal_status=terminal_status)
            assistant_blocks = turn_snapshot.blocks
            assistant_citations = turn_snapshot.citations
            if not usage_payload:
                usage_payload = turn_snapshot.usage
            assistant_content = turn_snapshot.content

            assistant_tool_calls = turn_snapshot.tool_calls
            if not assistant_content.strip() and assistant_tool_calls:
                if terminal_status == "completed":
                    terminal_status = "partial"
                if not run_failed_message:
                    run_failed_message = "The model ended without producing a final response."

            # Terminal delivery is the authoritative outcome of the run. The
            # transcript, summary and context snapshot below are projections;
            # a full disk, lock, or serialization failure must not strand the
            # client in a streaming state after the model has already stopped.
            run_manager = getattr(self, "_run_manager", None)
            mark_terminal_status = getattr(run_manager, "mark_terminal_status", None)
            if callable(mark_terminal_status):
                mark_terminal_status(conversation.id, terminal_status)
            if query_run_completed_payload and not _terminal_delivery_complete():
                run_completed_payload = dict(query_run_completed_payload)
                run_completed_payload["status"] = terminal_status
                run_completed_payload["conversation_id"] = conversation.id
                run_completed_payload["message_id"] = assistant_message_id
                if query_terminal_reason:
                    run_completed_payload["terminal_reason"] = query_terminal_reason
                generic_summary = str(run_completed_payload.get("summary") or "").strip().lower()
                if terminal_status != "completed" and generic_summary in {"", "completed", "run completed"}:
                    run_completed_payload["summary"] = {
                        "partial": "Run partially completed",
                        "cancelled": "Run cancelled",
                        "failed": "Run failed",
                    }.get(terminal_status, "Run completed")
                await self._send_event(
                    AgentEvent(type="agent.run.completed", data=run_completed_payload)
                )
            conversation_summary_payload: dict[str, Any] | None = None
            if assistant_content or assistant_blocks or assistant_tool_calls or assistant_artifacts:
                completed_at = _now_ms()
                assistant_message: dict[str, Any] = {
                    "id": assistant_message_id,
                    "role": "assistant",
                    "content": assistant_content,
                    "timestamp": datetime.fromtimestamp(completed_at / 1000, UTC).isoformat(),
                    "completed_at": completed_at,
                    "terminal_status": terminal_status,
                    "termination_reason": query_terminal_reason,
                    "usage": usage_payload or {},
                }
                if run_failed_message:
                    assistant_message["failure_message"] = run_failed_message
                if assistant_tool_calls:
                    assistant_message["tool_calls"] = assistant_tool_calls
                    reply_attachments = _reply_attachments_from_tool_calls(assistant_tool_calls)
                    if reply_attachments:
                        assistant_message["reply_attachments"] = reply_attachments
                if assistant_blocks:
                    assistant_message["blocks"] = assistant_blocks
                if assistant_artifacts:
                    assistant_message["artifacts"] = assistant_artifacts
                if assistant_citations:
                    assistant_message["citations"] = assistant_citations

                try:
                    upsert = getattr(self.conversation_repo, "upsert_transcript_message", None)
                    if callable(upsert):
                        await asyncio.to_thread(upsert, conversation.id, assistant_message)
                    else:
                        await asyncio.to_thread(
                            self.conversation_repo.append_transcript_message,
                            conversation.id,
                            assistant_message,
                        )
                except Exception:
                    logger.exception(
                        "Failed to persist terminal assistant transcript projection "
                        "for conversation %s",
                        conversation.id,
                    )

                if assistant_content:
                    new_summary = build_conversation_summary(
                        user_message=user_message,
                        attachments=normalized_attachments,
                        assistant_content=assistant_content,
                        compaction_summary=conversation.compaction_summary or "",
                    )
                    new_local_facts = merge_conversation_facts(
                        getattr(conversation, "local_facts", []),
                        extract_turn_facts(
                            conversation_id=conversation.id,
                            user_message=user_message,
                            attachments=normalized_attachments,
                            assistant_content=assistant_content,
                        ),
                    )
                    try:
                        await asyncio.to_thread(
                            self.conversation_repo.update_summary,
                            conversation.id,
                            new_summary,
                        )
                        updated_conversation = await asyncio.to_thread(
                            self.conversation_repo.update_facts,
                            conversation.id,
                            local_facts=new_local_facts,
                        ) or await asyncio.to_thread(
                            self.conversation_repo.get_conversation,
                            conversation.id,
                        )
                        conversation_summary_payload = {
                            "type": "conversation.summary.updated",
                            "conversation_id": conversation.id,
                            "summary": new_summary,
                            "title": getattr(updated_conversation, "title", conversation.title),
                            "updated_at": getattr(updated_conversation, "updated_at", conversation.updated_at),
                        }
                    except Exception:
                        logger.exception(
                            "Failed to persist conversation summary/facts projection "
                            "for conversation %s",
                            conversation.id,
                        )

            try:
                await self._flush_ui_agent_state_now(conversation.id)
            except Exception:
                logger.exception(
                    "Failed to flush terminal UI-agent-state projection for conversation %s",
                    conversation.id,
                )
            try:
                saved_snapshot = run_context_builder.export_snapshot()
                llm_stateful_snapshot = _export_llm_stateful_continuation_snapshot(run_llm)
                if llm_stateful_snapshot:
                    saved_snapshot[LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY] = llm_stateful_snapshot
                else:
                    saved_snapshot.pop(LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY, None)
                latest_conversation = await asyncio.to_thread(
                    self.conversation_repo.get_conversation,
                    conversation.id,
                )
                _merge_ui_agent_state_into_snapshot(
                    saved_snapshot,
                    getattr(latest_conversation, "context_snapshot", None),
                )
                await asyncio.to_thread(
                    self.conversation_repo.save_context_snapshot,
                    conversation.id,
                    saved_snapshot,
                )
                if conversation.id == self.active_conversation_id:
                    self._load_active_conversation_snapshot(conversation.id, saved_snapshot)
            except Exception:
                logger.exception(
                    "Failed to persist terminal context snapshot projection for conversation %s",
                    conversation.id,
                )
            # `done` is the observable commit boundary. Everything a client can
            # immediately query after it must already be durable. Each
            # projection above isolates its own failure, so delivery remains
            # unconditional even when one persistence step fails.
            await _send_done_once(status=terminal_status, reason=query_terminal_reason)
            mark_delivery_complete = getattr(run_manager, "mark_delivery_complete", None)
            if callable(mark_delivery_complete):
                mark_delivery_complete(conversation.id)
            await self._send_event(AgentEvent.session_state_changed(
                state="idle",
                conversation_id=conversation.id,
                reason=("completed" if terminal_status == "completed"
                        else terminal_status),
            ))
            if conversation_summary_payload is not None:
                await self._send_ws_payload(
                    conversation_summary_payload,
                    log_context="conversation.summary.updated",
                )
