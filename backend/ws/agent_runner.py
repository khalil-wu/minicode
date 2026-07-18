"""
Agent run logic mixin extracted from ws/handler.py.

SessionAgentRunnerMixin provides the _run_agent method which orchestrates
LLM refresh, query engine submission, cost tracking, transcript persistence,
and conversation summary/facts updates.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QuerySubmission
from backend.agent.runtime_spans import runtime_span
from backend.agent.runtime import default_runtime
from backend.agent.tool_projection import sanitize_internal_tool_names_for_user_text, user_facing_tool_name
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
    remove_pending_tool_call,
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
_PROGRESS_STAGES = {"status", "planning", "verification", "final"}
_PROGRESS_STATUSES = {"running", "completed", "failed", "info"}
LLM_STATEFUL_CONTINUATION_SNAPSHOT_KEY = "llm_stateful_continuation"
_COLLABORATION_TOOL_NAMES = {
    "task",
    "task_status",
    "task_stop",
    "workflow",
    "send_message",
    "message_list",
    "task_create",
    "task_list",
    "task_get",
    "task_update",
    "task_output",
    "team_create",
    "team_list",
    "team_delete",
}
_COLLABORATION_FINAL_STALL_RE = re.compile(
    r"(?:"
    r"let me|i(?:'ll| will| am going to| need to| should)|now i|"
    r"我(?:会|将|要|先|来|准备|需要|打算|用|直接|再)|让我|现在我|接下来我|稍等"
    r").{0,180}"
    r"(?:final report|report|summary|synthesi[sz]e|collect|poll|status|workflow|subagents?|"
    r"answer\s+based\s+on\s+what\s+i(?:'ve| have)?\s+found|give\s+the\s+user.{0,80}answer|"
    r"报告|总结|结论|结果|取出|读取|查看|检查|工作流|子代理|多\s*agent)",
    re.IGNORECASE | re.DOTALL,
)

logger = logging.getLogger(__name__)
_PUBLIC_RESULT_MARKER_RE = re.compile(
    r"^(?:#+\s*)?(?:findings|summary|result|results|结论|结果|问题|建议|发现|要点)\b",
    re.IGNORECASE,
)
_TURN_MESSAGE_SCOPED_EVENT_TYPES = {
    "text_chunk",
    "text_replace",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "command_output_chunk",
    "tool_result",
    "agent.loop.started",
    "agent.loop.completed",
    "agent.run.started",
    "agent.run.updated",
    "agent.run.completed",
    "agent.phase.updated",
    "agent.item",
    "agent.progress",
    "runtime.span",
    "tool_use_summary",
    "task.update",
    "approval_request",
    "approval.file_diff",
    "ask_user",
    "verification.started",
    "verification.result",
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


def _contains_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text)


def _failed_tool_call_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if str(record.get("status") or "").strip().lower() in _FAILED_TOOL_STATUSES
    ]


def _has_metadata_value(value: Any) -> bool:
    return value is not None and value != ""


def _text_chunk_metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: data[key]
        for key in (
            "source",
            "visibility",
            "role",
            "phase",
            "segmentId",
            "iterationIndex",
            "streamAttempt",
            "sealReason",
            "sealed",
            "promoteAllUnsealedNarration",
            "providerRaw",
            "finishReason",
        )
        if _has_metadata_value(data.get(key))
    }
    nested = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if isinstance(nested, dict):
        for source_key, target_key in (
            ("visibility", "visibility"),
            ("role", "role"),
            ("phase", "phase"),
            ("segmentId", "segmentId"),
            ("segment_id", "segmentId"),
            ("iterationIndex", "iterationIndex"),
            ("iteration_index", "iterationIndex"),
            ("streamAttempt", "streamAttempt"),
            ("stream_attempt", "streamAttempt"),
            ("sealReason", "sealReason"),
            ("seal_reason", "sealReason"),
            ("sealed", "sealed"),
            ("promoteAllUnsealedNarration", "promoteAllUnsealedNarration"),
            ("promote_all_unsealed_narration", "promoteAllUnsealedNarration"),
        ):
            if _has_metadata_value(nested.get(source_key)):
                metadata[target_key] = nested[source_key]
        provider_raw = nested.get("providerRaw")
        if isinstance(provider_raw, dict) and provider_raw:
            metadata["providerRaw"] = dict(provider_raw)
        finish_reason = nested.get("finishReason")
        if isinstance(finish_reason, str) and finish_reason:
            metadata["finishReason"] = finish_reason
    return metadata


def _first_nonempty_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _workflow_span_event_name(event_type: str) -> str:
    if not event_type.startswith("workflow_"):
        return ""
    return event_type.replace("_", ".")


def _workflow_span_status(event_type: str, event: dict[str, Any]) -> str:
    if event_type == "workflow_completed":
        return "completed"
    if event_type == "workflow_cancelled" or str(event.get("launch_error") or "").strip():
        return "failed"
    return "running"


def _workflow_span_summary(event_type: str, event: dict[str, Any]) -> str:
    if event_type == "workflow_started":
        name = _first_nonempty_text(event.get("name"), event.get("workflow_id"), "workflow")
        mode = _first_nonempty_text(event.get("mode"))
        steps = event.get("steps") if isinstance(event.get("steps"), list) else []
        suffix = f" ({mode}, {len(steps)} step(s))" if mode or steps else ""
        return f"Workflow started: {name}{suffix}"
    if event_type == "workflow_nodes_resumed":
        tasks = event.get("tasks") if isinstance(event.get("tasks"), list) else []
        error = _first_nonempty_text(event.get("launch_error"))
        if error:
            return f"Workflow resume needs attention: {error}"
        return f"Workflow resumed {len(tasks)} node(s)"
    if event_type == "workflow_nodes_unblocked":
        tasks = event.get("tasks") if isinstance(event.get("tasks"), list) else []
        error = _first_nonempty_text(event.get("launch_error"))
        if error:
            return f"Workflow launch needs attention: {error}"
        return f"Workflow unblocked {len(tasks)} node(s)"
    if event_type == "workflow_completed":
        return "Workflow completed"
    if event_type == "workflow_cancelled":
        return "Workflow cancelled"
    return event_type.replace("_", " ")


def _runtime_span_for_agent_event(event_type: str, data: dict[str, Any]) -> AgentEvent | None:
    """Derive semantic runtime spans from legacy collaboration events."""
    if event_type == "runtime.span":
        return None

    if event_type in {"subagent.start", "subagent.progress", "subagent.done"}:
        subagent_id = str(data.get("subagent_id") or "").strip()
        if not subagent_id:
            return None
        role = _first_nonempty_text(data.get("role"), data.get("agent_type"), "subagent")
        span_id = f"subagent:{subagent_id}"
        common: dict[str, Any] = {
            "span_id": span_id,
            "phase": "subagent",
            "label": role,
            "agent_id": subagent_id,
            "waiting_on": _first_nonempty_text(data.get("waiting_on"), data.get("tool_name")),
            "ui_visible": False,
            "requires_attention": bool(data.get("requires_attention") or data.get("error") or data.get("timed_out")),
        }
        extra_data = {
            key: data[key]
            for key in (
                "parent_id",
                "parent_run_id",
                "workflow_id",
                "workflow_name",
                "workflow_mode",
                "node_id",
                "task_id",
                "iteration",
                "iterations",
                "max_iterations",
                "tool_call_count",
                "blocks_final_reply",
            )
            if data.get(key) is not None
        }
        if event_type == "subagent.start":
            return runtime_span(
                "subagent.started",
                status="running",
                summary=_first_nonempty_text(data.get("current_activity"), data.get("prompt"), f"{role} started"),
                started_at=data.get("last_progress_at") if isinstance(data.get("last_progress_at"), int) else None,
                data=extra_data,
                **common,
            )
        if event_type == "subagent.progress":
            return runtime_span(
                "subagent.progress",
                status="running",
                summary=_first_nonempty_text(data.get("current_activity"), data.get("detail"), f"{role} running"),
                data=extra_data,
                **common,
            )
        raw_status = str(data.get("status") or "").strip().lower()
        failed = bool(data.get("error") or data.get("timed_out"))
        span_status = (
            "cancelled" if raw_status in {"cancelled", "interrupted"}
            else "partial" if raw_status == "partial"
            else "failed" if failed or raw_status in {"error", "failed"}
            else "completed"
        )
        return runtime_span(
            "subagent.completed",
            status=span_status,
            summary=_first_nonempty_text(data.get("error"), data.get("summary"), f"{role} completed"),
            duration_ms=data.get("duration_ms") if isinstance(data.get("duration_ms"), int) else None,
            data=extra_data,
            **common,
        )

    if event_type == "subagent.event":
        nested = data.get("event")
        if not isinstance(nested, dict):
            return None
        nested_type = str(nested.get("type") or "").strip()
        span_event = _workflow_span_event_name(nested_type)
        if not span_event:
            return None
        workflow_id = _first_nonempty_text(nested.get("workflow_id"), data.get("subagent_id"))
        if not workflow_id:
            return None
        status = _workflow_span_status(nested_type, nested)
        steps = nested.get("steps") if isinstance(nested.get("steps"), list) else []
        tasks = nested.get("tasks") if isinstance(nested.get("tasks"), list) else []
        span_data = {
            key: nested[key]
            for key in ("workflow_id", "name", "mode", "launched", "launch_summary", "launch_error")
            if nested.get(key) is not None
        }
        if steps:
            span_data["step_count"] = len(steps)
        if tasks:
            span_data["task_count"] = len(tasks)
        return runtime_span(
            span_event,
            span_id=f"workflow:{workflow_id}",
            phase="workflow",
            status=status,
            label=_first_nonempty_text(nested.get("name"), workflow_id),
            summary=_workflow_span_summary(nested_type, nested),
            agent_id=workflow_id,
            ui_visible=False,
            requires_attention=status == "failed",
            data=span_data,
        )

    if event_type == "inspector.update":
        if str(data.get("target_kind") or "").strip() != "cache":
            return None
        payload = data.get("payload")
        if not isinstance(payload, dict) or payload.get("kind") != "cache_metric":
            return None
        cache_layer = _first_nonempty_text(payload.get("cache_layer"), "cache")
        signature = _first_nonempty_text(payload.get("args_signature"), data.get("target_id"), payload.get("observed_at"))
        hit = bool(payload.get("hit"))
        stale = bool(payload.get("stale"))
        evicted = bool(payload.get("evicted"))
        status = "completed" if hit else "info"
        if stale or evicted:
            status = "failed"
        summary_state = "hit" if hit else "miss"
        if stale:
            summary_state = "stale"
        elif evicted:
            summary_state = "evicted"
        return runtime_span(
            f"cache.lookup.{summary_state}",
            span_id=f"cache:{cache_layer}:{signature}",
            run_id=_first_nonempty_text(payload.get("run_id")),
            turn_id=_first_nonempty_text(payload.get("turn_id")),
            phase="cache",
            status=status,
            label=cache_layer,
            summary=f"Cache {summary_state}: {cache_layer}",
            tool_name=_first_nonempty_text(payload.get("tool_name")),
            ui_visible=False,
            requires_attention=stale or evicted,
            data=dict(payload),
        )

    return None


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
        cache.clear()


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
        if re.match(r"^running\s+[a-z0-9_.:-]+$", value, re.IGNORECASE):
            continue
        if re.match(r"^tool started\s*:", value, re.IGNORECASE):
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
            **({"blocksFinalReply": bool(data.get("blocks_final_reply"))} if data.get("blocks_final_reply") is not None else {}),
            **({"lastProgressAt": data.get("last_progress_at")} if isinstance(data.get("last_progress_at"), int) else {}),
        })[-20:]
        return state

    if event_type == "subagent.done":
        subagent_id = str(data.get("subagent_id") or "").strip()
        if not subagent_id:
            return None
        raw_status = str(data.get("status") or "completed").strip().lower()
        status = (
            "partial" if raw_status == "partial"
            else "cancelled" if raw_status in {"cancelled", "interrupted"}
            else "error" if data.get("error") or raw_status in {"error", "failed"}
            else "done"
        )
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        record = data.get("record") if isinstance(data.get("record"), dict) else {}
        result_content = str(result.get("content") or data.get("summary") or "").strip()
        result_error = str(result.get("error") or data.get("error") or "").strip()
        state["subagents"] = _upsert_by_id(list(state.get("subagents") or []), {
            "id": subagent_id,
            "role": str(record.get("agent_type") or record.get("role") or "subagent"),
            "status": status if status in _SUBAGENT_STATUSES else "done",
            "summary": str(data.get("error") or data.get("summary") or ""),
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
            **({"displayScope": str(data.get("display_scope"))} if data.get("display_scope") is not None else {}),
            **({"panelHint": str(data.get("panel_hint"))} if data.get("panel_hint") is not None else {}),
            **({"requiresAttention": bool(data.get("requires_attention"))} if data.get("requires_attention") is not None else {}),
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


def _tool_record_detail(record: dict[str, Any], *, fallback: str) -> str:
    detail = (
        str(record.get("summary") or "").strip()
        or str(record.get("displaySummary") or "").strip()
        or str(record.get("contentPreview") or "").strip()
        or str(record.get("outputPreview") or "").strip()
        or str(record.get("inputSummary") or "").strip()
        or fallback
    )
    if len(detail) > 700:
        detail = detail[:700].rstrip() + "..."
    return detail


def _has_collaboration_tool_records(records: list[dict[str, Any]]) -> bool:
    for record in records:
        name = str(record.get("name") or "").strip().lower()
        if name in _COLLABORATION_TOOL_NAMES:
            return True
        if name.startswith(("task_", "team_")):
            return True
    return False


def _is_low_value_collaboration_final_reply(reply: str, records: list[dict[str, Any]]) -> bool:
    text = " ".join(str(reply or "").split())
    if not text:
        return False
    if not _has_collaboration_tool_records(records):
        return False
    if len(text) > 600:
        return False
    if any(_PUBLIC_RESULT_MARKER_RE.search(line.strip()) for line in str(reply or "").splitlines()):
        return False
    return bool(_COLLABORATION_FINAL_STALL_RE.search(text))


def _ask_user_question_from_record(record: dict[str, Any]) -> str:
    args = record.get("args")
    if isinstance(args, dict):
        question = str(args.get("question") or args.get("prompt") or "").strip()
        if question:
            return question
    request = record.get("request")
    if isinstance(request, dict):
        question = str(request.get("question") or request.get("prompt") or "").strip()
        if question:
            return question
    return str(record.get("inputSummary") or record.get("displaySummary") or "").strip()


def _format_failed_ask_user_reply(
    records: list[dict[str, Any]],
    *,
    user_message: str,
) -> str:
    failed = _failed_tool_call_records(records)
    if not failed:
        return ""
    if any(str(record.get("name") or "").strip().lower() != "ask_user" for record in failed):
        return ""
    question = next(
        (value for value in (_ask_user_question_from_record(record) for record in failed) if value),
        "",
    )
    cjk = _contains_cjk(user_message) or _contains_cjk(question)
    if question:
        return f"需要你确认一下：{question}" if cjk else f"I need one detail before continuing: {question}"
    return "需要你补充一个必要信息后我才能继续。" if cjk else "I need one detail before I can continue."


def _format_failed_tool_only_reply(
    records: list[dict[str, Any]],
    *,
    user_message: str,
    failure_message: str = "",
) -> str:
    failed = _failed_tool_call_records(records)
    if not failed:
        return ""
    ask_user_reply = _format_failed_ask_user_reply(records, user_message=user_message)
    if ask_user_reply:
        return ask_user_reply
    if _contains_cjk(user_message):
        intro = (
            "\u5de5\u5177\u8c03\u7528\u5931\u8d25\uff0c\u800c\u4e14\u6a21\u578b\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u56de\u590d\u3002"
            "\u8fd9\u8f6e\u4e0d\u80fd\u5f53\u4f5c\u6210\u529f\u5b8c\u6210\uff0c\u5931\u8d25\u70b9\u5982\u4e0b\uff1a"
        )
        failure_label = "\u8fd0\u884c\u9519\u8bef"
        no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u5931\u8d25\u7ec6\u8282\u3002"
    else:
        intro = (
            "Some actions failed and the final summary could not be generated. "
            "Here is what failed:"
        )
        failure_label = "Run error"
        no_details = "The tool did not return usable failure details."

    parts = [intro]
    failure_detail = failure_message.strip()
    if failure_detail:
        parts.append(f"{failure_label}: {failure_detail}")
    for index, record in enumerate(failed[-3:], start=1):
        cjk = _contains_cjk(user_message)
        name = user_facing_tool_name(str(record.get("name") or "tool"), cjk=cjk)
        status = str(record.get("status") or "failed")
        detail = sanitize_internal_tool_names_for_user_text(_tool_record_detail(record, fallback=no_details), cjk=cjk)
        parts.append(f"{index}. {name} [{status}]\n{detail}")
    return "\n\n".join(parts)


def _format_tool_activity_without_final_reply(
    records: list[dict[str, Any]],
    *,
    user_message: str,
    failure_message: str = "",
) -> str:
    if not records:
        return ""
    failed_reply = _format_failed_tool_only_reply(
        records,
        user_message=user_message,
        failure_message=failure_message,
    )
    if failed_reply:
        return failed_reply

    if _contains_cjk(user_message):
        intro = (
            "\u6a21\u578b\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u603b\u7ed3\uff0c\u6211\u5148\u628a\u5df2\u7ecf\u62ff\u5230\u7684\u7ed3\u679c\u653e\u5728\u8fd9\u91cc\uff1a"
        )
        failure_label = "\u8fd0\u884c\u9519\u8bef"
        no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u6458\u8981\u3002"
    else:
        intro = (
            "The model did not generate a final summary, so here are the results already returned:"
        )
        failure_label = "Run error"
        no_details = "The tool did not return a usable summary."

    parts = [intro]
    failure_detail = failure_message.strip()
    if failure_detail:
        parts.append(f"{failure_label}: {failure_detail}")
    for index, record in enumerate(records[-3:], start=1):
        cjk = _contains_cjk(user_message)
        name = user_facing_tool_name(str(record.get("name") or "tool"), cjk=cjk)
        status = str(record.get("status") or "completed")
        detail = sanitize_internal_tool_names_for_user_text(_tool_record_detail(record, fallback=no_details), cjk=cjk)
        parts.append(f"{index}. {name} [{status}]\n{detail}")
    return "\n\n".join(parts)


class SessionAgentRunnerMixin:
    """Agent run logic for WebSocketSession.

    Depends on session attributes: ws, query_engine, conversation_repo,
    context_builder, permission_checker, permission_context, config,
    llm, artifact_store, tool_registry, skill_manager, vector_memory,
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
        # the client-created draft id so streamed events cannot attach to a
        # later local assistant draft if an old turn finishes late.
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

        run_context_builder = ContextBuilder(
            token_budget=run_config.token_budget,
            agent_settings=run_config.agent,
            skill_executor=getattr(self, "skill_executor", None),
            rag_pipeline=getattr(self, "rag_pipeline", None),
            memory_manager=getattr(self, "memory_manager", None),
            llm=run_llm,
            skill_manager=self.skill_manager,
            vector_memory=self.vector_memory,
        )
        run_context_builder.load_snapshot(run_context_snapshot)
        _import_llm_stateful_continuation_from_snapshot(run_llm, run_context_snapshot)
        normalized_attachments = list(attachments or [])
        run_metadata = dict(metadata or {})
        if previous_turn_aborted:
            run_metadata.setdefault("previous_turn_aborted", True)
        run_metadata.setdefault("assistant_message_id", assistant_message_id)
        run_metadata.setdefault("agent_runtime", default_runtime())
        run_workspace_root = self._workspace_root_for_conversation(conversation)
        run_workspace_context = self._workspace_context_for_conversation(conversation)
        run_metadata.setdefault("workspace_context", run_workspace_context)
        run_metadata.setdefault("conversation_id", conversation.id)
        run_metadata.setdefault("cost_session_id", self.session_id)
        run_metadata.setdefault("requires_explicit_workspace", True)
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
            run_permission_context.mode = "plan"

        run_metadata.setdefault("permission_mode_setter", _set_run_permission_mode)

        # 回填压缩摘要为持久备忘，保证模型轮次即使丢掉 snapshot 也能读到高层结论
        compaction_summary = (conversation.compaction_summary or "").strip()
        if compaction_summary:
            run_context_builder.set_compaction_summary_note(compaction_summary)

        self.conversation_repo.append_transcript_message(
            conversation.id,
            {
                "id": f"user_{uuid.uuid4().hex[:8]}",
                "role": "user",
                "content": user_message,
                "timestamp": datetime.now(UTC).isoformat(),
                "attachments": normalized_attachments,
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

        async def _stream_callback(line: str, stream: str = "stdout") -> None:
            await self._send_ws_payload(
                {
                    "type": "command_output_chunk",
                    "conversation_id": conv_id,
                    "message_id": assistant_message_id,
                    "turn_id": assistant_message_id,
                    "content": line,
                    "stream": stream if stream in {"stdout", "stderr"} else "stdout",
                },
                log_context="command_output_chunk",
            )

        async def _emit_derived_runtime_span(event_type: str, data: dict[str, Any]) -> None:
            span_event = _runtime_span_for_agent_event(event_type, data)
            if span_event is None:
                return
            span_payload = dict(span_event.data)
            span_payload.setdefault("conversation_id", conversation.id)
            span_payload.setdefault("message_id", assistant_message_id)
            span_payload.setdefault("turn_id", assistant_message_id)
            self._persist_ui_agent_state_event(conversation.id, span_event.type, span_payload)
            await self._send_event(AgentEvent(type=span_event.type, data=span_payload))

        async def _emit_runtime_event(event_type: str, data: dict[str, Any]) -> None:
            payload = dict(data)
            payload.setdefault("conversation_id", conversation.id)
            if event_type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                payload.setdefault("message_id", assistant_message_id)
                payload.setdefault("turn_id", assistant_message_id)
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
            elif event_type == "agent.loop.started":
                loop_id = str(payload.get("loop_id") or payload.get("item_id") or "").strip()
                if loop_id:
                    active_agent_loops[loop_id] = dict(payload)
            elif event_type == "agent.loop.completed":
                loop_id = str(payload.get("loop_id") or payload.get("item_id") or "").strip()
                if loop_id:
                    active_agent_loops.pop(loop_id, None)
            if event_type == "text_chunk" and payload.get("visibility") != "debug":
                content = str(payload.get("content", ""))
                metadata = _text_chunk_metadata(payload)
                if content:
                    turn_state.append_text(content, metadata)
                if payload.get("finalize"):
                    turn_state.finalize_text(metadata)
            await self._send_event(AgentEvent(type=event_type, data=payload))
            await _emit_derived_runtime_span(event_type, payload)

        assistant_artifacts: list[dict[str, Any]] = []
        usage_payload: dict[str, int] | None = None
        run_failed_message = ""
        done_event_sent = False
        query_terminal_status = ""
        query_terminal_reason = ""
        query_done_payload: dict[str, Any] = {}
        active_runtime_spans: dict[str, dict[str, Any]] = {}
        active_agent_loops: dict[str, dict[str, Any]] = {}
        assistant_message_id = str(stream_state.get("message_id") or assistant_message_id)

        def _now_ms() -> int:
            return int(time.time() * 1000)

        turn_state = AgentTurnState(now_ms=_now_ms)
        synthesized_no_final_reply = False

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
            nonlocal run_failed_message, synthesized_no_final_reply
            if synthesized_no_final_reply or run_failed_message:
                return
            tool_records = turn_state.tool_call_records()
            current_reply = turn_state.content().strip()
            if current_reply and not _is_low_value_collaboration_final_reply(current_reply, tool_records):
                return
            if not tool_records or _failed_tool_call_records(tool_records):
                return
            fallback_reply = _format_tool_activity_without_final_reply(
                tool_records,
                user_message=user_message,
                failure_message=run_failed_message,
            )
            if not fallback_reply:
                return
            if current_reply:
                turn_state.replace_text("")
            synthesized_no_final_reply = True
            fallback_metadata = {"source": "fallback", "visibility": "final", "phase": "final"}
            turn_state.append_text(fallback_reply, fallback_metadata)
            fallback_event = AgentEvent.text_chunk(fallback_reply, source="fallback", visibility="final", phase="final")
            fallback_event.data["conversation_id"] = conversation.id
            fallback_event.data["message_id"] = assistant_message_id
            await self._send_event(fallback_event)
            if not run_failed_message:
                run_failed_message = "操作已完成，但最终回复未能生成；请查看上方的工具结果。"
                turn_state.record_error({"message": run_failed_message})
                error_event = AgentEvent.error(
                    run_failed_message,
                    recoverable=True,
                    error_type="api",
                )
                error_event.data["conversation_id"] = conversation.id
                error_event.data["message_id"] = assistant_message_id
                await self._send_event(error_event)

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

        async def _send_done_once(status: str = "completed", reason: str = "") -> None:
            nonlocal done_event_sent
            if done_event_sent:
                return
            # Include accumulated usage so error/budget termination paths
            # (which don't emit their own done event from the loop) still
            # report correct token usage to the client.
            u = usage_payload or {}
            done_event = AgentEvent.done(
                status=status,
                reason=reason,
                input_tokens=int(u.get("input_tokens", 0) or 0),
                output_tokens=int(u.get("output_tokens", 0) or 0),
                cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
                cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
                reasoning_output_tokens=int(u.get("reasoning_output_tokens", 0) or 0),
            )
            for key, value in query_done_payload.items():
                if key not in {"conversation_id", "message_id", "status", "reason"}:
                    done_event.data[key] = value
            done_event.data["conversation_id"] = conversation.id
            done_event.data["message_id"] = assistant_message_id
            await self._send_event(done_event)
            done_event_sent = True

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
                        vector_memory=self.vector_memory,
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

                if event.type == "text_chunk":
                    if event.data.get("image_data"):
                        image_data = str(event.data.get("image_data") or "").strip()
                        media_type = str(event.data.get("media_type") or "image/png").strip() or "image/png"
                        if image_data:
                            artifact_id = self.artifact_store.save(
                                image_data,
                                source="generated_image",
                                type="image",
                                preview_lines=1,
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
                    if event.data.get("visibility") != "debug":
                        content = str(event.data.get("content", ""))
                        metadata = _text_chunk_metadata(event.data)
                        if content:
                            turn_state.append_text(content, metadata)
                        if event.data.get("finalize"):
                            turn_state.finalize_text(metadata)
                elif event.type == "text_replace":
                    turn_state.replace_text(
                        str(event.data.get("content", "")),
                        _text_chunk_metadata(event.data),
                    )
                elif event.type == "image_chunk":
                    image_data = str(event.data.get("image_data") or "").strip()
                    media_type = str(event.data.get("media_type") or "image/png").strip() or "image/png"
                    if image_data:
                        artifact_id = self.artifact_store.save(
                            image_data,
                            source="generated_image",
                            type="image",
                            preview_lines=1,
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
                elif event.type in {"thinking_delta", "thinking"}:
                    thinking_chunk = str(event.data.get("content", ""))
                    thinking_metadata = {
                        key: event.data[key]
                        for key in ("source", "visibility", "is_raw_provider_reasoning", "provider_reasoning_type")
                        if key in event.data
                    }
                    turn_state.append_thinking(thinking_chunk, thinking_metadata)
                elif event.type == "tool_call":
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
                    tool_id = str(event.data.get("id") or "").strip()
                    if tool_id:
                        remove_pending_tool_call(stream_state, tool_id)
                        turn_state.record_tool_result(event.data)
                    await _maybe_emit_source_citation(event.data)
                elif event.type == "agent.progress":
                    turn_state.record_progress(event.data)
                elif event.type == "agent.item":
                    turn_state.record_process_item(event.data)
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

                event.data.setdefault("conversation_id", conversation.id)
                if event.type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                    event.data.setdefault("message_id", assistant_message_id)
                if event.type == "done":
                    continue
                await self._send_event(event)
                await _emit_derived_runtime_span(event.type, event.data)
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
                        "recoverable": True,
                        "error_type": "api",
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
                else "failed" if run_failed_message
                else "cancelled" if query_terminal_status == "interrupted"
                else query_terminal_status or "completed"
            )

            # Close any lifecycle item whose normal terminal event was skipped
            # by a timeout, provider exception or interrupted stream.
            lifecycle_ended_at = _now_ms()
            for span_id, started in list(active_runtime_spans.items()):
                started_at = int(started.get("started_at") or lifecycle_ended_at)
                base_event = str(started.get("event") or "runtime").removesuffix(".started")
                closed = runtime_span(
                    f"{base_event}.{terminal_status}",
                    span_id=span_id,
                    run_id=str(started.get("run_id") or ""),
                    turn_id=str(started.get("turn_id") or assistant_message_id),
                    iteration_id=str(started.get("iteration_id") or ""),
                    phase=str(started.get("phase") or ""),
                    status=terminal_status,
                    label=str(started.get("label") or "runtime"),
                    summary=f"{str(started.get('label') or 'Runtime').capitalize()} {terminal_status}",
                    started_at=started_at,
                    ended_at=lifecycle_ended_at,
                    duration_ms=lifecycle_ended_at - started_at,
                    requires_attention=terminal_status != "completed",
                )
                closed.data["conversation_id"] = conversation.id
                closed.data["message_id"] = assistant_message_id
                await self._send_event(closed)
            active_runtime_spans.clear()
            for loop_id, started in list(active_agent_loops.items()):
                closed = AgentEvent.loop_completed(
                    loop_id=loop_id,
                    iteration_id=str(started.get("iteration_id") or loop_id),
                    status=terminal_status,
                    title="Stopped" if terminal_status != "completed" else "Processed",
                    summary=f"Agent loop {terminal_status}",
                    started_at=int(started.get("started_at") or lifecycle_ended_at),
                    completed_at=lifecycle_ended_at,
                )
                closed.data["conversation_id"] = conversation.id
                closed.data["message_id"] = assistant_message_id
                await self._send_event(closed)
            active_agent_loops.clear()

            # Note: session.state_changed(idle) is emitted AFTER any synthesized
            # fallback reply below, so the fallback text_chunk reaches the client
            # while it is still streaming (otherwise the client ends streaming on
            # idle and the fallback only appears after a refresh).
            if terminal_status == "completed":
                turn_state.finalize_text()
            turn_snapshot = turn_state.finalize(terminal_status=terminal_status)
            assistant_blocks = turn_snapshot.blocks
            assistant_citations = turn_snapshot.citations
            if not usage_payload:
                usage_payload = turn_snapshot.usage
            assistant_content = turn_snapshot.content

            assistant_tool_calls = turn_snapshot.tool_calls
            if (
                _is_low_value_collaboration_final_reply(assistant_content, assistant_tool_calls)
                and assistant_tool_calls
                and not _failed_tool_call_records(assistant_tool_calls)
                and _format_tool_activity_without_final_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                )
            ):
                turn_state.replace_text("")
                turn_snapshot = turn_state.finalize(terminal_status=terminal_status)
                assistant_blocks = turn_snapshot.blocks
                assistant_citations = turn_snapshot.citations
                assistant_content = turn_snapshot.content
                assistant_tool_calls = turn_snapshot.tool_calls
            synthesized_final_reply_to_emit = ""
            synthesized_final_error_to_emit = ""
            failed_tool_only_reply = ""
            if not assistant_content.strip():
                failed_tool_only_reply = _format_failed_tool_only_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                )
            if failed_tool_only_reply and not run_failed_message and terminal_status != "partial":
                run_failed_message = "Tool calls failed before the assistant produced a reply."
                terminal_status = "failed"
                turn_snapshot = turn_state.finalize(terminal_status=terminal_status)
                assistant_blocks = turn_snapshot.blocks
                assistant_citations = turn_snapshot.citations
                assistant_content = turn_snapshot.content
                assistant_tool_calls = turn_snapshot.tool_calls
                failed_tool_only_reply = _format_failed_tool_only_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                ) or failed_tool_only_reply
            elif failed_tool_only_reply and terminal_status == "partial":
                # Max-iteration/partial runs must retain the useful tool
                # evidence; do not relabel them as "tool calls failed".
                failed_tool_only_reply = _format_tool_activity_without_final_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                ) or failed_tool_only_reply
                synthesized_final_error_to_emit = run_failed_message
            if failed_tool_only_reply and not assistant_content.strip():
                assistant_content = failed_tool_only_reply
                synthesized_final_reply_to_emit = failed_tool_only_reply
                assistant_blocks = [
                    *assistant_blocks,
                    {
                        "type": "text",
                        "content": assistant_content,
                        "source": "fallback",
                        "visibility": "final",
                        "phase": "final",
                    },
                ]
            if not assistant_content.strip() and assistant_tool_calls:
                no_final_reply = _format_tool_activity_without_final_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                )
                if no_final_reply:
                    if not run_failed_message:
                        run_failed_message = "操作已完成，但最终回复未能生成；请查看上方的工具结果。"
                        terminal_status = "failed"
                        synthesized_final_error_to_emit = run_failed_message
                    assistant_content = no_final_reply
                    synthesized_final_reply_to_emit = no_final_reply
                    assistant_blocks = [
                        *assistant_blocks,
                        {
                            "type": "text",
                            "content": assistant_content,
                            "source": "fallback",
                            "visibility": "final",
                            "phase": "final",
                        },
                    ]
            if synthesized_final_reply_to_emit and not synthesized_no_final_reply:
                fallback_event = AgentEvent.text_chunk(
                    synthesized_final_reply_to_emit,
                    source="fallback",
                    visibility="final",
                    phase="final",
                )
                fallback_event.data["conversation_id"] = conversation.id
                fallback_event.data["message_id"] = assistant_message_id
                await self._send_event(fallback_event)
            if synthesized_final_error_to_emit:
                await self._send_event(
                    AgentEvent(
                        type="error",
                        data={
                            "message": synthesized_final_error_to_emit,
                            "recoverable": True,
                            "error_type": "tool_error",
                            "conversation_id": conversation.id,
                            "message_id": assistant_message_id,
                        },
                    )
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
                if assistant_blocks:
                    assistant_message["blocks"] = assistant_blocks
                if assistant_artifacts:
                    assistant_message["artifacts"] = assistant_artifacts
                if assistant_citations:
                    assistant_message["citations"] = assistant_citations

                self.conversation_repo.append_transcript_message(conversation.id, assistant_message)

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
                    self.conversation_repo.update_summary(conversation.id, new_summary)
                    updated_conversation = self.conversation_repo.update_facts(
                        conversation.id,
                        local_facts=new_local_facts,
                    ) or self.conversation_repo.get_conversation(conversation.id)
                    conversation_summary_payload = {
                        "type": "conversation.summary.updated",
                        "conversation_id": conversation.id,
                        "summary": new_summary,
                        "title": getattr(updated_conversation, "title", conversation.title),
                        "updated_at": getattr(updated_conversation, "updated_at", conversation.updated_at),
                    }

            await self._flush_ui_agent_state_now(conversation.id)
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
            self.conversation_repo.save_context_snapshot(conversation.id, saved_snapshot)
            if conversation.id == self.active_conversation_id:
                self._load_active_conversation_snapshot(conversation.id, saved_snapshot)
            run_manager = getattr(self, "_run_manager", None)
            mark_terminal_status = getattr(run_manager, "mark_terminal_status", None)
            if callable(mark_terminal_status):
                mark_terminal_status(conversation.id, terminal_status)
            await self._send_event(AgentEvent.session_state_changed(
                state="idle",
                conversation_id=conversation.id,
                reason=("completed" if terminal_status == "completed"
                        else terminal_status),
            ))
            mark_delivery_complete = getattr(run_manager, "mark_delivery_complete", None)
            if callable(mark_delivery_complete):
                mark_delivery_complete(conversation.id)
            await _send_done_once(status=terminal_status, reason=query_terminal_reason)
            if conversation_summary_payload is not None:
                await self._send_ws_payload(
                    conversation_summary_payload,
                    log_context="conversation.summary.updated",
                )
