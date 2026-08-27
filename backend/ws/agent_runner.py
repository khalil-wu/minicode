"""
Agent run logic mixin extracted from ws/handler.py.

SessionAgentRunnerMixin provides the _run_agent method which orchestrates
LLM refresh, query engine submission, cost tracking, transcript persistence,
and conversation summary/facts updates.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import logging
import os
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from datetime import UTC, datetime
from typing import Any

from backend.agent.attachment_policy import AttachmentUnavailableError
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import execution_journal_owner
from backend.agent.lifecycle_generation import LifecycleGenerationState
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import (
    AGENT_PROGRESS_STAGES,
    AGENT_PROGRESS_STATUSES,
    AgentEvent,
    UserCommand,
)
from backend.agent.provider_activity import (
    merge_provider_activity_detail,
    provider_activity_status_rank,
)
from backend.agent.query_engine import AgentSession, QuerySubmission
from backend.agent.runtime import default_runtime
from backend.agent.turn_state import AgentTurnState
from backend.artifact.store import MAX_CONTENT_LENGTH as MAX_ARTIFACT_CONTENT_CHARS
from backend.config import get_available_models, get_llm_provider, load_config
from backend.agent.conversation_query_guard import (
    ConversationQueryClaim,
    conversation_query_guards,
)
from backend.extensions.capability_source import ExtensionCapabilitySource
from backend.extensions.lifecycle_observer import lifecycle_observer_factory
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.llm.model_selection import (
    apply_model_thinking_level,
    clamp_model_thinking_level,
    config_with_model_budget,
    default_model_thinking_level,
    model_thinking_levels,
)
from backend.memory.pollution import pollution_sources_from_tool_calls
from backend.permissions.checker import PermissionChecker
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.utils import (
    build_conversation_summary,
)
from backend.ws.stream_state import (
    create_stream_state,
    upsert_pending_tool_call,
)
from backend.workspace.trust import is_workspace_trusted

UI_AGENT_STATE_SNAPSHOT_KEY = "ui_agent_state"
UI_AGENT_STATE_REVISION_KEY = "_ui_agent_state_revision"
_UI_AGENT_STATE_DEBOUNCE_SECONDS = 0.08
_PLAN_STEP_STATUSES = {"pending", "in_progress", "completed"}
_TODO_STATUSES = {"pending", "in_progress", "completed", "blocked"}
_SUBAGENT_STATUSES = {"pending", "running", "blocked", "done", "partial", "cancelled", "error"}
_PROGRESS_STAGES = AGENT_PROGRESS_STAGES
_PROGRESS_STATUSES = AGENT_PROGRESS_STATUSES
_UI_AGENT_STATE_EVENT_TYPES = {
    "turn.plan.updated",
    "task.update",
    "subagent.start",
    "subagent.progress",
    "subagent.done",
    "agent.progress",
}
logger = logging.getLogger(__name__)
_GENERATED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
_GENERATED_IMAGE_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}
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
    # MiniCode's turn-owned checklist and aggregate diff snapshots carry the
    # same turn/message owner as tool items. Keeping them in this set prevents
    # late events from another turn from being projected into the live bar.
    "turn.plan.updated",
    "turn.diff.updated",
    "runtime.span",
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


def _validated_generated_image(
    image_data: str,
    media_type: str,
) -> tuple[str, str, bytes]:
    """Validate one provider image before persistence or renderer projection."""

    normalized_media_type = str(media_type or "image/png").split(";", 1)[0].strip().lower()
    if normalized_media_type == "image/jpg":
        normalized_media_type = "image/jpeg"
    if normalized_media_type not in _GENERATED_IMAGE_MEDIA_TYPES:
        raise ValueError("unsupported generated-image media type")

    encoded = str(image_data or "").strip()
    if encoded.startswith("data:"):
        header, separator, encoded_body = encoded.partition(",")
        expected_header = f"data:{normalized_media_type};base64"
        if not separator or header.strip().lower() != expected_header:
            raise ValueError("generated-image data URL does not match its media type")
        encoded = encoded_body.strip()
    if not encoded:
        raise ValueError("generated-image payload is empty")
    if len(encoded) > MAX_ARTIFACT_CONTENT_CHARS:
        raise ValueError("generated-image payload exceeds the artifact size limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("generated-image payload is not valid base64") from exc
    if not decoded:
        raise ValueError("generated-image payload decodes to an empty file")

    if normalized_media_type == "image/webp":
        valid_magic = (
            len(decoded) >= 12
            and decoded.startswith(b"RIFF")
            and decoded[8:12] == b"WEBP"
        )
    else:
        valid_magic = any(
            decoded.startswith(signature)
            for signature in _GENERATED_IMAGE_MAGIC[normalized_media_type]
        )
    if not valid_magic:
        raise ValueError("generated-image bytes do not match their declared media type")
    return encoded, normalized_media_type, decoded


def _generated_image_projection(
    *,
    artifact_id: str,
    media_type: str,
    decoded_image: bytes,
    conversation_id: str,
    message_id: str,
    text_offset: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    image_format = media_type.removeprefix("image/").upper()
    summary = f"Generated {image_format} image"
    shared_artifact = {
        "kind": "image",
        "summary": summary,
        "bytes": len(decoded_image),
    }
    transcript_artifact = {
        "artifactId": artifact_id,
        **shared_artifact,
        "mediaType": media_type,
        # JavaScript slices strings by UTF-16 code units. Persist that exact
        # coordinate so a cold reload can put the image back between the
        # provider's introductory and completion text, including when the
        # prefix contains emoji or other astral characters.
        "textOffset": max(0, int(text_offset)),
    }
    wire_artifact = {
        "artifact_id": artifact_id,
        **shared_artifact,
        "media_type": media_type,
        "text_offset": max(0, int(text_offset)),
    }
    return transcript_artifact, {
        "type": "artifact.preview",
        "conversation_id": conversation_id,
        "message_id": message_id,
        **wire_artifact,
    }


def _finalize_generated_image_text_offsets(
    artifacts: list[dict[str, Any]],
    assistant_content: str,
) -> None:
    """Repair image anchors when live answer deltas were disabled."""

    # The Images adapter emits its artifact between the introductory sentence
    # and this fixed completion sentence. When live text streaming is disabled
    # the event-time offset is zero, so resolve the boundary at finalization.
    completion_anchors = (
        "图像已经为你生成好了。",
        "The image has been generated.",
    )
    for artifact in artifacts:
        if str(artifact.get("kind") or "").strip().lower() != "image":
            continue
        try:
            current_offset = int(
                artifact.get("textOffset") or artifact.get("text_offset") or 0
            )
        except (TypeError, ValueError):
            current_offset = 0
        if current_offset > 0:
            continue
        for anchor in completion_anchors:
            anchor_offset = assistant_content.rfind(anchor)
            if anchor_offset > 0:
                offset = _utf16_code_unit_length(assistant_content[:anchor_offset])
                if "textOffset" in artifact:
                    artifact["textOffset"] = offset
                if "text_offset" in artifact:
                    artifact["text_offset"] = offset
                break


def _utf16_code_unit_length(value: str) -> int:
    """Return the string offset used by JavaScript ``String.slice``."""

    return len(str(value or "").encode("utf-16-le")) // 2


def _generated_image_rejection_notice(
    *,
    conversation_id: str,
    message_id: str,
    media_type: str,
    encoded_characters: int,
    reason: str,
) -> AgentEvent:
    return AgentEvent(
        type="system_notice",
        data={
            "conversation_id": conversation_id,
            "message_id": message_id,
            "content": (
                "A generated image was not displayed because the "
                f"provider payload was invalid: {reason}."
            ),
            "data": {
                "kind": "generated_image_rejected",
                "media_type": media_type,
                "encoded_characters": encoded_characters,
            },
        },
    )


def _resolver_accepts_positional_arguments(resolver: Any, *args: Any) -> bool:
    """Whether a host resolver can accept this exact positional call shape.

    MiniCode keeps narrow one-argument seams for embedded hosts while the
    configuration resolvers accept an optional effective-settings argument.
    Inspecting the signature before invocation preserves both forms without
    catching a ``TypeError`` thrown *inside* the resolver itself.
    """

    try:
        inspect.signature(resolver).bind(*args)
    except (TypeError, ValueError):
        return False
    return True
_TOOL_TURN_SCOPED_EVENT_TYPES = {
    "tool_call",
    "tool_output_delta",
    "command_output_chunk",
    "tool_result",
    "runtime.span",
}


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
            path_key = os.path.normcase(os.path.normpath(path))
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
    "small_fast_model",
    "reasoning_effort",
    "responses_reasoning_summary",
    "max_tokens",
    "wire_api",
    "proxy_mode",
    "prompt_cache_retention",
    "reasoning_effort_levels",
    "context_window",
    "context_window_source",
    "context_window_verified",
    "max_context_window",
    "max_context_window_source",
    "max_context_window_verified",
    "max_output_tokens",
    "max_output_tokens_source",
    "max_output_tokens_verified",
    "default_reasoning_effort",
    "default_reasoning_summary",
    "seed",
    "default_headers",
    "auth_header",
    "image_model",
    "image_size",
    "image_quality",
)


def _llm_settings_identity(value: Any) -> tuple[tuple[str, str], ...]:
    """Stable subset of LLM settings that changes adapter wire behavior.

    The effective model is already a separate cache-key dimension. Keeping it
    out of this subset lets a session reuse the correctly configured adapter
    when the saved default model changes but the active run still uses the same
    explicit model override.
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
    model_runtime: Any | None = None,
) -> tuple[Any, ...]:
    return (
        str(provider or "").strip(),
        str(model or "").strip(),
        _llm_settings_identity(getattr(config, "llm", None)),
        (
            model_runtime.cache_identity(provider, model)
            if model_runtime is not None
            and callable(getattr(model_runtime, "cache_identity", None))
            else ()
        ),
    )


def _config_with_runtime_model_budget(
    config: Any,
    *,
    model_runtime: Any | None,
    provider: str,
    model: str,
) -> Any:
    return config_with_model_budget(
        config,
        model_runtime=model_runtime,
        provider=provider,
        model=model,
    )


def _schedule_session_llm_close(session: Any, adapter: Any) -> None:
    close = getattr(adapter, "aclose", None)
    if not callable(close):
        return
    close_tasks = getattr(session, "_llm_close_tasks", None)
    if not isinstance(close_tasks, set):
        close_tasks = set()
        setattr(session, "_llm_close_tasks", close_tasks)
    try:
        task = asyncio.create_task(close())
    except RuntimeError:
        return
    close_tasks.add(task)

    def _closed(done: asyncio.Task[Any]) -> None:
        close_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Failed to close retired LLM adapter", exc_info=True)

    task.add_done_callback(_closed)


def _release_session_llm_lease(
    session: Any,
    adapter_id: int,
    owner_task: asyncio.Task[Any],
) -> None:
    leases = getattr(session, "_llm_adapter_leases", None)
    if not isinstance(leases, dict):
        return
    owners = leases.get(adapter_id)
    if isinstance(owners, set):
        owners.discard(owner_task)
        if owners:
            return
    leases.pop(adapter_id, None)
    retired = getattr(session, "_retired_llm_adapters", None)
    adapter = retired.pop(adapter_id, None) if isinstance(retired, dict) else None
    if adapter is not None:
        _schedule_session_llm_close(session, adapter)


def _lease_session_llm_for_task(
    session: Any,
    adapter: Any,
    owner_task: asyncio.Task[Any] | None,
) -> None:
    """Keep an adapter alive until an explicitly owning task has ended.

    Foreground agent tasks normally call the current-task wrapper below.  A
    few session-owned maintenance jobs (memory consolidation in particular)
    run in a detached task but still use the session adapter.  They must hold
    the same lease, otherwise a provider/model refresh can retire and close
    the HTTP client while that background request is streaming.
    """

    if adapter is None or owner_task is None or owner_task.done():
        return
    leases = getattr(session, "_llm_adapter_leases", None)
    if not isinstance(leases, dict):
        leases = {}
        setattr(session, "_llm_adapter_leases", leases)
    adapter_id = id(adapter)
    owners = leases.setdefault(adapter_id, set())
    if owner_task in owners:
        return
    owners.add(owner_task)
    owner_task.add_done_callback(
        lambda done: _release_session_llm_lease(session, adapter_id, done)
    )


def _lease_session_llm_for_current_task(session: Any, adapter: Any) -> None:
    """Keep an adapter alive until the current Agent/command task ends."""

    _lease_session_llm_for_task(session, adapter, asyncio.current_task())


def _activatable_tool_names(tool_registry: Any) -> list[str]:
    """Return selectable MiniCode tools while preserving registry order."""

    result: list[str] = []
    seen: set[str] = set()
    for raw_name in tool_registry.list_tools():
        name = str(raw_name).strip()
        if not name or name in seen:
            continue
        spec = tool_registry.get_tool_spec(name)
        if spec.exposure == "hidden":
            continue
        seen.add(name)
        result.append(name)
    return result


def _model_thinking_levels(model: Any, adapter: Any) -> tuple[str, ...]:
    return model_thinking_levels(model, adapter)


def _clamp_thinking_level(requested: Any, available: tuple[str, ...]) -> str:
    return clamp_model_thinking_level(requested, available)


def _apply_thinking_level(adapter: Any, model: Any, requested: Any) -> str:
    """Apply the canonical clamped thinking level to the active adapter."""

    available = model_thinking_levels(model, adapter)
    requested_level = str(requested or "off").strip().lower()
    # MiniCode always clamps through getSupportedThinkingLevels(), including `off`.
    # A provider may explicitly map `off` to null, in which case MiniCode searches
    # upward for the first supported level instead of forcing an unsupported
    # sentinel onto the session.
    effective = clamp_model_thinking_level(requested_level, available)
    if not effective:
        effective = "off"
    return apply_model_thinking_level(adapter, model, effective)


def _clear_session_llm_cache(session: Any) -> None:
    cache = getattr(session, "_llm_adapter_cache", None)
    if isinstance(cache, dict):
        adapters = list({id(adapter): adapter for adapter in cache.values()}.values())
        cache.clear()
        leases = getattr(session, "_llm_adapter_leases", None)
        retired = getattr(session, "_retired_llm_adapters", None)
        if not isinstance(retired, dict):
            retired = {}
            setattr(session, "_retired_llm_adapters", retired)
        for adapter in adapters:
            adapter_id = id(adapter)
            owners = leases.get(adapter_id) if isinstance(leases, dict) else None
            if isinstance(owners, set) and any(not task.done() for task in owners):
                retired[adapter_id] = adapter
                continue
            _schedule_session_llm_close(session, adapter)


def _get_or_create_session_llm(
    session: Any,
    *,
    config: Any,
    provider: str,
    model: str,
    model_runtime: Any | None = None,
) -> Any:
    cache = getattr(session, "_llm_adapter_cache", None)
    if not isinstance(cache, dict):
        # The cache must live on the session: it is what lets a later turn reuse
        # and eventually close this adapter. A local dict would leak it.
        cache = {}
        setattr(session, "_llm_adapter_cache", cache)
    key = _llm_adapter_cache_key(
        config=config,
        provider=provider,
        model=model,
        model_runtime=model_runtime,
    )
    if key in cache:
        adapter = cache[key]
        _lease_session_llm_for_current_task(session, adapter)
        return adapter

    from backend.llm.model_registry import create_session_llm

    adapter = create_session_llm(
        config,
        model_override=model or None,
        provider_override=provider or None,
        model_runtime=model_runtime,
    )
    _clear_session_llm_cache(session)
    cache[key] = adapter
    _lease_session_llm_for_current_task(session, adapter)
    return adapter


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


def _normalized_plan_step(step: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None
    text = str(step.get("step") or "").strip()
    if not text:
        return None
    status = str(step.get("status") or "pending").strip()
    return {
        "step": text,
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

    if event_type == "turn.plan.updated":
        raw_steps = data.get("plan")
        if not isinstance(raw_steps, list):
            return None
        steps = [
            normalized
            for index, step in enumerate(raw_steps)
            if (normalized := _normalized_plan_step(step, index)) is not None
        ]
        state["plan"] = {
            "threadId": str(data.get("thread_id") or data.get("conversation_id") or ""),
            "turnId": str(data.get("turn_id") or ""),
            "plan": steps,
            **({"explanation": str(data.get("explanation"))} if data.get("explanation") else {}),
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
        record = (
            data.get("record")
            if isinstance(data.get("record"), dict)
            else data.get("snapshot")
            if isinstance(data.get("snapshot"), dict)
            else {}
        )
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
        preserve_existing_lifecycle = bool(
            isinstance(existing_progress, dict)
            and progress_id.startswith("provider:")
            and provider_activity_status_rank(existing_progress.get("status"))
            > provider_activity_status_rank(status)
        )
        if (
            isinstance(existing_progress, dict)
            and not preserve_existing_lifecycle
            and str(existing_progress.get("status") or "") in {"completed", "failed"}
            and status not in {"completed", "failed"}
        ):
            return state
        detail = merge_provider_activity_detail(
            existing_progress.get("detail")
            if isinstance(existing_progress, dict)
            else "",
            data.get("detail"),
        )
        entry = {
            "type": "progress",
            "id": progress_id,
            "stage": stage,
            **({"phase": str(data.get("phase"))} if data.get("phase") is not None else {}),
            "status": (
                str(existing_progress.get("status") or status)
                if preserve_existing_lifecycle
                else status
            ),
            "message": (
                str(existing_progress.get("message") or message)
                if preserve_existing_lifecycle
                else message
            ),
            **({"label": str(data.get("label"))} if data.get("label") is not None else {}),
            **(
                {
                    "summary": str(
                        existing_progress.get("summary")
                        or existing_progress.get("message")
                        or message
                    )
                }
                if preserve_existing_lifecycle
                else {"summary": str(data.get("summary"))}
                if data.get("summary") is not None
                else {}
            ),
            **({"visibility": str(data.get("visibility"))} if data.get("visibility") is not None else {}),
            **({"detail": detail} if detail else {}),
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


def _reconcile_ui_agent_state_with_runtime(
    current: Any,
    *,
    runtime: Any,
    conversation_id: str,
) -> tuple[dict[str, Any], bool]:
    """Refresh persisted child rows from the durable agent runtime.

    MiniCode restores child threads from authoritative thread items instead of
    trusting a cached presentation row. MiniCode resumes from the child
    transcript/metadata for the same reason. MiniCode's ``ui_agent_state`` is
    only a projection, so a terminal durable child must win over a stale
    ``running`` row after reconnect or conversation switching.
    """

    from backend.services.subagent_service import build_subagent_status_event

    state = _coerce_ui_agent_state(current)
    changed = False
    for existing in list(state.get("subagents") or []):
        if not isinstance(existing, dict):
            continue
        subagent_id = str(existing.get("id") or "").strip()
        if not subagent_id:
            continue
        try:
            subagent_snapshot = runtime.get_subagent_snapshot(
                subagent_id,
                include_result=True,
            )
        except Exception:
            logger.debug(
                "Failed to inspect durable subagent %s while reconciling conversation %s",
                subagent_id,
                conversation_id,
                exc_info=True,
            )
            continue
        if not isinstance(subagent_snapshot, dict):
            continue

        parent_run_id = str(subagent_snapshot.get("parent_run_id") or "").strip()
        parent_run = runtime.get_run(parent_run_id) if parent_run_id else None
        if (
            parent_run is None
            or str(getattr(parent_run, "conversation_id", "") or "").strip()
            != str(conversation_id or "").strip()
        ):
            continue

        event = build_subagent_status_event(
            subagent_id,
            subagent_snapshot,
            conversation_id=conversation_id,
        )
        next_state = _ui_agent_state_for_event(state, event.type, dict(event.data))
        if next_state is None or next_state == state:
            continue
        state = next_state
        changed = True
    return state, changed


def _merge_ui_agent_state_into_snapshot(snapshot: dict[str, Any], source_snapshot: Any) -> dict[str, Any]:
    if isinstance(source_snapshot, dict):
        # ContextBuilder exports model-context fields. Conversation/session
        # ownership metadata (Plan slug/reference, UI projection revisions,
        # scheduled-task provenance, etc.) must be merged across that replace.
        for key, value in source_snapshot.items():
            if key not in snapshot:
                snapshot[key] = value
        if UI_AGENT_STATE_SNAPSHOT_KEY in source_snapshot:
            snapshot[UI_AGENT_STATE_SNAPSHOT_KEY] = source_snapshot[UI_AGENT_STATE_SNAPSHOT_KEY]
        if UI_AGENT_STATE_REVISION_KEY in source_snapshot:
            snapshot[UI_AGENT_STATE_REVISION_KEY] = source_snapshot[UI_AGENT_STATE_REVISION_KEY]
    return snapshot


async def _commit_automatic_compaction(
    repository: Any,
    *,
    conversation_id: str,
    context_builder: Any,
    summary: str,
    projection_lock: asyncio.Lock | None = None,
) -> dict[str, Any]:
    """Durably replace context before a compacted loop may continue."""

    summary_text = str(summary or "").strip()
    if not summary_text:
        raise RuntimeError("automatic compaction produced no durable summary")
    async def _commit() -> dict[str, Any]:
        saved_snapshot = context_builder.export_snapshot()
        latest = await asyncio.to_thread(repository.get_conversation, conversation_id)
        if latest is None:
            raise RuntimeError(
                "conversation disappeared while committing automatic compaction"
            )
        _merge_ui_agent_state_into_snapshot(
            saved_snapshot,
            getattr(latest, "context_snapshot", None),
        )
        commit = getattr(repository, "commit_compaction", None)
        if not callable(commit):
            raise RuntimeError("conversation repository has no canonical compaction commit")
        committed = await asyncio.to_thread(
            commit,
            conversation_id,
            context_snapshot=saved_snapshot,
            state="compacted",
            summary=summary_text,
            expected_revision=max(0, int(getattr(latest, "revision", 0) or 0)),
        )
        if committed is None:
            raise RuntimeError(
                "conversation disappeared while committing automatic compaction"
            )
        return saved_snapshot

    if projection_lock is None:
        return await _commit()
    async with projection_lock:
        return await _commit()


async def _replay_pending_conversation_projections(
    repository: Any,
    journal: Any,
    *,
    conversation_id: str,
) -> None:
    """Publish durable terminal facts before the next turn mutates context."""

    pending_projection_reader = getattr(
        journal,
        "pending_conversation_projections",
        None,
    )
    if callable(pending_projection_reader):
        for pending_projection in pending_projection_reader():
            payload = dict(getattr(pending_projection, "payload", {}) or {})
            if str(payload.get("conversation_id") or "") != conversation_id:
                continue
            recovered = await asyncio.to_thread(
                repository.commit_turn_projection,
                conversation_id,
                assistant_message=(
                    dict(payload["assistant_message"])
                    if isinstance(payload.get("assistant_message"), dict)
                    else None
                ),
                context_snapshot=(
                    dict(payload["context_snapshot"])
                    if isinstance(payload.get("context_snapshot"), dict)
                    else {}
                ),
                summary=(
                    str(payload["summary"])
                    if payload.get("summary") is not None
                    else None
                ),
                expected_revision=int(payload.get("expected_revision") or 0),
            )
            if recovered is None:
                raise RuntimeError("conversation no longer exists")
            journal.append_lifecycle(
                "conversation_projection_committed",
                {
                    "conversation_id": conversation_id,
                    "pending_event_id": pending_projection.event_id,
                    "conversation_revision": int(
                        getattr(recovered, "revision", 0) or 0
                    ),
                    "message_id": str(
                        (
                            payload.get("assistant_message")
                            if isinstance(payload.get("assistant_message"), dict)
                            else {}
                        ).get("id")
                        or ""
                    ),
                    "recovered": True,
                },
            )

    unprojected_reader = getattr(
        journal,
        "unprojected_terminal_projections",
        None,
    )
    if not callable(unprojected_reader):
        return
    for projection in unprojected_reader():
        if str(projection.get("conversation_id") or "") != conversation_id:
            continue
        assistant_projection = projection.get("assistant_message")
        recovered = await asyncio.to_thread(
            repository.commit_turn_projection,
            conversation_id,
            assistant_message=(
                dict(assistant_projection)
                if isinstance(assistant_projection, dict)
                else None
            ),
            context_snapshot=dict(projection.get("context_snapshot") or {}),
            summary=None,
        )
        if recovered is None:
            raise RuntimeError(
                "conversation disappeared while replaying terminal journal"
            )
        journal.append_lifecycle(
            "conversation_projection_committed",
            {
                "conversation_id": conversation_id,
                "pending_event_id": str(projection.get("source_event_id") or ""),
                "conversation_revision": int(
                    getattr(recovered, "revision", 0) or 0
                ),
                "message_id": str(
                    (assistant_projection or {}).get("id")
                    if isinstance(assistant_projection, dict)
                    else ""
                ),
                "recovered": True,
                "minimal_projection": True,
            },
        )


class SessionAgentRunnerMixin:
    """Agent run logic for WebSocketSession.

    Depends on session attributes: ws, query_engine, conversation_repo,
    context_builder, permission_checker, permission_context, config,
    llm, artifact_store, tool_registry, skill_manager,
    _approval_handler, _active_task_id, _interrupted, etc.
    """

    def _conversation_projection_lock(self, conversation_id: str) -> asyncio.Lock:
        locks = getattr(self, "_conversation_projection_locks", None)
        if not isinstance(locks, dict):
            locks = self._conversation_projection_locks = {}
        lock = locks.get(conversation_id)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            locks[conversation_id] = lock
        return lock

    async def _flush_ui_agent_state_now(self, conversation_id: str) -> None:
        async with self._conversation_projection_lock(conversation_id):
            await self._flush_ui_agent_state_now_unlocked(conversation_id)

    async def _flush_ui_agent_state_now_unlocked(self, conversation_id: str) -> None:
        pending = getattr(self, "_ui_agent_state_pending", {})
        tasks = getattr(self, "_ui_agent_state_tasks", {})
        current_task = asyncio.current_task()
        scheduled = tasks.get(conversation_id)
        if scheduled is not None and scheduled is not current_task:
            if not scheduled.done():
                scheduled.cancel()
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

        if tasks.get(conversation_id) is current_task or tasks.get(conversation_id) is scheduled:
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
        terminal_fences = getattr(self, "_terminal_projection_fences", None)
        fenced_message_id = (
            terminal_fences.get(conversation_id)
            if isinstance(terminal_fences, dict)
            else None
        )
        incoming_message_id = str(data.get("message_id") or "").strip()
        if fenced_message_id and incoming_message_id in {
            "",
            str(fenced_message_id),
        }:
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

    async def _reconcile_persisted_ui_agent_state(
        self,
        conversation_id: str,
        *,
        conversation: Any | None = None,
    ) -> Any | None:
        """Persist an authoritative child-status projection before hydration."""

        owner = str(conversation_id or "").strip()
        if not owner:
            return None
        async with self._conversation_projection_lock(owner):
            pending = getattr(self, "_ui_agent_state_pending", {})
            tasks = getattr(self, "_ui_agent_state_tasks", {})
            scheduled = tasks.get(owner) if isinstance(tasks, dict) else None
            must_reload_after_flush = (
                isinstance(pending, dict)
                and owner in pending
            ) or (
                scheduled is not None
                and not scheduled.done()
            )
            await self._flush_ui_agent_state_now_unlocked(owner)
            loaded_owner = str(getattr(conversation, "id", "") or "").strip()
            if conversation is None or loaded_owner != owner or must_reload_after_flush:
                conversation = await asyncio.to_thread(
                    self.conversation_repo.get_conversation,
                    owner,
                )
            if conversation is None:
                return None
            snapshot = dict(getattr(conversation, "context_snapshot", {}) or {})
            current_state = snapshot.get(UI_AGENT_STATE_SNAPSHOT_KEY)
            reconciled_state, changed = _reconcile_ui_agent_state_with_runtime(
                current_state,
                runtime=default_runtime(),
                conversation_id=owner,
            )
            if not changed:
                return conversation

            try:
                current_revision = int(snapshot.get(UI_AGENT_STATE_REVISION_KEY) or 0)
            except (TypeError, ValueError):
                current_revision = 0
            revision = max(current_revision + 1, time.time_ns())
            updated = await asyncio.to_thread(
                self.conversation_repo.patch_context_snapshot,
                owner,
                {UI_AGENT_STATE_SNAPSHOT_KEY: reconciled_state},
                revision=revision,
                revision_key=UI_AGENT_STATE_REVISION_KEY,
            )
            cache = getattr(self, "_ui_agent_state_cache", None)
            if not isinstance(cache, dict):
                cache = self._ui_agent_state_cache = {}
            cache[owner] = (revision, reconciled_state)
            return updated or conversation

    def _extension_runtime_state(self, conversation_id: str) -> LifecycleGenerationState:
        states = getattr(self, "_extension_runtime_states", None)
        if not isinstance(states, dict):
            states = self._extension_runtime_states = {}
        clean_id = str(conversation_id or "").strip()
        if not clean_id:
            raise ValueError("conversation id is required for lifecycle runtime ownership")
        state = states.get(clean_id)
        if state is None:
            state = LifecycleGenerationState(clean_id)
            states[clean_id] = state
        elif not isinstance(state, LifecycleGenerationState):
            raise TypeError(
                f"lifecycle runtime state for {clean_id!r} is not canonical"
            )
        return state

    def _schedule_extension_session_shutdown(
        self,
        conversation_id: str,
        runner: Any,
    ) -> bool:
        """Honor MiniCode's deferred ``ctx.shutdown()`` at the WebSocket owner.

        Pi RPC records the request synchronously, then disposes the runtime
        only after the command that requested it (or the active agent run) has
        settled.  The WebSocket session is MiniCode's equivalent owner.  A
        candidate generation may record a request, but cannot close the live
        session until that generation has been atomically published.
        """

        clean_id = str(conversation_id or "").strip()
        state = getattr(self, "_extension_runtime_states", {}).get(clean_id)
        if not isinstance(state, LifecycleGenerationState) or not state.is_current(
            runner
        ):
            return False
        state["shutdown_requested"] = True
        setattr(self, "_extension_shutdown_requested", True)
        run_manager = getattr(self, "_run_manager", None)
        stop_wakes = getattr(run_manager, "stop_notification_wake_intake", None)
        if callable(stop_wakes):
            stop_wakes()
        clear_queues = getattr(run_manager, "clear_all_user_message_queues", None)
        if callable(clear_queues):
            clear_queues()

        existing = getattr(self, "_extension_requested_shutdown_task", None)
        if isinstance(existing, asyncio.Task) and not existing.done():
            return True
        owner_task = asyncio.current_task()

        async def _shutdown_after_owner_boundary() -> None:
            if owner_task is not None and owner_task is not asyncio.current_task():
                try:
                    await asyncio.shield(owner_task)
                except (asyncio.CancelledError, Exception):
                    # Completion, cancellation, and failure are all settled
                    # command boundaries. Session shutdown still proceeds.
                    pass
            await asyncio.sleep(0)
            manager = getattr(self, "_ws_manager", None)
            shutdown_session = getattr(manager, "shutdown_session", None)
            if callable(shutdown_session):
                await shutdown_session(
                    str(getattr(self, "session_id", "") or ""),
                    reason="extension_shutdown",
                )
                return
            shutdown = getattr(self, "shutdown", None)
            if callable(shutdown):
                await shutdown(reason="extension_shutdown")
            websocket = getattr(self, "ws", None)
            close = getattr(websocket, "close", None)
            if callable(close):
                try:
                    await close(code=1000, reason="extension shutdown")
                except Exception:
                    logger.debug(
                        "Failed to close websocket after extension shutdown",
                        exc_info=True,
                    )

        try:
            task = asyncio.create_task(
                _shutdown_after_owner_boundary(),
                name=f"extension-session-shutdown:{clean_id or 'session'}",
            )
        except RuntimeError:
            return False
        self._extension_requested_shutdown_task = task

        def _done(done: asyncio.Task[Any]) -> None:
            if getattr(self, "_extension_requested_shutdown_task", None) is done:
                self._extension_requested_shutdown_task = None
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                logger.exception(
                    "Extension-requested session shutdown failed for %s",
                    clean_id or "<session>",
                )

        task.add_done_callback(_done)
        track = getattr(self, "_track_command_task", None)
        if callable(track):
            track(task)
        return True

    def _model_runtime_for_conversation(
        self, conversation_id: str | None
    ) -> Any | None:
        clean_id = str(conversation_id or "").strip()
        state = getattr(self, "_extension_runtime_states", {}).get(clean_id)
        if not isinstance(state, dict):
            return None
        runtime = state.get("model_runtime")
        return runtime if bool(getattr(runtime, "active", False)) else None

    def _model_registry_for_conversation(
        self, conversation_id: str | None
    ) -> Any | None:
        clean_id = str(conversation_id or "").strip()
        state = getattr(self, "_extension_runtime_states", {}).get(clean_id)
        if not isinstance(state, dict):
            return None
        registry = state.get("model_registry")
        runtime = getattr(registry, "runtime", None)
        return registry if bool(getattr(runtime, "active", False)) else None

    def _on_model_runtime_changed(
        self,
        conversation_id: str,
        runtime: Any,
        provider_id: str,
        action: str,
    ) -> None:
        """Refresh only if the mutating MiniCode generation is still published."""

        clean_id = str(conversation_id or "").strip()
        state = getattr(self, "_extension_runtime_states", {}).get(clean_id)
        if (
            not isinstance(state, dict)
            or state.get("model_runtime") is not runtime
            or bool(state.get("shutting_down"))
        ):
            return
        _clear_session_llm_cache(self)
        existing = state.get("model_refresh_task")
        if isinstance(existing, asyncio.Task) and not existing.done():
            return
        try:
            task = asyncio.create_task(
                self._refresh_model_runtime_projection(
                    clean_id,
                    runtime,
                    provider_id=provider_id,
                    action=action,
                )
            )
        except RuntimeError:
            return
        state["model_refresh_task"] = task

        def _done(done: asyncio.Task[Any]) -> None:
            if state.get("model_refresh_task") is done:
                state.pop("model_refresh_task", None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "Failed to refresh provider projection for conversation %s",
                    clean_id,
                )

        task.add_done_callback(_done)
        track = getattr(self, "_track_command_task", None)
        if callable(track):
            track(task)

    async def _refresh_model_runtime_projection(
        self,
        conversation_id: str,
        runtime: Any,
        *,
        provider_id: str,
        action: str,
    ) -> None:
        del provider_id, action
        state = getattr(self, "_extension_runtime_states", {}).get(conversation_id)
        if not isinstance(state, dict) or state.get("model_runtime") is not runtime:
            return
        refresh_dynamic_models = getattr(runtime, "refresh_dynamic_models", None)
        if callable(refresh_dynamic_models):
            # MiniCode schedules an offline provider-model refresh after registration.
            # Run it for the owning conversation even while that conversation
            # is not the visible tab; refreshModels stores and OAuth-dependent
            # model projections belong to the runtime generation, not the UI.
            await refresh_dynamic_models(allow_network=False, force=True)
        else:
            runtime.refresh()
        state = getattr(self, "_extension_runtime_states", {}).get(conversation_id)
        if not isinstance(state, dict) or state.get("model_runtime") is not runtime:
            return
        if conversation_id != str(getattr(self, "active_conversation_id", "") or ""):
            return
        configured_provider = str(
            getattr(self, "_resolve_llm_provider", get_llm_provider)() or ""
        ).strip().lower()
        provider = str(getattr(self, "provider", "") or "").strip()
        provider_override = bool(getattr(self, "_provider_override_active", False))
        if not provider_override or runtime.get_provider(provider) is None:
            provider = configured_provider
            self._provider_override_active = False
        refresh_provider_auth = getattr(runtime, "refresh_provider_auth", None)
        if callable(refresh_provider_auth):
            await refresh_provider_auth(provider)
        models = list(runtime.get_models(provider))
        available_models = [model.id for model in models]
        selected = str(getattr(self, "selected_model", "") or "").strip()
        if not selected:
            # Preserve an explicit configured model even when the refreshed
            # catalog no longer advertises it. The run admission boundary
            # reports that mismatch; selecting the first catalog entry would
            # change the user's model silently.
            selected = str(
                getattr(load_config().llm, "model", "") or ""
            ).strip()
        self.provider = provider
        self.available_models = available_models
        self.selected_model = selected
        self.models_source = (
            "extension"
            if runtime.get_registered_provider_config(provider) is not None
            else getattr(self, "_resolve_models_source", lambda _provider: "")(provider)
        )
        if selected and runtime.get_model(provider, selected) is not None:
            self.config = _config_with_runtime_model_budget(
                load_config(),
                model_runtime=runtime,
                provider=provider,
                model=selected,
            )
            self.llm = _get_or_create_session_llm(
                self,
                config=self.config,
                provider=provider,
                model=selected,
                model_runtime=runtime,
            )
            self.context_builder._llm = self.llm
            self.context_builder._budget = self.config.token_budget
        send_state = getattr(self, "_send_llm_state", None)
        if callable(send_state):
            await send_state()

    def _conversation_tool_registry(
        self,
        conversation_id: str,
        *,
        workspace_root: Path | None = None,
        force_rebuild: bool = False,
    ) -> Any:
        """Return the MiniCode-owned tool registry for one conversation."""

        clean_id = str(conversation_id or "").strip()
        registries = getattr(self, "_conversation_tool_registries", None)
        if not isinstance(registries, dict):
            registries = self._conversation_tool_registries = {}
        if workspace_root is None and clean_id:
            conversation = self.conversation_repo.get_conversation(clean_id)
            if conversation is not None:
                workspace_root = self._workspace_root_for_conversation(conversation)
        current_manager = self._mcp_manager_for_workspace(workspace_root)
        current_version = int(getattr(current_manager, "registry_version", 0) or 0)
        workspace_key = self._extension_workspace_key(workspace_root)
        try:
            effective_config = load_config(cwd=workspace_root)
        except TypeError:
            effective_config = load_config()
        existing = registries.get(clean_id)
        if isinstance(existing, tuple) and len(existing) == 3:
            version, registry_workspace_key, registry = existing
            if (
                not force_rebuild
                and int(version) == current_version
                and str(registry_workspace_key or "") == workspace_key
            ):
                return registry
            task = getattr(self, "_conversation_run_tasks", {}).get(clean_id)
            if (
                isinstance(task, asyncio.Task)
                and task is not asyncio.current_task()
                and not task.done()
            ):
                # An in-flight AgentSession keeps the registry it captured.  A
                # later turn rebuilds after the MCP/config generation changes.
                return registry

        registry = self._build_conversation_tool_registry(
            clean_id,
            workspace_root=workspace_root,
            mcp_manager=current_manager,
        )
        registries[clean_id] = (current_version, workspace_key, registry)
        return registry

    def _mcp_manager_for_workspace(self, workspace_root: Path | None) -> Any | None:
        """Resolve an already-created MCP owner without changing global state."""

        try:
            from backend.api import _state

            bootstrap = getattr(_state, "bootstrap", None)
            get_manager = getattr(bootstrap, "get_mcp_manager_for_workspace", None)
            if callable(get_manager):
                manager = get_manager(workspace_root)
                if manager is not None:
                    return manager
        except Exception:
            logger.debug("Failed to resolve workspace MCP manager", exc_info=True)
        return getattr(self, "mcp_manager", None)

    def _build_conversation_tool_registry(
        self,
        conversation_id: str = "",
        *,
        workspace_root: Path | None = None,
        mcp_manager: Any | None = None,
    ) -> Any:
        """Build one fresh MiniCode agent-run tool-registry generation."""

        from backend.api import _state

        bootstrap = getattr(_state, "bootstrap", None)
        if bootstrap is None:
            raise RuntimeError(
                "MiniCode bootstrap is unavailable; conversation tool registry was not created"
            )
        manager = (
            mcp_manager
            if mcp_manager is not None
            else self._mcp_manager_for_workspace(workspace_root)
        )
        try:
            return bootstrap.create_tool_registry(
                self.artifact_store,
                mcp_manager=manager,
            )
        except Exception:
            logger.exception(
                "Failed to build conversation tool registry for %s",
                str(conversation_id or "").strip() or "<unbound>",
            )
            raise RuntimeError(
                "MiniCode could not create the conversation tool registry"
            )

    def _store_conversation_tool_registry(
        self,
        conversation_id: str,
        workspace_root: Path | None,
        registry: Any,
    ) -> None:
        registries = getattr(self, "_conversation_tool_registries", None)
        if not isinstance(registries, dict):
            registries = self._conversation_tool_registries = {}
        manager = self._mcp_manager_for_workspace(workspace_root)
        registries[str(conversation_id or "").strip()] = (
            int(getattr(manager, "registry_version", 0) or 0),
            self._extension_workspace_key(workspace_root),
            registry,
        )

    def _lifecycle_runtime_for_conversation(
        self, conversation_id: str | None
    ) -> Any | None:
        clean_id = str(conversation_id or "").strip()
        state = getattr(self, "_extension_runtime_states", {}).get(clean_id)
        if not isinstance(state, LifecycleGenerationState):
            return None
        runtime = state.runtime
        return runtime if bool(getattr(runtime, "active", False)) else None

    @staticmethod
    def _extension_workspace_key(workspace_root: Path | None) -> str:
        if workspace_root is None:
            return ""
        try:
            return str(Path(workspace_root).expanduser().resolve())
        except OSError:
            return ""

    async def _ensure_lifecycle_runtime(
        self,
        *,
        conversation_id: str,
        workspace_root: Path | None,
        tool_registry: Any,
        force_reload: bool = False,
        reload_reason: str = "reload",
        defer_previous_shutdown_until: asyncio.Task[Any] | None = None,
    ) -> Any | None:
        """Load the extension generation owned by one MiniCode conversation.

        Discovery is intentionally limited to :class:`ExtensionLoader`'s
        MiniCode project/user roots. A project root is marked trusted only by
        the desktop workspace-trust ledger; no client metadata can opt a path
        into executable scope. The same runtime returned by ``load`` is retained
        and injected into the turn context so late registrations and tool hooks
        observe one lifecycle owner.
        """

        # Session shutdown is a one-way lifecycle fence. A slow extension
        # discovery that began before disconnect must not publish a fresh
        # generation after the websocket/session owner has been destroyed.
        if bool(getattr(self, "_extension_runtimes_shutting_down", False)):
            return None

        owner_id = str(conversation_id or "").strip()
        if not owner_id:
            raise ValueError("conversation id is required to load extensions")
        conversation_repo = getattr(self, "conversation_repo", None)
        if conversation_repo is None:
            raise RuntimeError("conversation repository is required to load extensions")
        conversation = conversation_repo.get_conversation(owner_id)
        if conversation is None:
            raise RuntimeError(
                f"Conversation {owner_id!r} disappeared while loading extensions"
            )
        state = self._extension_runtime_state(owner_id)
        lock = state["lock"]

        async with lock:
            if bool(getattr(self, "_extension_runtimes_shutting_down", False)) or bool(
                state.get("shutting_down")
            ):
                return None
            runtime = state.runtime
            workspace_key = self._extension_workspace_key(workspace_root)
            bound_registry = state.get("registry")
            runner_workspace_key = str(state.get("workspace_key") or "")
            registries = getattr(self, "_conversation_tool_registries", None)
            published_registry = None
            if isinstance(registries, dict):
                published = registries.get(owner_id)
                if isinstance(published, tuple) and len(published) == 3:
                    published_registry = published[-1]
            if (
                bound_registry is not None
                and published_registry is bound_registry
                and tool_registry is not published_registry
            ):
                # A turn can capture its registry just before a settings refresh
                # publishes a new complete MiniCode generation.  Reuse the canonical
                # published registry instead of replacing that fresh runtime
                # with the stale pre-refresh snapshot.  A genuine MCP registry
                # rebuild remains distinguishable because it is published
                # before the still-old runner is rebound.
                tool_registry = published_registry

            project_trusted = bool(
                workspace_root is not None and is_workspace_trusted(workspace_root)
            )
            replacing_runtime = runtime is not None and bool(
                getattr(runtime, "active", False)
            )
            capability_source = ExtensionCapabilitySource(
                session_owner=str(getattr(self, "session_id", "") or id(self)),
                owner_id=owner_id,
                workspace_root=workspace_root,
                project_trusted=project_trusted,
                on_model_change=lambda runtime, provider_id, action: self._on_model_runtime_changed(
                    owner_id, runtime, provider_id, action
                ),
            )
            generation_fingerprint = capability_source.fingerprint()
            runtime_fingerprint = str(state.get("fingerprint") or "")

            if runtime is not None and bool(getattr(runtime, "active", False)):
                if (
                    not force_reload
                    and runner_workspace_key == workspace_key
                    and bound_registry is tool_registry
                    and runtime_fingerprint == generation_fingerprint
                ):
                    return runtime
            # The source owns discovery, trust-scoped path expansion, loader
            # cache partitioning and provider capability construction. The WS
            # owner only receives an unpublished candidate.
            try:
                capability = await capability_source.load(clear_cache=replacing_runtime)
            except Exception:
                logger.exception(
                    "Failed to initialize extension capability for conversation %s",
                    owner_id,
                )
                if replacing_runtime:
                    return runtime
                state.discard_published()
                return None
            loader = capability.loader
            previous_runtime = runtime
            previous_loader = state.get("loader")
            previous_model_runtime = state.get("model_runtime")
            if replacing_runtime:
                # Clear only this session/cwd partition.  The old generation
                # remains live until the fresh modules have loaded, matching
                # MiniCode's atomic active-component swap.
                loader.clear_cache()
                if bound_registry is tool_registry:
                    # MiniCode replaces the complete AgentSession registry on reload.
                    # Binding a fresh generation into the old registry would
                    # make rollback impossible if registration fails and can
                    # leave stale adapters as the replacement chain.  Build the
                    # candidate registry off to the side and publish it only
                    # after the new runner is fully bound.
                    tool_registry = self._build_conversation_tool_registry(owner_id)
            candidate_model_runtime = capability.model_runtime
            candidate_model_registry = capability.model_registry
            result = capability.result

            next_runtime = result.runner
            if next_runtime is None:
                logger.warning(
                    "Extension loader returned no runner for conversation %s",
                    owner_id,
                )
                candidate_model_runtime.retire()
                if replacing_runtime:
                    return runtime
                retire_previous_model_runtime = getattr(
                    previous_model_runtime, "retire", None
                )
                if callable(retire_previous_model_runtime):
                    retire_previous_model_runtime()
                state.discard_published()
                return None
            if bool(getattr(self, "_extension_runtimes_shutting_down", False)) or bool(
                state.get("shutting_down")
            ):
                # Discovery completed after the session shutdown fence. This
                # candidate was never published, so invalidate it directly and
                # retire its private model runtime without session_start or
                # session_shutdown lifecycle events.
                try:
                    next_runtime.invalidate(
                        "Extension generation was discarded during session shutdown"
                    )
                finally:
                    candidate_model_runtime.retire()
                return None
            for error in result.errors:
                logger.warning(
                    "MiniCode extension unavailable for conversation %s (%s): %s",
                    owner_id,
                    error.get("path", "<unknown>"),
                    error.get("error", "unknown error"),
                )

            try:
                next_runtime.bind_tool_registry(tool_registry)
                # MiniCode binds the complete AgentSession runtime before its command
                # surface becomes invokable.  Give the off-to-the-side
                # candidate a conversation-owned command snapshot before the
                # scoped command projection is atomically replaced; otherwise
                # a concurrent slash dispatch can reach a half-bound runner.
                self._bind_lifecycle_runtime_command_snapshot(
                    next_runtime,
                    conversation=conversation,
                    tool_registry=tool_registry,
                    model_runtime=candidate_model_runtime,
                    model_registry=candidate_model_registry,
                )
                command_registry = getattr(self, "command_registry", None)
                if command_registry is not None:
                    next_runtime.bind_command_registry(
                        command_registry,
                        scope_id=owner_id,
                    )
            except Exception:
                logger.exception(
                    "Failed to bind fresh MiniCode extension generation for conversation %s",
                    owner_id,
                )
                try:
                    # The candidate never became the active AgentSession
                    # generation, so it must not receive MiniCode's session_shutdown
                    # lifecycle event. Invalidate its wrappers directly, as an
                    # unpublished MiniCode active-component candidate.
                    next_runtime.invalidate(
                        "Extension generation was discarded before publication"
                    )
                except Exception:
                    logger.debug("Fresh extension runner cleanup failed", exc_info=True)
                candidate_model_runtime.retire()
                if replacing_runtime:
                    command_registry = getattr(self, "command_registry", None)
                    rebind_commands = getattr(runtime, "bind_command_registry", None)
                    if command_registry is not None and callable(rebind_commands):
                        try:
                            rebind_commands(command_registry, scope_id=owner_id)
                        except Exception:
                            logger.exception(
                                "Failed to restore previous extension command projection for %s",
                                owner_id,
                            )
                    return runtime
                retire_previous_model_runtime = getattr(
                    previous_model_runtime, "retire", None
                )
                if callable(retire_previous_model_runtime):
                    retire_previous_model_runtime()
                state.discard_published()
                return None

            runtime = next_runtime
            state.publish(
                runtime=runtime,
                loader=loader,
                registry=tool_registry,
                workspace_key=workspace_key,
                fingerprint=generation_fingerprint,
                result=result,
                host_actions_bound=True,
                model_runtime=candidate_model_runtime,
                model_registry=candidate_model_registry,
            )
            state.pop("reload_pending", None)
            available_tool_names = _activatable_tool_names(tool_registry)
            available_tool_set = set(available_tool_names)
            if hasattr(runtime, "_pending_active_tools"):
                pending_active_tools = getattr(runtime, "_pending_active_tools")
                try:
                    delattr(runtime, "_pending_active_tools")
                except AttributeError:
                    pass
                state["active_tool_names"] = [
                    name
                    for name in pending_active_tools
                    if name in available_tool_set
                ]
            elif "active_tool_names" in state:
                selected_tool_names = [
                    str(name)
                    for name in state.get("active_tool_names") or []
                    if str(name) in available_tool_set
                ]
                if replacing_runtime:
                    registered_tools = getattr(
                        runtime,
                        "get_registered_tools",
                        lambda: [],
                    )()
                    selected_tool_names.extend(
                        str(getattr(definition, "name", "") or "")
                        for definition in registered_tools
                        if str(getattr(definition, "name", "") or "")
                        in available_tool_set
                    )
                state["active_tool_names"] = list(
                    dict.fromkeys(selected_tool_names)
                )
            pending_model_selection = getattr(
                runtime, "_pending_model_selection", None
            )
            if pending_model_selection is not None:
                try:
                    delattr(runtime, "_pending_model_selection")
                except AttributeError:
                    pass
                try:
                    await runtime.runtime.set_model(pending_model_selection)
                except Exception:
                    logger.exception(
                        "Failed to apply staged extension model selection for conversation %s",
                        owner_id,
                    )
            if bool(getattr(runtime, "_shutdown_requested", False)):
                self._schedule_extension_session_shutdown(owner_id, runtime)
            self._store_conversation_tool_registry(
                owner_id,
                workspace_root,
                tool_registry,
            )
            if previous_runtime is not None or previous_model_runtime is not None:
                reason = (
                    "workspace_switch"
                    if runner_workspace_key != workspace_key
                    else reload_reason
                    if force_reload or runtime_fingerprint != generation_fingerprint
                    else "registry_refresh"
                )
                await self._retire_lifecycle_runtime(
                    owner_id,
                    previous_runtime,
                    previous_loader,
                    previous_model_runtime,
                    reason=reason,
                    clear_loader_cache=runner_workspace_key != workspace_key,
                    defer_until=defer_previous_shutdown_until,
                )
            try:
                # MiniCode emits session_start only after the complete AgentSession
                # runtime and command surface are live. This event may rename
                # the session, append entries, send messages, select tools, or
                # select a model, so running it against an unpublished
                # candidate would violate MiniCode's atomic refresh boundary.
                await runtime.startup(
                    "reload" if replacing_runtime else "startup"
                )
            except Exception:
                # Individual MiniCode handlers are isolated by ExtensionRunner.emit;
                # this guard covers only host/runtime failures after publish.
                logger.exception(
                    "MiniCode extension session_start failed for conversation %s",
                    owner_id,
                )
            self._on_model_runtime_changed(
                owner_id,
                candidate_model_runtime,
                "*",
                "publish",
            )
            return runtime

    async def _retire_lifecycle_runtime(
        self,
        conversation_id: str,
        runner: Any,
        loader: Any,
        model_runtime: Any,
        *,
        reason: str,
        clear_loader_cache: bool,
        defer_until: asyncio.Task[Any] | None,
    ) -> None:
        """Retire an old lifecycle generation after its captured turn releases it."""

        if isinstance(defer_until, asyncio.Task) and not defer_until.done():
            state = self._extension_runtime_state(conversation_id)
            state.retire(
                runtime=runner,
                loader=loader,
                model_runtime=model_runtime,
                reason=reason,
                clear_loader_cache=clear_loader_cache,
                defer_until=defer_until,
            )

            def _ready(_done: asyncio.Task[Any]) -> None:
                try:
                    cleanup = asyncio.create_task(
                        self._drain_retired_lifecycle_runtimes(conversation_id)
                    )
                except RuntimeError:
                    return
                track = getattr(self, "_track_command_task", None)
                if callable(track):
                    track(cleanup)

            defer_until.add_done_callback(_ready)
            return
        await self._shutdown_extension_generation(
            conversation_id,
            runner,
            loader,
            model_runtime,
            reason=reason,
            clear_loader_cache=clear_loader_cache,
        )

    async def _drain_retired_lifecycle_runtimes(
        self,
        conversation_id: str,
        *,
        force: bool = False,
    ) -> None:
        states = getattr(self, "_extension_runtime_states", None)
        if not isinstance(states, dict):
            return
        state = states.get(str(conversation_id or "").strip())
        if not isinstance(state, LifecycleGenerationState):
            return
        lock = state.get("lock")
        if not isinstance(lock, asyncio.Lock):
            return
        async with lock:
            ready = state.take_ready_retirements(force=force)
        for record in ready:
            await self._shutdown_extension_generation(
                conversation_id,
                record.get("runtime"),
                record.get("loader"),
                record.get("model_runtime"),
                reason=str(record.get("reason") or "reload"),
                clear_loader_cache=bool(record.get("clear_loader_cache")),
            )

    async def _shutdown_extension_generation(
        self,
        conversation_id: str,
        runner: Any,
        loader: Any,
        model_runtime: Any,
        *,
        reason: str,
        clear_loader_cache: bool,
    ) -> None:
        try:
            shutdown = getattr(runner, "shutdown", None)
            if callable(shutdown):
                await shutdown(reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to shut down stale extension generation for conversation %s",
                conversation_id,
            )
        finally:
            if clear_loader_cache:
                clear_cache = getattr(loader, "clear_cache", None)
                if callable(clear_cache):
                    try:
                        clear_cache()
                    except Exception:
                        logger.exception(
                            "Failed to clear previous extension cache for conversation %s",
                            conversation_id,
                        )
            retire_model_runtime = getattr(model_runtime, "retire", None)
            if callable(retire_model_runtime):
                try:
                    retire_model_runtime()
                except Exception:
                    logger.exception(
                        "Failed to retire model runtime for conversation %s",
                        conversation_id,
                    )

    async def _shutdown_lifecycle_runtimes(self, reason: str = "session_shutdown") -> None:
        """Close every conversation-owned extension lifecycle generation."""

        self._extension_runtimes_shutting_down = True
        states = getattr(self, "_extension_runtime_states", {})
        if not isinstance(states, dict):
            return
        cleanup_tasks: list[asyncio.Task[Any]] = []
        current_task = asyncio.current_task()
        try:
            for conversation_id, state in list(states.items()):
                if not isinstance(state, LifecycleGenerationState):
                    continue
                state.fence_shutdown()
                refresh_task = state.get("model_refresh_task")
                if (
                    isinstance(refresh_task, asyncio.Task)
                    and refresh_task is not current_task
                    and not refresh_task.done()
                ):
                    refresh_task.cancel()
                    cleanup_tasks.append(refresh_task)
                generations: list[tuple[Any, Any, Any]] = [
                    (
                        state.get("runtime"),
                        state.get("loader"),
                        state.get("model_runtime"),
                    )
                ]
                generations.extend(
                    (
                        record.get("runtime"),
                        record.get("loader"),
                        record.get("model_runtime"),
                    )
                    for record in list(
                        state.get("retired_generations") or []
                    )
                    if isinstance(record, dict)
                )
                seen_runners: set[int] = set()
                seen_loaders: set[int] = set()
                seen_model_runtimes: set[int] = set()
                for runner, loader, model_runtime in generations:
                    shutdown_runner = runner
                    if runner is None or id(runner) in seen_runners:
                        shutdown_runner = None
                    elif runner is not None:
                        seen_runners.add(id(runner))
                    clear_loader_cache = (
                        loader is not None and id(loader) not in seen_loaders
                    )
                    if clear_loader_cache:
                        seen_loaders.add(id(loader))
                    retire_model_runtime = model_runtime
                    if (
                        model_runtime is None
                        or id(model_runtime) in seen_model_runtimes
                    ):
                        retire_model_runtime = None
                    elif model_runtime is not None:
                        seen_model_runtimes.add(id(model_runtime))
                    if (
                        shutdown_runner is None
                        and not clear_loader_cache
                        and retire_model_runtime is None
                    ):
                        continue
                    cleanup_tasks.append(
                        asyncio.create_task(
                            self._shutdown_extension_generation(
                                conversation_id,
                                shutdown_runner,
                                loader,
                                retire_model_runtime,
                                reason=reason,
                                clear_loader_cache=clear_loader_cache,
                            ),
                            name=f"extension-generation-shutdown:{conversation_id}",
                        )
                    )
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        finally:
            # The session is the owner. Even if its bounded shutdown cancels
            # this drain, every generation task receives cancellation together
            # and its own finally block invalidates the remaining resources.
            for state in list(states.values()):
                if isinstance(state, dict):
                    state.clear()
            states.clear()
            registries = getattr(self, "_conversation_tool_registries", None)
            if isinstance(registries, dict):
                registries.clear()

    def _bind_lifecycle_runtime_host_actions(
        self,
        runner: Any,
        *,
        conversation: Any,
        tool_registry: Any,
        run_metadata: dict[str, Any],
        run_context_builder: Any,
        run_llm: Any,
        cancel_event: asyncio.Event | None,
        model_runtime: Any | None = None,
        model_registry: Any | None = None,
        agent_session: AgentSession | None = None,
    ) -> None:
        """Bind MiniCode runtime actions to the existing websocket/session owner.

        Pi's ExtensionRunner is the lifecycle owner, while the host remains
        authoritative for conversations, model selection, and turn queues.
        These callbacks are intentionally a thin projection of those existing
        services; they do not introduce a second session or message runtime.
        """

        bind_actions = getattr(runner, "bind_actions", None)
        bind_context_actions = getattr(runner, "bind_context_actions", None)
        if not callable(bind_actions) and not callable(bind_context_actions):
            return

        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        run_manager = getattr(self, "_run_manager", None)
        model_runtime = model_runtime or self._model_runtime_for_conversation(
            conversation_id
        )
        model_registry = model_registry or self._model_registry_for_conversation(
            conversation_id
        )

        def _published_runtime_state() -> dict[str, Any] | None:
            state = getattr(self, "_extension_runtime_states", {}).get(
                conversation_id
            )
            return (
                state
                if isinstance(state, LifecycleGenerationState)
                and state.is_current(runner)
                else None
            )

        def _available_tool_names() -> list[str]:
            return _activatable_tool_names(tool_registry)

        def _default_active_tool_names() -> list[str]:
            from backend.tools.toolsets import ToolsetPolicy

            get_spec = getattr(tool_registry, "get_tool_spec", None)
            if not callable(get_spec):
                return _available_tool_names()
            policy = ToolsetPolicy.default()
            return [
                name
                for name in _available_tool_names()
                if policy.is_directly_visible(get_spec(name))
            ]

        def _message_text(value: Any) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                return str(value.get("text") or value.get("content") or "")
            if isinstance(value, (list, tuple)):
                return "\n".join(
                    part
                    for item in value
                    if (part := _message_text(item)).strip()
                )
            return str(value or "")

        def _command(content: Any, options: Any = None) -> UserCommand:
            options_map = options if isinstance(options, dict) else {}
            text = _message_text(content)
            return UserCommand(
                type="user_message",
                data={
                    "content": text,
                    "conversation_id": conversation_id,
                    "assistant_message_id": f"assistant_extension_{uuid.uuid4().hex}",
                    "user_message_id": f"user_extension_{uuid.uuid4().hex}",
                    "streaming_behavior": str(
                        options_map.get("deliverAs")
                        or options_map.get("deliver_as")
                        or "steer"
                    ).strip().lower(),
                },
            )

        def _send_message(message: Any, options: Any = None) -> None:
            # MiniCode custom messages are converted to user-role provider messages;
            # keep them turn-local until the next context build boundary.
            run_metadata.setdefault("_extension_pending_messages", []).append(
                {"message": message, "options": options}
            )

        def _send_user_message(content: Any, options: Any = None) -> Any:
            command = _command(content, options)
            options_map = options if isinstance(options, dict) else {}
            deliver_as = str(
                options_map.get("deliverAs")
                or options_map.get("deliver_as")
                or "steer"
            ).strip().lower()
            if run_manager is not None and conversation_id:
                if deliver_as == "followup":
                    return run_manager.enqueue_user_message(conversation_id, command)
                target_message_id = str(
                    getattr(self, "_conversation_streams", {})
                    .get(conversation_id, {})
                    .get("message_id")
                    or ""
                ).strip()
                item = run_manager.enqueue_turn_steer(
                    conversation_id,
                    command,
                    target_message_id=target_message_id,
                )
                if item is not None:
                    return item
                return run_manager.enqueue_user_message(conversation_id, command)
            run_metadata.setdefault("_extension_pending_user_messages", []).append(
                command
            )
            return command

        def _append_entry(custom_type: Any, data: Any = None) -> None:
            run_metadata.setdefault("_extension_entries", []).append(
                {"custom_type": str(custom_type or ""), "data": data}
            )

        def _set_session_name(name: Any) -> Any:
            rename = getattr(self.conversation_repo, "rename_conversation", None)
            if callable(rename) and conversation_id:
                return rename(conversation_id, str(name or ""))
            return None

        def _get_session_name() -> str:
            return str(getattr(conversation, "title", "") or "")

        def _set_label(entry_id: Any, label: Any = None) -> None:
            run_metadata.setdefault("_extension_labels", []).append(
                {"entry_id": str(entry_id or ""), "label": label}
            )

        def _get_active_tools() -> list[str]:
            available = set(_available_tool_names())
            if agent_session is not None and agent_session.active_tool_names is not None:
                return [
                    name for name in agent_session.active_tool_names if name in available
                ]
            if hasattr(runner, "_pending_active_tools"):
                return [
                    str(name)
                    for name in getattr(runner, "_pending_active_tools") or []
                    if str(name) in available
                ]
            state = _published_runtime_state()
            if state is None:
                state = getattr(self, "_extension_runtime_states", {}).get(
                    conversation_id
                )
            if isinstance(state, dict) and "active_tool_names" in state:
                return [
                    str(name)
                    for name in state.get("active_tool_names") or []
                    if str(name) in available
                ]
            return _default_active_tool_names()

        def _get_all_tools() -> list[dict[str, Any]]:
            tools: list[dict[str, Any]] = []
            get_tool = getattr(tool_registry, "get_tool", None)
            source_info_for_tool = getattr(runner, "source_info_for_tool", None)
            for name in _available_tool_names():
                tool = get_tool(name) if callable(get_tool) else None
                if tool is None:
                    continue
                try:
                    schema = tool.model_schema() or tool.get_schema()
                    definition = getattr(tool, "definition", None)
                    raw_guidelines = getattr(
                        definition,
                        "prompt_guidelines",
                        getattr(tool, "prompt_guidelines", ()),
                    )
                    source_info = (
                        source_info_for_tool(name)
                        if callable(source_info_for_tool)
                        else None
                    )
                    if not isinstance(source_info, dict):
                        source_info = {
                            "path": f"<builtin:{name}>",
                            "source": "builtin",
                            "scope": "temporary",
                            "origin": "top-level",
                        }
                    tools.append(
                        {
                            "name": name,
                            "description": str(
                                getattr(schema, "description", "")
                                or getattr(tool, "description", "")
                                or ""
                            ),
                            "parameters": getattr(schema, "parameters", {"type": "object"}),
                            "promptGuidelines": (
                                [str(item) for item in raw_guidelines]
                                if raw_guidelines
                                else None
                            ),
                            "sourceInfo": source_info,
                        }
                    )
                except Exception:
                    continue
            return tools

        def _set_active_tools(names: Any) -> list[str]:
            requested = list(
                dict.fromkeys(
                    str(name).strip()
                    for name in (names or [])
                    if str(name).strip()
                )
            )
            available = set(_available_tool_names())
            selected = [name for name in requested if name in available]
            state = _published_runtime_state()
            if state is None:
                setattr(runner, "_pending_active_tools", selected)
            else:
                state["active_tool_names"] = selected
            if agent_session is not None:
                agent_session.active_tool_names = tuple(selected)
            return selected

        def _refresh_tools() -> None:
            before = set(_available_tool_names())
            override_existing = bool(
                getattr(runner, "_tool_bind_override_existing", True)
            )
            detach = getattr(runner, "detach_tool_registry", None)
            bind = getattr(runner, "bind_tool_registry", None)
            if not callable(detach) or not callable(bind):
                return
            detach()
            bind(tool_registry, override_existing=override_existing)
            after = _available_tool_names()
            added = [name for name in after if name not in before]
            if not added:
                return
            state = _published_runtime_state()
            if state is not None and "active_tool_names" in state:
                state["active_tool_names"] = list(
                    dict.fromkeys(
                        [*(state.get("active_tool_names") or []), *added]
                    )
                )
            elif hasattr(runner, "_pending_active_tools"):
                runner._pending_active_tools = list(
                    dict.fromkeys(
                        [*getattr(runner, "_pending_active_tools"), *added]
                    )
                )
            if agent_session is not None:
                current_active = list(agent_session.active_tool_names or ())
                agent_session.active_tool_names = tuple(
                    dict.fromkeys((*current_active, *added))
                )

        def _request_shutdown() -> bool:
            return self._schedule_extension_session_shutdown(
                conversation_id,
                runner,
            )

        def _get_commands() -> list[dict[str, Any]]:
            from backend.commands.catalog import get_enabled_composer_command_catalog

            extension_commands = [
                {
                    "name": command.name,
                    "description": str(command.description or ""),
                    "source": "extension",
                    "extension_path": command.extension_path,
                }
                for command in runner.get_commands()
            ]
            return [
                *extension_commands,
                *get_enabled_composer_command_catalog(
                    self._workspace_root_for_conversation(conversation),
                    resolve_active_workspace=False,
                ),
            ]

        async def _set_model(model: Any) -> bool:
            if isinstance(model, dict):
                provider_name = str(model.get("provider") or "").strip()
                model_name = str(
                    model.get("id")
                    or model.get("model")
                    or model.get("modelId")
                    or ""
                ).strip()
            else:
                provider_name = str(getattr(model, "provider", "") or "").strip()
                model_name = str(
                    getattr(model, "id", "")
                    or getattr(model, "model_id", "")
                    or model
                    or ""
                ).strip()
            if not provider_name:
                provider_name = str(getattr(self, "provider", "") or "").strip()
            resolved_model = (
                model_runtime.get_model(provider_name, model_name)
                if model_runtime is not None
                and callable(getattr(model_runtime, "get_model", None))
                else None
            )
            if (
                resolved_model is None
            ):
                return False
            # Provider/model identity is immutable for a live turn.  A model
            # switch requested by an extension is staged for the next turn so
            # one transcript never contains mixed provider semantics.
            turn_snapshot = run_metadata.get("_turn_model_snapshot")
            if isinstance(turn_snapshot, dict):
                current_identity = (
                    str(turn_snapshot.get("provider") or "").strip(),
                    str(turn_snapshot.get("model") or "").strip(),
                )
                requested_identity = (provider_name, model_name)
                if requested_identity != current_identity:
                    pending = getattr(self, "_queued_model_selections", None)
                    if not isinstance(pending, dict):
                        pending = self._queued_model_selections = {}
                    pending[conversation_id] = {
                        "provider": provider_name,
                        "model": model_name,
                    }
                    emit = getattr(runner, "emit", None)
                    if callable(emit):
                        await emit(
                            {
                                "type": "model_select",
                                "model": {"provider": provider_name, "id": model_name},
                                "source": "queued_next_turn",
                            }
                        )
                    return True
            published_state = getattr(self, "_extension_runtime_states", {}).get(
                conversation_id
            )
            if (
                not isinstance(published_state, LifecycleGenerationState)
                or not published_state.is_current(runner)
                or published_state.get("model_runtime") is not model_runtime
            ):
                # Candidate session_start runs before the atomic generation
                # swap. Validate now, then apply only after publication so a
                # later bind failure cannot mutate the live provider/model.
                setattr(runner, "_pending_model_selection", resolved_model)
                return True
            setter = getattr(self, "_set_selected_provider_model", None)
            if not callable(setter):
                return False
            previous_provider = str(getattr(self, "provider", "") or "")
            previous_model = str(getattr(self, "selected_model", "") or "")
            previous_runtime_model = (
                model_runtime.get_model(previous_provider, previous_model)
                if model_runtime is not None
                else None
            )
            previous_thinking = _get_thinking_level()
            changed = await setter(
                provider_name,
                model_name,
                manual_override=True,
                model_runtime=model_runtime,
                emit_unavailable=False,
            )
            if not changed:
                return False
            selected_config = getattr(self, "config", None)
            selected_budget = getattr(selected_config, "token_budget", None)
            if selected_budget is not None:
                run_context_builder._budget = selected_budget
            active_llm = getattr(self, "llm", None)
            if active_llm is None:
                return False
            if agent_session is not None:
                agent_session.llm = active_llm
                agent_session.token_budget = selected_budget or agent_session.token_budget
                agent_session.agent_settings = getattr(
                    selected_config,
                    "agent",
                    agent_session.agent_settings,
                )
            bind_llm = getattr(run_context_builder, "bind_llm", None)
            if callable(bind_llm):
                bind_llm(active_llm)
            # Match MiniCode AgentSession._getThinkingLevelForModelSwitch(): a
            # reasoning-capable current model carries its live session level
            # across the switch; a non-reasoning current model restores the
            # configured default (medium when unset) before target clamping.
            thinking_seed = (
                previous_thinking
                if bool(getattr(previous_runtime_model, "reasoning", False))
                else str(
                    getattr(getattr(selected_config, "llm", None), "reasoning_effort", "")
                    or "medium"
                ).strip().lower()
            )
            effective_thinking = _apply_thinking_level(
                active_llm,
                resolved_model,
                thinking_seed,
            )
            run_metadata["_extension_thinking_level"] = effective_thinking
            parent_runtime = run_metadata.get("_subagent_parent_runtime")
            if isinstance(parent_runtime, dict):
                parent_runtime.update(
                    {
                        "config": selected_config,
                        "provider": str(getattr(self, "provider", "") or ""),
                        "model": str(getattr(self, "selected_model", "") or ""),
                        "model_runtime": model_runtime,
                        "available_models": tuple(
                            model.id
                            for model in model_runtime.get_models(
                                str(getattr(self, "provider", "") or "")
                            )
                        )
                        if model_runtime is not None
                        else tuple(getattr(self, "available_models", ()) or ()),
                        "llm": active_llm,
                        "thinking_level": effective_thinking or "off",
                    }
                )
            emit = getattr(runner, "emit", None)
            current_provider = str(getattr(self, "provider", "") or "")
            current_model = str(getattr(self, "selected_model", "") or "")
            if callable(emit) and (
                previous_provider != current_provider or previous_model != current_model
            ):
                await emit(
                    {
                        "type": "model_select",
                        "model": {
                            "provider": current_provider,
                            "id": current_model,
                        },
                        "previousModel": {
                            "provider": previous_provider,
                            "id": previous_model,
                        },
                        "source": "set",
                    }
                )
            return True

        def _get_thinking_level() -> str:
            llm_config = getattr(getattr(self, "config", None), "llm", None)
            return str(
                run_metadata.get("_extension_thinking_level")
                or getattr(llm_config, "reasoning_effort", "")
                or "off"
            )

        def _set_thinking_level(level: Any) -> str:
            previous = _get_thinking_level()
            active_llm = (
                agent_session.llm
                if agent_session is not None
                else getattr(self, "llm", None) or run_llm
            )
            current_provider = str(getattr(self, "provider", "") or "")
            current_model = str(getattr(self, "selected_model", "") or "")
            selected_runtime_model = (
                model_runtime.get_model(current_provider, current_model)
                if model_runtime is not None
                else None
            )
            value = _apply_thinking_level(
                active_llm,
                selected_runtime_model,
                level,
            )
            run_metadata["_extension_thinking_level"] = value
            parent_runtime = run_metadata.get("_subagent_parent_runtime")
            if isinstance(parent_runtime, dict):
                parent_runtime["llm"] = active_llm
                parent_runtime["thinking_level"] = value or "off"
            emit = getattr(runner, "emit", None)
            if callable(emit) and previous != value:
                try:
                    asyncio.create_task(
                        emit(
                            {
                                "type": "thinking_level_select",
                                "level": value,
                                "previousLevel": previous,
                            }
                        )
                    )
                except RuntimeError:
                    pass
            return value

        async def _exec(command: Any, args: Any = None, options: Any = None) -> Any:
            """Run extension commands through MiniCode's canonical shell tool.

            Extensions never receive a raw subprocess capability.  The live
            turn context is installed by loop bootstrap and carries the
            permission checker, sandbox policy, cancellation event, deadline,
            artifact store, and ownership metadata used by ``run_command``.
            Approval-required requests fail closed here because an extension
            action has no model tool-call receipt to attach to the websocket
            approval protocol.
            """
            from uuid import uuid4

            from backend.agent.context import clone_context_builder
            from backend.agent.state import AgentState
            from backend.agent.tool_execution import execute_tool_batch
            from backend.llm.base import ToolCallEvent
            from backend.tools.base import ToolResult

            tool_context = run_metadata.get("_tool_execution_context")
            if tool_context is None:
                raise RuntimeError("extension exec is only available during an active MiniCode turn")
            command_text = str(command or "").strip()
            if not command_text:
                return ToolResult(content="Missing command", is_error=True, status="failed")
            argv = [str(item) for item in (args or ())]
            if argv:
                import os
                import shlex

                command_text = (
                    subprocess.list2cmdline([command_text, *argv])
                    if os.name == "nt"
                    else shlex.join([command_text, *argv])
                )
            options_map = dict(options) if isinstance(options, dict) else {}
            request_args = {"command": command_text, **options_map}
            registry = tool_registry
            tool = registry.get_tool("run_command")
            if tool is None:
                raise RuntimeError("MiniCode run_command capability is unavailable")
            permission_context = getattr(tool_context, "permission", None)
            checker = getattr(tool_context, "permission_checker", None) or getattr(
                self, "permission_checker", None
            )
            metadata = getattr(tool_context, "metadata", {})
            parent_context = metadata.get("_context_builder") if isinstance(metadata, dict) else None
            parent_state = metadata.get("_agent_state") if isinstance(metadata, dict) else None
            if parent_context is None or parent_state is None or checker is None:
                raise RuntimeError("extension exec is missing canonical tool execution bindings")
            command_context = clone_context_builder(parent_context)
            command_state = AgentState(
                user_message=f"extension exec: {command_text}",
                max_iterations=max(1, int(getattr(parent_state, "max_iterations", 1) or 1)),
            )
            call_id = f"extension_exec_{uuid4().hex}"
            result_event = None
            batch = execute_tool_batch(
                [ToolCallEvent(id=call_id, name="run_command", arguments=request_args)],
                ctx=command_context,
                state=command_state,
                tool_registry=registry,
                permission_checker=checker,
                approval_handler=None,
                skill_manager=None,
                permission_context=permission_context,
                tool_ctx=tool_context,
            )
            try:
                async for event in batch:
                    if event.type == "tool_result" and str(event.data.get("id") or "") == call_id:
                        result_event = event
            finally:
                await batch.aclose()
            if result_event is None:
                return ToolResult(
                    content="Extension command ended without a canonical tool result.",
                    is_error=True,
                    status="failed",
                )
            return ToolResult(
                content=str(result_event.data.get("summary") or ""),
                is_error=bool(result_event.data.get("is_error")),
                status=str(result_event.data.get("status") or ""),
                artifact_id=str(result_event.data.get("artifact_id") or "") or None,
                duration_ms=result_event.data.get("duration_ms"),
                display_summary=str(result_event.data.get("display_summary") or ""),
                result_kind=str(result_event.data.get("result_kind") or "") or None,
                limitation=str(result_event.data.get("limitation") or "") or None,
                request_digest=str(result_event.data.get("request_digest") or ""),
                cleanup_receipt=dict(result_event.data.get("cleanup_receipt") or {}),
            )

        actions = {
            "send_message": _send_message,
            "send_user_message": _send_user_message,
            "append_entry": _append_entry,
            "set_session_name": _set_session_name,
            "get_session_name": _get_session_name,
            "set_label": _set_label,
            "get_active_tools": _get_active_tools,
            "get_all_tools": _get_all_tools,
            "set_active_tools": _set_active_tools,
            "get_commands": _get_commands,
            "set_model": _set_model,
            "get_thinking_level": _get_thinking_level,
            "set_thinking_level": _set_thinking_level,
            "refresh_tools": _refresh_tools,
            "shutdown": _request_shutdown,
            "exec": _exec,
        }
        if callable(bind_actions):
            bind_actions(actions)

        async def _wait_for_idle() -> None:
            task = getattr(self, "_conversation_run_tasks", {}).get(conversation_id)
            if isinstance(task, asyncio.Task) and not task.done():
                if task is asyncio.current_task():
                    raise RuntimeError(
                        "An extension cannot wait for the agent turn that is currently executing it"
                    )
                await asyncio.shield(task)

        async def _reload_extensions() -> None:
            nonlocal tool_registry
            task = getattr(self, "_conversation_run_tasks", {}).get(conversation_id)
            if isinstance(task, asyncio.Task) and not task.done():
                raise RuntimeError(
                    "Extension reload requires an idle conversation; await wait_for_idle() from a command context"
                )
            discover_skills = getattr(self.skill_manager, "discover", None)
            if callable(discover_skills):
                discover_skills()
            from backend.commands.slash_commands import refresh_slash_commands

            refresh_slash_commands(self.command_registry)
            tool_registry = self._conversation_tool_registry(
                conversation_id,
                workspace_root=self._workspace_root_for_conversation(conversation),
                force_rebuild=True,
            )
            refreshed = await self._ensure_lifecycle_runtime(
                conversation_id=conversation_id,
                workspace_root=self._workspace_root_for_conversation(conversation),
                tool_registry=tool_registry,
                force_reload=True,
                reload_reason="reload",
            )
            if refreshed is None:
                raise RuntimeError("Extension reload did not produce an active runtime")
            if agent_session is not None and getattr(self, "llm", None) is not None:
                agent_session.llm = self.llm
                selected_config = getattr(self, "config", None)
                selected_budget = getattr(selected_config, "token_budget", None)
                if selected_budget is not None:
                    agent_session.token_budget = selected_budget
                    run_context_builder._budget = selected_budget
                bind_llm = getattr(run_context_builder, "bind_llm", None)
                if callable(bind_llm):
                    bind_llm(self.llm)
            tool_registry = (
                self._extension_runtime_state(conversation_id).get("registry")
                or tool_registry
            )
            if agent_session is not None:
                agent_session.tool_registry = tool_registry
                available_after_reload = set(_activatable_tool_names(tool_registry))
                published_after_reload = self._extension_runtime_state(
                    conversation_id
                ).get("active_tool_names")
                if isinstance(published_after_reload, list):
                    agent_session.active_tool_names = tuple(
                        str(name)
                        for name in published_after_reload
                        if str(name) in available_after_reload
                    )
            # The new MiniCode generation owns a fresh runtime.  Rebind the host
            # projections that were intentionally absent during factory load.
            self._bind_lifecycle_runtime_host_actions(
                refreshed,
                conversation=conversation,
                tool_registry=tool_registry,
                run_metadata=run_metadata,
                run_context_builder=run_context_builder,
                run_llm=run_llm,
                cancel_event=cancel_event,
                model_runtime=model_runtime,
                model_registry=model_registry,
                agent_session=agent_session,
            )
            self._mark_lifecycle_runtime_host_actions_bound(
                conversation_id,
                refreshed,
            )
            from backend.commands.catalog import get_enabled_composer_command_catalog

            await self._send_ws_payload(
                {
                    "type": "commands.list",
                    "conversation_id": conversation_id,
                    "commands": [
                        *self.command_registry.list_extension_slash_commands(
                            scope_id=conversation_id
                        ),
                        *get_enabled_composer_command_catalog(
                            self._workspace_root_for_conversation(conversation),
                            resolve_active_workspace=False,
                        ),
                    ],
                },
                log_context="commands.list",
            )
            send_runtime_capabilities = getattr(
                self,
                "_send_runtime_capabilities",
                None,
            )
            if callable(send_runtime_capabilities):
                await send_runtime_capabilities(source="extension.reload")

        async def _compact(options: Any = None) -> Any:
            from backend.llm.base import LLMAdapter

            active_llm = (
                agent_session.llm
                if agent_session is not None
                else getattr(self, "llm", None) or run_llm
            )
            bind_llm = getattr(run_context_builder, "bind_llm", None)
            if callable(bind_llm):
                bind_llm(active_llm)
            _lease_session_llm_for_current_task(self, active_llm)
            provider_hook_token = LLMAdapter.bind_provider_lifecycle_runtime(runner)
            try:
                options_map = options if isinstance(options, dict) else {}
                # MiniCode's compact options carry customInstructions
                # (agent-harness.ts); accept both it and the legacy focus key.
                focus = (
                    str(options_map.get("customInstructions") or "").strip()
                    or str(options_map.get("focus") or "").strip()
                    or None
                )
                return await run_context_builder.compact(focus=focus)
            finally:
                LLMAdapter.unbind_provider_lifecycle_runtime(provider_hook_token)

        context_actions = {
            "model": lambda: (
                model_runtime.get_model(
                    str(getattr(self, "provider", "") or ""),
                    str(getattr(self, "selected_model", "") or ""),
                )
                if model_runtime is not None
                else None
            ),
            "session_manager": lambda: self,
            "model_registry": lambda: model_registry,
            "is_idle": lambda: not bool(
                isinstance(
                    getattr(self, "_conversation_run_tasks", {}).get(conversation_id),
                    asyncio.Task,
                )
                and not getattr(self, "_conversation_run_tasks", {})[conversation_id].done()
            ),
            "is_project_trusted": lambda: bool(
                self._workspace_root_for_conversation(conversation)
                and is_workspace_trusted(
                    self._workspace_root_for_conversation(conversation)
                )
            ),
            "abort": lambda: cancel_event.set() if isinstance(cancel_event, asyncio.Event) else None,
            "has_pending_messages": lambda: bool(
                run_manager
                and run_manager.pending_turn_input_snapshot()
            ),
            "get_context_usage": lambda: getattr(run_context_builder, "context_usage", lambda: {})(),
            "compact": _compact,
            "get_system_prompt": lambda: str(
                run_metadata.get("_extension_system_prompt") or ""
            ),
            "get_system_prompt_options": lambda: {
                "cwd": str(
                    self._workspace_root_for_conversation(conversation)
                    or Path.cwd()
                )
            },
            "wait_for_idle": _wait_for_idle,
            "reload": _reload_extensions,
        }
        if callable(bind_context_actions):
            bind_context_actions(context_actions)

    def _mark_lifecycle_runtime_host_actions_bound(
        self,
        conversation_id: str,
        runner: Any,
    ) -> None:
        """Mark only the still-published MiniCode generation as fully host-bound."""

        states = getattr(self, "_extension_runtime_states", None)
        if not isinstance(states, dict):
            return
        state = states.get(str(conversation_id or "").strip())
        if isinstance(state, LifecycleGenerationState) and state.is_current(runner):
            state["host_actions_bound"] = True

    def _bind_lifecycle_runtime_command_snapshot(
        self,
        runner: Any,
        *,
        conversation: Any,
        tool_registry: Any,
        model_runtime: Any | None = None,
        model_registry: Any | None = None,
    ) -> None:
        """Bind a conversation-owned MiniCode command context outside an Agent turn.

        Pi constructs and binds the replacement AgentSession runtime before
        publishing its command surface.  MiniCode keeps the same ownership
        boundary by giving a candidate runner an immutable conversation
        snapshot; an active turn may immediately replace this with its exact
        live ContextBuilder after publication.
        """

        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not conversation_id:
            raise ValueError("conversation id is required to bind extension actions")
        workspace_root = self._workspace_root_for_conversation(conversation)
        config = _config_with_runtime_model_budget(
            load_config(),
            model_runtime=model_runtime,
            provider=str(getattr(self, "provider", "") or ""),
            model=str(getattr(self, "selected_model", "") or ""),
        )
        run_llm = getattr(self, "llm", None)
        run_memory_manager = getattr(self, "memory_manager", None)
        if workspace_root is not None:
            from backend.memory.file_memory import FileMemory
            from backend.memory.manager import MemoryManager

            run_memory_manager = MemoryManager(
                FileMemory.for_workspace(workspace_root)
            )
        context_builder = ContextBuilder(
            token_budget=config.token_budget,
            agent_settings=config.agent,
            skill_executor=getattr(self, "skill_executor", None),
            memory_manager=run_memory_manager,
            llm=run_llm,
            skill_manager=self.skill_manager,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        context_builder.load_snapshot(
            dict(getattr(conversation, "context_snapshot", {}) or {})
        )
        cancel_event = getattr(self, "_conversation_run_cancel_events", {}).get(
            conversation_id
        )
        self._bind_lifecycle_runtime_host_actions(
            runner,
            conversation=conversation,
            tool_registry=tool_registry,
            run_metadata={
                "conversation_id": conversation_id,
                "_extension_command_context": True,
            },
            run_context_builder=context_builder,
            run_llm=run_llm,
            cancel_event=(
                cancel_event if isinstance(cancel_event, asyncio.Event) else None
            ),
            model_runtime=model_runtime,
            model_registry=model_registry,
        )

    async def _ensure_extension_commands_for_conversation(
        self, conversation_id: str
    ) -> Any | None:
        """Materialize the MiniCode command surface before slash dispatch/catalog reads."""

        clean_id = str(conversation_id or "").strip()
        if not clean_id:
            return None
        conversation = self.conversation_repo.get_conversation(clean_id)
        if conversation is None:
            return None
        refresh = getattr(self, "refresh_tool_registry_if_mcp_changed", None)
        if callable(refresh):
            refresh(allow_when_busy=False)
        workspace_root = self._workspace_root_for_conversation(conversation)
        tool_registry = self._conversation_tool_registry(
            clean_id,
            workspace_root=workspace_root,
        )
        runner = await self._ensure_lifecycle_runtime(
            conversation_id=clean_id,
            workspace_root=workspace_root,
            tool_registry=tool_registry,
        )
        if runner is None:
            return None
        tool_registry = (
            self._extension_runtime_state(clean_id).get("registry") or tool_registry
        )

        state = self._extension_runtime_state(clean_id)
        task = getattr(self, "_conversation_run_tasks", {}).get(clean_id)
        if (
            isinstance(task, asyncio.Task)
            and not task.done()
            and state.is_current(runner)
            and bool(state.get("host_actions_bound"))
        ):
            # The active turn already installed the exact AgentSession binding.
            # A freshly reloaded generation is also pre-bound to its own
            # command snapshot before publication.  Do not overwrite either
            # context while streaming.
            return runner

        self._bind_lifecycle_runtime_command_snapshot(
            runner,
            conversation=conversation,
            tool_registry=tool_registry,
        )
        self._mark_lifecycle_runtime_host_actions_bound(clean_id, runner)
        return runner

    async def refresh_plugin_runtime_state(
        self,
        *,
        reason: str = "plugin.settings",
    ) -> dict[str, Any]:
        """Atomically refresh conversation-owned Hook and MiniCode generations.

        Claude Code publishes a freshly loaded plugin component snapshot only
        after discovery succeeds.  Pi rebuilds the complete extension runner
        and tool registry on reload.  This session-level refresh composes those
        two lifecycle rules without interrupting an in-flight Agent turn: the
        turn keeps its captured generation, while commands and the next turn
        observe the newly published one.
        """

        refresh_reason = str(reason or "plugin.settings").strip() or "plugin.settings"
        report: dict[str, Any] = {
            "ok": True,
            "reason": refresh_reason,
            "refreshed_hooks": [],
            "reloaded_extensions": [],
            "deferred_retirements": [],
            "warnings": [],
            "errors": [],
        }

        from backend.config import load_config_layer_stack
        from backend.hooks.manager import (
            iter_hook_managers_for_owner,
            load_hook_manager_for_workspace,
            register_hook_manager_for_session,
        )

        hook_scopes = iter_hook_managers_for_owner(
            str(getattr(self, "session_id", "") or "")
        )
        for scope_id, previous in hook_scopes:
            try:
                conversation = self.conversation_repo.get_conversation(scope_id)
                workspace_root = (
                    self._workspace_root_for_conversation(conversation)
                    if conversation is not None
                    else getattr(previous, "workspace_root", None)
                )
                config_stack = load_config_layer_stack(cwd=workspace_root)
                refreshed = load_hook_manager_for_workspace(
                    workspace_root,
                    config_layer_stack=config_stack,
                    session_id=scope_id,
                )
                register_hook_manager_for_session(
                    scope_id,
                    refreshed,
                    owner_session_id=str(getattr(self, "session_id", "") or ""),
                )
                report["refreshed_hooks"].append(
                    {
                        "scope_id": scope_id,
                        "workspace_root": (
                            str(workspace_root) if workspace_root is not None else ""
                        ),
                        "fingerprint": str(
                            getattr(refreshed, "registry_fingerprint", "") or ""
                        ),
                    }
                )
            except Exception as exc:
                report["ok"] = False
                report["errors"].append(
                    f"Hook refresh failed for {scope_id}: {exc}"
                )
                logger.exception(
                    "Failed to refresh Claude hook generation for session %s scope %s",
                    getattr(self, "session_id", ""),
                    scope_id,
                )

        extension_scope_ids = list(
            str(scope_id or "").strip()
            for scope_id in getattr(self, "_extension_runtime_states", {})
            if str(scope_id or "").strip()
        )
        active_conversation_id = str(
            getattr(self, "active_conversation_id", "") or ""
        ).strip()
        if (
            active_conversation_id
            and active_conversation_id not in extension_scope_ids
        ):
            extension_scope_ids.append(active_conversation_id)

        for conversation_id in extension_scope_ids:
            conversation = self.conversation_repo.get_conversation(conversation_id)
            if conversation is None:
                report["warnings"].append(
                    f"Skipped extension refresh for missing conversation {conversation_id}"
                )
                continue
            workspace_root = self._workspace_root_for_conversation(conversation)
            state = self._extension_runtime_state(conversation_id)
            previous_runner = state.runtime
            running_task = getattr(self, "_conversation_run_tasks", {}).get(
                conversation_id
            )
            defer_until = (
                running_task
                if isinstance(running_task, asyncio.Task) and not running_task.done()
                else None
            )
            try:
                tool_registry = self._conversation_tool_registry(
                    conversation_id,
                    workspace_root=workspace_root,
                )
                refreshed_runner = await self._ensure_lifecycle_runtime(
                    conversation_id=conversation_id,
                    workspace_root=workspace_root,
                    tool_registry=tool_registry,
                    force_reload=True,
                    reload_reason=refresh_reason,
                    defer_previous_shutdown_until=defer_until,
                )
                current_state = self._extension_runtime_state(conversation_id)
                current_runner = current_state.runtime
                if refreshed_runner is None or current_runner is None:
                    raise RuntimeError("candidate did not produce an active extension runner")
                if previous_runner is not None and current_runner is previous_runner:
                    # Candidate discovery/binding failed. The lifecycle owner
                    # intentionally kept the last known-good generation.
                    report["ok"] = False
                    report["errors"].append(
                        "Extension refresh failed for "
                        f"{conversation_id}; retained the last known-good generation"
                    )
                    continue
                entry = {
                    "conversation_id": conversation_id,
                    "workspace_root": (
                        str(workspace_root) if workspace_root is not None else ""
                    ),
                    "generation": int(
                        getattr(current_runner, "generation", 0) or 0
                    ),
                    "deferred_previous_shutdown": bool(
                        defer_until is not None and previous_runner is not None
                    ),
                }
                report["reloaded_extensions"].append(entry)
                if entry["deferred_previous_shutdown"]:
                    report["deferred_retirements"].append(conversation_id)
                load_result = current_state.get("result")
                for load_error in list(getattr(load_result, "errors", []) or []):
                    if not isinstance(load_error, dict):
                        continue
                    report["warnings"].append(
                        "Extension source unavailable for "
                        f"{conversation_id} ({load_error.get('path', '<unknown>')}): "
                        f"{load_error.get('error', 'unknown error')}"
                    )
            except Exception as exc:
                report["ok"] = False
                report["errors"].append(
                    f"Extension refresh failed for {conversation_id}: {exc}"
                )
                logger.exception(
                    "Failed to refresh MiniCode extension generation for session %s conversation %s",
                    getattr(self, "session_id", ""),
                    conversation_id,
                )

        if active_conversation_id and bool(getattr(self, "_is_connected", False)):
            try:
                from backend.commands.catalog import (
                    get_enabled_composer_command_catalog,
                )

                active_conversation = self.conversation_repo.get_conversation(
                    active_conversation_id
                )
                active_workspace_root = (
                    self._workspace_root_for_conversation(active_conversation)
                    if active_conversation is not None
                    else None
                )
                await self._send_ws_payload(
                    {
                        "type": "commands.list",
                        "conversation_id": active_conversation_id,
                        "commands": [
                            *self.command_registry.list_extension_slash_commands(
                                scope_id=active_conversation_id
                            ),
                            *get_enabled_composer_command_catalog(
                                active_workspace_root,
                                resolve_active_workspace=False,
                            ),
                        ],
                    },
                    log_context="commands.list",
                )
                send_runtime_capabilities = getattr(
                    self,
                    "_send_runtime_capabilities",
                    None,
                )
                if callable(send_runtime_capabilities):
                    await send_runtime_capabilities(source=refresh_reason)
            except Exception as exc:
                report["ok"] = False
                report["errors"].append(
                    f"Runtime capability projection failed: {exc}"
                )
                logger.exception(
                    "Failed to project refreshed plugin runtime for session %s",
                    getattr(self, "session_id", ""),
                )

        return report

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
        # _start_agent_run already copied caller metadata for this task. Keep
        # this exact object so admission facts (run_id/runtime) remain visible
        # to the scheduler if setup fails before QueryEngine takes over.
        run_metadata = metadata if isinstance(metadata, dict) else {}
        owner_id = ":".join(
            part
            for part in (
                "ws",
                str(getattr(self, "session_id", "") or ""),
                str(run_metadata.get("_run_task_id") or ""),
            )
            if part
        )
        query_guards = conversation_query_guards()
        query_claim = query_guards.try_start(
            target_conversation_id,
            owner_id=owner_id,
        )
        if query_claim is None:
            active_claim = query_guards.active_claim(target_conversation_id)
            message_id = _client_assistant_message_id(run_metadata)
            await self._send_event(AgentEvent(
                type="error",
                data={
                    "message": "This conversation already has an active turn. Queue the message as steer/follow-up or wait for completion.",
                    "recoverable": True,
                    "error_type": "conversation_busy",
                    "conversation_id": target_conversation_id,
                    "active_generation": active_claim.generation if active_claim else 0,
                    **({"message_id": message_id} if message_id else {}),
                },
            ))
            done_event = AgentEvent.done(
                status="failed",
                reason="conversation_busy",
            )
            done_event.data["conversation_id"] = target_conversation_id
            if message_id:
                done_event.data["message_id"] = message_id
            await self._send_event(done_event)
            run_manager = getattr(self, "_run_manager", None)
            if run_manager is not None:
                run_manager.mark_terminal_status(target_conversation_id, "failed")
                run_manager.mark_delivery_complete(
                    target_conversation_id,
                    str(run_metadata.get("_run_task_id") or ""),
                )
            return
        try:
            run_metadata["conversation_run_generation"] = query_claim.generation
            await self._run_agent_locked(
                user_message,
                attachments=attachments,
                conversation_id=target_conversation_id or None,
                metadata=run_metadata,
                cancel_event=cancel_event,
                query_claim=query_claim,
            )
            if not str(run_metadata.get("run_id") or "").strip():
                run_manager = getattr(self, "_run_manager", None)
                delivery_complete = bool(
                    run_manager is not None
                    and run_manager.is_delivery_complete(
                        target_conversation_id,
                        str(run_metadata.get("_run_task_id") or ""),
                    )
                )
                if not delivery_complete:
                    message_id = _client_assistant_message_id(run_metadata)
                    done_event = AgentEvent.done(
                        status="failed",
                        reason="startup_rejected",
                    )
                    done_event.data["conversation_id"] = target_conversation_id
                    if message_id:
                        done_event.data["message_id"] = message_id
                    await self._send_event(done_event)
                    await self._send_event(
                        AgentEvent.session_state_changed(
                            state="idle",
                            conversation_id=target_conversation_id,
                            reason="startup_rejected",
                        )
                    )
                    if run_manager is not None:
                        run_manager.mark_terminal_status(
                            target_conversation_id,
                            "failed",
                        )
                        run_manager.mark_delivery_complete(
                            target_conversation_id,
                            str(run_metadata.get("_run_task_id") or ""),
                        )
        except asyncio.CancelledError:
            # The pre-admission setup zone (MCP refresh, conversation lookup,
            # hydration, registry and lifecycle construction) runs before
            # QueryEngine owns a terminal transaction, and ``except Exception``
            # does not cover cancellation. Without this branch a stop during
            # setup leaves the client with no terminal event at all: the
            # scheduler fallback declines to fabricate one for a run that was
            # never durably admitted, so the conversation stays busy forever.
            if not str(run_metadata.get("run_id") or "").strip():
                message_id = _client_assistant_message_id(run_metadata)
                done_event = AgentEvent.done(
                    status="cancelled",
                    reason="startup_cancelled",
                )
                done_event.data["conversation_id"] = target_conversation_id
                if message_id:
                    done_event.data["message_id"] = message_id
                try:
                    await self._send_event(done_event)
                    await self._send_event(
                        AgentEvent.session_state_changed(
                            state="idle",
                            conversation_id=target_conversation_id,
                            reason="startup_cancelled",
                        )
                    )
                finally:
                    run_manager = getattr(self, "_run_manager", None)
                    if run_manager is not None:
                        run_manager.mark_terminal_status(
                            target_conversation_id,
                            "cancelled",
                        )
                        run_manager.mark_delivery_complete(
                            target_conversation_id,
                            str(run_metadata.get("_run_task_id") or ""),
                        )
            raise
        except Exception as exc:
            # Failures before durable admission (conversation lookup, registry
            # construction, hydration) cannot reach QueryEngine's terminal
            # transaction. Close the accepted scheduler task here so the UI
            # never receives only a recoverable error and remains busy forever.
            if not str(run_metadata.get("run_id") or "").strip():
                logger.exception(
                    "Agent turn setup failed before admission for session %s conversation %s",
                    getattr(self, "session_id", ""),
                    target_conversation_id,
                )
                message_id = _client_assistant_message_id(run_metadata)
                error_event = AgentEvent.error(
                    # The cause is the only actionable part of a pre-admission
                    # failure: without it the user sees a dead turn and no reason.
                    f"MiniCode could not initialize the agent turn: {exc}",
                    recoverable=False,
                    error_type="startup",
                    error_code="startup_failed",
                )
                error_event.data["conversation_id"] = target_conversation_id
                if message_id:
                    error_event.data["message_id"] = message_id
                done_event = AgentEvent.done(
                    status="failed",
                    reason="startup_failed",
                )
                done_event.data["conversation_id"] = target_conversation_id
                if message_id:
                    done_event.data["message_id"] = message_id
                try:
                    await self._send_event(error_event)
                    await self._send_event(done_event)
                    await self._send_event(
                        AgentEvent.session_state_changed(
                            state="idle",
                            conversation_id=target_conversation_id,
                            reason="startup_failed",
                        )
                    )
                finally:
                    run_manager = getattr(self, "_run_manager", None)
                    if run_manager is not None:
                        run_manager.mark_terminal_status(
                            target_conversation_id,
                            "failed",
                        )
                        run_manager.mark_delivery_complete(
                            target_conversation_id,
                            str(run_metadata.get("_run_task_id") or ""),
                        )
                return
            raise
        finally:
            query_guards.end(query_claim)

    async def _run_agent_locked(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        query_claim: ConversationQueryClaim | None = None,
    ) -> None:
        run_interrupted = False
        run_cancel_event = cancel_event or asyncio.Event()
        # Keep the caller-owned metadata mapping so durable admission identity
        # and scheduler fallback fields remain visible to the task owner.
        run_metadata = metadata if isinstance(metadata, dict) else {}
        run_task_id = str(run_metadata.get("_run_task_id") or "").strip()
        # Hot-reload any MCP tools connected since this session was created.
        # The conversation-owned AgentSession registry below is rebuilt from
        # that generation while in-flight conversations retain their snapshot.
        self.refresh_tool_registry_if_mcp_changed()
        target_conversation_id = conversation_id or self.active_conversation_id or ""
        if not target_conversation_id:
            self._ensure_active_conversation()
            target_conversation_id = self.active_conversation_id or ""
        conversation = self.conversation_repo.get_conversation(target_conversation_id)
        if conversation is None:
            await emit_conversation_not_found(self, target_conversation_id)
            return

        # A restored conversation may have only its newest entries loaded
        # synchronously.  Do not construct or send a provider request until
        # the durable older prefix has been hydrated.
        conversation_runtime = getattr(self, "conversation_runtime", None)
        wait_for_hydration = getattr(conversation_runtime, "wait_for_hydration", None)
        if callable(wait_for_hydration):
            await wait_for_hydration(conversation.id)

        def _owns_query_claim() -> bool:
            return query_claim is None or conversation_query_guards().owns(query_claim)

        terminal_projection_started = False

        def _accepts_projection_events() -> bool:
            return _owns_query_claim() and not terminal_projection_started

        # Establish durable run ownership before the first new-turn mutation.
        # Recovery must publish any prior terminal fact before this turn resets
        # the UI projection or writes its user message.
        assistant_message_id = _client_assistant_message_id(metadata) or f"assistant_{uuid.uuid4().hex[:8]}"
        admission_runtime = default_runtime()
        admission_record = admission_runtime.start_run(
            conversation_id=conversation.id,
            parent_run_id=str(run_metadata.get("parent_run_id") or ""),
            role=str(run_metadata.get("agent_role") or "main"),
            task_id=run_task_id,
            session_id=str(getattr(self, "session_id", "") or ""),
            budget=getattr(getattr(self, "config", None), "token_budget", None),
            run_id=str(run_metadata.get("run_id") or "") or None,
            mailbox_epoch=int(run_metadata.get("mailbox_epoch") or 0),
        )
        run_metadata["agent_runtime"] = admission_runtime
        run_metadata["run_id"] = admission_record.run_id
        run_metadata["conversation_id"] = conversation.id
        journal_owner = execution_journal_owner(
            "conversation",
            conversation.id,
        )
        execution_journal = admission_runtime.execution_journal(journal_owner)
        run_metadata["_execution_journal"] = execution_journal

        run_workspace_root = self._workspace_root_for_conversation(conversation)
        run_tool_registry = self._conversation_tool_registry(
            conversation.id,
            workspace_root=run_workspace_root,
        )
        previous_turn_aborted = _consume_previous_turn_aborted(self, conversation.id)
        is_active_conversation_run = conversation.id == self.active_conversation_id
        async with self._conversation_projection_lock(conversation.id):
            await _replay_pending_conversation_projections(
                self.conversation_repo,
                execution_journal,
                conversation_id=conversation.id,
            )
            await self._flush_ui_agent_state_now_unlocked(conversation.id)
            getattr(self, "_ui_agent_state_cache", {}).pop(conversation.id, None)
            latest_for_turn = await asyncio.to_thread(
                self.conversation_repo.get_conversation,
                conversation.id,
            )
            if latest_for_turn is None:
                raise RuntimeError(
                    "conversation disappeared while initializing a new turn"
                )
            conversation = latest_for_turn
            run_context_snapshot = _reset_ui_agent_state_snapshot(
                conversation.context_snapshot
            )
            await asyncio.to_thread(
                self.conversation_repo.save_context_snapshot,
                conversation.id,
                run_context_snapshot,
            )

        # Track active streaming metadata for reconnection recovery. Prefer
        # the client-created assistant placeholder id so late events from an
        # old turn cannot attach to a newer assistant message.
        stream_state = create_stream_state(conversation.id, assistant_message_id)
        getattr(self, "_conversation_streams", {})[conversation.id] = stream_state

        # Extension providers must enter ModelRuntime before turn-model
        # resolution. Loading them after adapter creation makes a registered
        # provider unusable on the first normal prompt and gives setModel a
        # registry that is one turn behind.
        lifecycle_runtime = await self._ensure_lifecycle_runtime(
            conversation_id=conversation.id,
            workspace_root=run_workspace_root,
            tool_registry=run_tool_registry,
        )
        run_model_runtime = self._model_runtime_for_conversation(conversation.id)
        if lifecycle_runtime is not None:
            run_tool_registry = (
                self._extension_runtime_state(conversation.id).get("registry")
                or run_tool_registry
            )

        # Each run uses a local LLM/provider/model/config snapshot to avoid overwriting
        # session fields that concurrent runs might be reading
        try:
            run_config = load_config(cwd=run_workspace_root)
            run_settings = (
                run_config.config_layer_stack.effective_config()
                if run_config.config_layer_stack is not None
                else None
            )
            provider_resolver = getattr(self, "_resolve_llm_provider", get_llm_provider)
            models_resolver = getattr(self, "_resolve_available_models", get_available_models)
            models_source_resolver = getattr(self, "_resolve_models_source", None)
            config_provider = str(getattr(run_config.llm, "provider", "") or "").strip().lower()
            resolved_provider = str(
                provider_resolver(run_settings)
                if _resolver_accepts_positional_arguments(
                    provider_resolver, run_settings
                )
                else provider_resolver()
            ).strip().lower()
            # ``LLMSettings`` uses ``custom`` as its dataclass default.  When
            # the loaded snapshot contains no custom endpoint/model, that value
            # is an absent provider selection, not an explicit user choice;
            # resolve the configured provider at the normal config boundary.
            configured_provider = (
                resolved_provider
                if (
                    config_provider == "custom"
                    and not str(getattr(run_config.llm, "base_url", "") or "").strip()
                    and not str(getattr(run_config.llm, "model", "") or "").strip()
                    and resolved_provider
                )
                else config_provider or resolved_provider
            )
            queued_selection = getattr(self, "_queued_model_selections", {}).pop(
                conversation.id, None
            )
            queued_provider = (
                str(queued_selection.get("provider") or "").strip()
                if isinstance(queued_selection, dict)
                else ""
            )
            queued_model = (
                str(queued_selection.get("model") or "").strip()
                if isinstance(queued_selection, dict)
                else ""
            )
            selected_provider = str(getattr(self, "provider", "") or "").strip()
            preserve_provider_override = bool(
                getattr(self, "_provider_override_active", False)
                and run_model_runtime is not None
                and run_model_runtime.get_provider(selected_provider) is not None
            )
            run_provider = (
                queued_provider
                or (selected_provider if preserve_provider_override else configured_provider)
            )
            if run_model_runtime is not None:
                run_model_runtime.refresh()
                refresh_oauth = getattr(
                    run_model_runtime,
                    "refresh_oauth_credentials",
                    None,
                )
                if callable(refresh_oauth):
                    await refresh_oauth(run_provider)
                refresh_provider_auth = getattr(
                    run_model_runtime,
                    "refresh_provider_auth",
                    None,
                )
                if callable(refresh_provider_auth):
                    await refresh_provider_auth(run_provider)
                run_available_models = [
                    model.id for model in run_model_runtime.get_models(run_provider)
                ]
                run_models_source = (
                    "extension"
                    if run_model_runtime.get_registered_provider_config(run_provider)
                    is not None
                    else (
                        models_source_resolver(run_provider, run_settings)
                        if _resolver_accepts_positional_arguments(
                            models_source_resolver, run_provider, run_settings
                        )
                        else models_source_resolver(run_provider)
                    )
                    if models_source_resolver
                    else ""
                )
            else:
                run_available_models = list(
                    models_resolver(run_provider, run_settings)
                    if _resolver_accepts_positional_arguments(
                        models_resolver, run_provider, run_settings
                    )
                    else models_resolver(run_provider)
                )
                run_models_source = (
                    models_source_resolver(run_provider, run_settings)
                    if _resolver_accepts_positional_arguments(
                        models_source_resolver, run_provider, run_settings
                    )
                    else models_source_resolver(run_provider)
                    if models_source_resolver
                    else ""
                )

            # Determine model for this run
            config_model = getattr(run_config.llm, "model", "").strip()
            if queued_model:
                run_model = queued_model
            elif run_provider != self.provider:
                # Provider changed: use config model, not any previous override
                run_model = config_model
            elif not self._model_override_active:
                # Track config changes, while retaining the conversation's
                # already-selected model when a partial config snapshot omits
                # ``model``.  This is the canonical session selection, not a
                # provider/catalog fallback; a completely empty selection is
                # still rejected below.
                run_model = config_model or str(self.selected_model or "").strip()
            else:
                # Override active: keep using selected_model
                run_model = self.selected_model

            if run_available_models and run_model and run_model not in run_available_models:
                raise RuntimeError(
                    "provider_error_type=model: "
                    f"Selected model '{run_provider}/{run_model}' is unavailable "
                    "in the active provider model catalog"
                )
            if not run_model:
                raise RuntimeError(
                    f"provider_error_type=model: Provider '{run_provider}' requires an explicit model selection"
                )

            run_config = _config_with_runtime_model_budget(
                run_config,
                model_runtime=run_model_runtime,
                provider=run_provider,
                model=run_model,
            )

            run_llm = _get_or_create_session_llm(
                self,
                config=run_config,
                provider=run_provider,
                model=run_model,
                model_runtime=run_model_runtime,
            )
            run_runtime_model = (
                run_model_runtime.get_model(run_provider, run_model)
                if run_model_runtime is not None and run_provider and run_model
                else None
            )
            run_thinking_levels = model_thinking_levels(run_runtime_model, run_llm)
            requested_run_thinking = str(
                getattr(getattr(run_config, "llm", None), "reasoning_effort", "")
                or default_model_thinking_level(
                    run_runtime_model,
                    run_thinking_levels,
                )
                or "off"
            ).strip().lower()
            # MiniCode resolves and clamps the initial session thinking level before
            # the first model request. Applying it here also translates a
            # canonical thinkingLevelMap key to the provider's wire value.
            run_thinking_level = _apply_thinking_level(
                run_llm,
                run_runtime_model,
                requested_run_thinking,
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
                if not preserve_provider_override:
                    self._provider_override_active = False
        except Exception as exc:
            if not _owns_query_claim():
                try:
                    admission_runtime.commit_terminal(
                        admission_record.run_id,
                        "cancelled",
                        summary="stale_query_claim",
                        terminal_reason="stale_query_claim",
                    )
                except Exception:
                    logger.exception(
                        "Failed to close stale pre-query admission %s",
                        admission_record.run_id,
                    )
                logger.info(
                    "Discarding stale LLM initialization terminal projection for conversation %s",
                    conversation.id,
                )
                streams = getattr(self, "_conversation_streams", {})
                if streams.get(conversation.id) is stream_state:
                    streams.pop(conversation.id, None)
                return
            classification = classify_llm_error(exc)
            error_message = sanitize_llm_error_message(exc, classification)
            try:
                committed_run = admission_runtime.commit_terminal(
                    admission_record.run_id,
                    "failed",
                    summary="llm_initialization_failed",
                    terminal_reason="llm_initialization_failed",
                    error=error_message,
                )
            except Exception as commit_exc:
                terminal_error = AgentEvent.error(
                    "MiniCode could not durably commit the initialization failure.",
                    recoverable=False,
                    error_type="terminal_commit_failed",
                    error_code="runtime.initialization_terminal_commit_failed",
                )
                terminal_error.data.update({
                    "conversation_id": conversation.id,
                    "message_id": assistant_message_id,
                    "run_id": admission_record.run_id,
                    "terminal_commit_failed": True,
                })
                await self._send_event(terminal_error)
                done_event = AgentEvent.done(
                    status="failed",
                    reason="terminal_commit_failed",
                )
                done_event.data.update({
                    "conversation_id": conversation.id,
                    "message_id": assistant_message_id,
                })
                await self._send_event(done_event)
                logger.error(
                    "Failed to commit initialization terminal for %s: %s",
                    admission_record.run_id,
                    commit_exc,
                    exc_info=True,
                )
                run_manager = getattr(self, "_run_manager", None)
                if run_manager is not None:
                    run_manager.mark_terminal_status(conversation.id, "failed")
                    run_manager.mark_delivery_complete(conversation.id, run_task_id)
                getattr(self, "_conversation_streams", {}).pop(conversation.id, None)
                return
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
            await self._send_event(AgentEvent.agent_run_completed(committed_run))
            done_event = AgentEvent.done(status="failed", reason="llm_initialization_failed")
            done_event.data["failure_recoverable"] = not classification.fatal
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
            mark_delivery_complete = getattr(run_manager, "mark_delivery_complete", None)
            if callable(mark_delivery_complete):
                mark_delivery_complete(conversation.id, run_task_id)
            getattr(self, "_conversation_streams", {}).pop(conversation.id, None)
            return

        run_memory_manager = getattr(self, "memory_manager", None)
        if run_workspace_root is not None:
            from backend.memory.file_memory import FileMemory
            from backend.memory.generation import schedule_memory_startup
            from backend.memory.manager import MemoryManager

            run_memory_manager = MemoryManager(FileMemory.for_workspace(run_workspace_root))
            if str(getattr(conversation, "conversation_type", "main")) == "main":
                memory_task = schedule_memory_startup(
                    repository=self.conversation_repo,
                    llm=run_llm,
                    workspace_root=run_workspace_root,
                    current_conversation_id=conversation.id,
                    token_budget=int(getattr(run_config.token_budget, "total", 0) or 0),
                )
                _lease_session_llm_for_task(self, run_llm, memory_task)

        run_context_builder = ContextBuilder(
            token_budget=run_config.token_budget,
            agent_settings=run_config.agent,
            skill_executor=getattr(self, "skill_executor", None),
            memory_manager=run_memory_manager,
            llm=run_llm,
            skill_manager=self.skill_manager,
            conversation_id=conversation.id,
            workspace_root=run_workspace_root,
        )
        run_agent_session = AgentSession(
            llm=run_llm,
            tool_registry=run_tool_registry,
            artifact_store=self.artifact_store,
            permission_checker=PermissionChecker(run_config.permissions),
            agent_settings=run_config.agent,
            token_budget=run_config.token_budget,
            context_builder=run_context_builder,
            approval_handler=self._approval_handler,
            lifecycle_observer_factory=lifecycle_observer_factory,
            lifecycle_runtime=lifecycle_runtime,
        )

        def _active_run_llm() -> Any:
            return getattr(run_agent_session, "llm", None) or run_llm

        run_context_builder.load_snapshot(run_context_snapshot)
        normalized_attachments = list(attachments or [])
        # These values are host capabilities or concrete runtime owners, never
        # transport metadata. A local adapter/checkpoint resume may supply
        # ordinary correlation fields, but it must not shadow the live Pi
        # generation, tool policy, callbacks, or conversation ownership.
        for reserved_key in (
            "_lifecycle_runtime",
            "_toolset_policy",
            "_session_toolset_policy",
            "_execution_journal",
            "_mcp_manager",
            "_mcp_owner_session_id",
            "previous_turn_aborted",
            "agent_runtime",
            "workspace_context",
            "cost_session_id",
            "requires_explicit_workspace",
            "connected_mcp_servers",
            "permission_mode_setter",
            "permission_context_provider",
            "command_prompt_allow_rules_setter",
            "conversation_repository",
            "turn_input_queue",
            "persist_consumed_turn_input",
            "acknowledge_consumed_turn_input",
            "_subagent_parent_runtime",
            "_extension_thinking_level",
        ):
            run_metadata.pop(reserved_key, None)
        run_metadata["_extension_thinking_level"] = run_thinking_level
        run_metadata["_turn_model_snapshot"] = {
            "provider": run_provider,
            "model": run_model,
            "adapter_type": type(run_llm).__name__,
        }
        run_metadata["_subagent_parent_runtime"] = {
            # Codex builds every child from the live turn config instead of
            # re-reading process-global provider settings. Pi passes the
            # dispatching session model/thinking values to its child process.
            # Keep the same live, internal-only snapshot for TaskTool.
            "config": run_config,
            "provider": run_provider,
            "model": run_model,
            "model_runtime": run_model_runtime,
            "available_models": tuple(run_available_models),
            "models_source": run_models_source,
            "llm": run_llm,
            "thinking_level": run_thinking_level,
        }
        extension_state = getattr(self, "_extension_runtime_states", {}).get(
            conversation.id
        )
        if isinstance(extension_state, dict) and "active_tool_names" in extension_state:
            available_tool_names = set(
                _activatable_tool_names(run_tool_registry)
            )
            selected_tool_names = [
                str(name)
                for name in extension_state.get("active_tool_names") or []
                if str(name) in available_tool_names
            ]
            extension_state["active_tool_names"] = selected_tool_names
            run_agent_session.active_tool_names = tuple(selected_tool_names)
        parent_notification_only = bool(
            run_metadata.get("_parent_notification_only", False)
        )
        if lifecycle_runtime is not None:
            self._bind_lifecycle_runtime_host_actions(
                lifecycle_runtime,
                conversation=conversation,
                tool_registry=run_tool_registry,
                run_metadata=run_metadata,
                run_context_builder=run_context_builder,
                run_llm=run_llm,
                cancel_event=run_cancel_event,
                model_runtime=run_model_runtime,
                model_registry=self._model_registry_for_conversation(
                    conversation.id
                ),
                agent_session=run_agent_session,
            )
            self._mark_lifecycle_runtime_host_actions_bound(
                conversation.id,
                lifecycle_runtime,
            )
            # Preserve the exact published runtime so late extension
            # registrations refresh the live conversation registry.
            run_metadata["_lifecycle_runtime"] = lifecycle_runtime
        if previous_turn_aborted:
            run_metadata["previous_turn_aborted"] = True
        run_metadata["assistant_message_id"] = assistant_message_id
        run_metadata["agent_runtime"] = admission_runtime
        run_metadata["run_id"] = admission_record.run_id
        run_metadata["_execution_journal"] = execution_journal
        run_workspace_context = self._workspace_context_for_conversation(conversation)
        run_metadata["workspace_context"] = run_workspace_context
        run_metadata["conversation_id"] = conversation.id
        run_metadata["conversation_repository"] = self.conversation_repo
        run_metadata["cost_session_id"] = self.session_id
        run_metadata["requires_explicit_workspace"] = True
        mcp_manager = self._mcp_manager_for_workspace(run_workspace_root)
        run_metadata["_mcp_manager"] = mcp_manager
        run_metadata["_mcp_owner_session_id"] = str(
            getattr(self, "session_id", "") or ""
        )
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
        run_metadata["connected_mcp_servers"] = connected_mcp_servers
        run_permission_context = self._permission_context_for_conversation(
            conversation,
            source="agent.run",
        )
        if run_permission_context.mode == "plan":
            from backend.agent.plans import bind_plan_owner, merge_plan_constraints

            _slug, existing_plan_path = bind_plan_owner(
                self.conversation_repo,
                conversation.id,
                conversation.workspace_root or run_workspace_root,
            )
            conversation = self.conversation_repo.get_conversation(conversation.id) or conversation
            run_permission_context = self.permission_checker.build_context(
                mode=run_permission_context.mode,
                session_overrides=run_permission_context.session_overrides,
                command_prompt_allow_rules=run_permission_context.command_prompt_allow_rules,
                tool_deny_rules=run_permission_context.tool_deny_rules,
                filesystem_constraints=merge_plan_constraints(
                    run_permission_context.filesystem_constraints,
                    existing_plan_path,
                ),
                workspace_scope=run_permission_context.workspace_scope,
                source=run_permission_context.source,
                pre_plan_mode=str(getattr(conversation, "permission_previous_mode", "") or "") or None,
                approval_policy=run_permission_context.approval_policy,
                sandbox_mode=run_permission_context.sandbox_mode,
                requirements_source=run_permission_context.requirements_source,
            )

        async def _set_run_permission_mode(mode: str, *, source: str = "agent.run") -> None:
            from backend.agent.plans import bind_plan_owner, merge_plan_constraints
            from backend.ws.utils import normalize_permission_mode

            owner_id = conversation.id
            target_mode = normalize_permission_mode(str(mode or "")) or "confirm"
            current = self.conversation_repo.get_conversation(owner_id)
            if current is None:
                return

            # EnterPlanMode lazily binds the exact Markdown file owner before
            # persisting plan mode. ExitPlanMode rereads previous mode from the
            # repository before update_permission_mode clears it.
            if target_mode == "plan":
                _slug, plan_path = bind_plan_owner(
                    self.conversation_repo,
                    owner_id,
                    current.workspace_root or run_workspace_root,
                )
            else:
                previous = normalize_permission_mode(
                    str(getattr(current, "permission_previous_mode", "") or "")
                )
                if previous and previous != "plan":
                    target_mode = previous
                plan_path = None

            updated = self.conversation_repo.update_permission_mode(owner_id, target_mode)
            if updated is None:
                return

            is_active_owner = self.active_conversation_id == owner_id
            if is_active_owner:
                self._set_permission_context_mode(target_mode, source=source)
                self.permission_context = self.permission_checker.build_context(
                    mode=self.permission_context.mode,
                    session_overrides=self.permission_context.session_overrides,
                    command_prompt_allow_rules=self.permission_context.command_prompt_allow_rules,
                    tool_deny_rules=self.permission_context.tool_deny_rules,
                    filesystem_constraints=merge_plan_constraints(
                        self.permission_context.filesystem_constraints,
                        plan_path if target_mode == "plan" else None,
                    ),
                    workspace_scope=self.permission_context.workspace_scope,
                    source=source,
                    pre_plan_mode=(
                        str(getattr(updated, "permission_previous_mode", "") or "")
                        if target_mode == "plan"
                        else None
                    ),
                    approval_policy=self.permission_context.approval_policy,
                    sandbox_mode=self.permission_context.sandbox_mode,
                    requirements_source=self.permission_context.requirements_source,
                )
                await self._emit_permission_mode_updated()
                send_runtime = getattr(self, "_send_task_runtime_update", None)
                if callable(send_runtime):
                    result = send_runtime()
                    if asyncio.iscoroutine(result):
                        await result

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

        run_metadata["permission_mode_setter"] = _set_run_permission_mode
        run_metadata["permission_context_provider"] = _live_run_permission_context

        async def _add_run_command_prompt_allow_rules(
            prompts: list[str] | tuple[str, ...],
            *,
            source: str = "exit_plan_mode.command_prompts",
        ) -> None:
            normalized = [
                prompt
                for prompt in (str(item or "").strip() for item in prompts)
                if prompt
            ]
            if not normalized:
                return
            if self.active_conversation_id == conversation.id:
                changed = self._add_command_prompt_allow_rules(
                    normalized,
                    source=source,
                )
                if changed:
                    await self._emit_permission_rules_updated(
                        conversation_id=conversation.id,
                        source=source,
                    )

        run_metadata["command_prompt_allow_rules_setter"] = _add_run_command_prompt_allow_rules
        run_manager = getattr(self, "_run_manager", None)
        turn_input_queue = getattr(run_manager, "turn_input_queue", None)
        if callable(turn_input_queue):
            run_metadata["turn_input_queue"] = turn_input_queue(conversation.id)

        def _persist_consumed_turn_input(item: Any) -> None:
            persist_transcript = getattr(
                self.conversation_repo,
                "upsert_transcript_message",
                None,
            )
            if not callable(persist_transcript):
                persist_transcript = getattr(
                    self.conversation_repo,
                    "append_transcript_message",
                    None,
                )
            if not callable(persist_transcript):
                raise RuntimeError("Conversation transcript persistence is unavailable")
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
            persist_transcript(
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

        run_metadata["persist_consumed_turn_input"] = _persist_consumed_turn_input

        def _acknowledge_consumed_turn_input(item: Any) -> None:
            acknowledge = getattr(run_manager, "acknowledge_turn_input", None)
            if not callable(acknowledge):
                return
            acknowledge(conversation.id, getattr(item, "original_command", None))

        run_metadata["acknowledge_consumed_turn_input"] = (
            _acknowledge_consumed_turn_input
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

        if not parent_notification_only:
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
            if not _accepts_projection_events():
                return
            payload: dict[str, Any] = {
                "conversation_id": conv_id,
                "message_id": assistant_message_id,
                "content": line,
                "stream": stream if stream in {"stdout", "stderr"} else "stdout",
            }
            canonical_turn_id = str(stream_state.get("turn_id") or "").strip()
            if canonical_turn_id:
                payload["turn_id"] = canonical_turn_id
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
            if not _accepts_projection_events():
                return
            payload = dict(data)
            payload.setdefault("conversation_id", conversation.id)
            if event_type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                payload.setdefault("message_id", assistant_message_id)
                canonical_turn_id = str(
                    stream_state.get("turn_id")
                    or run_metadata.get("run_id")
                    or ""
                ).strip()
                if canonical_turn_id:
                    payload["turn_id"] = canonical_turn_id
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
        assistant_image_contexts: list[dict[str, Any]] = []
        usage_payload: dict[str, int] | None = None
        run_failed_message = ""
        run_failure_recoverable: bool | None = None
        done_event_sent = False
        query_terminal_status = ""
        query_terminal_reason = ""
        query_done_payload: dict[str, Any] = {}
        query_run_completed_payload: dict[str, Any] = {}
        provider_terminal_received = False
        active_runtime_spans: dict[str, dict[str, Any]] = {}
        assistant_message_id = str(stream_state.get("message_id") or assistant_message_id)

        def _now_ms() -> int:
            return int(time.time() * 1000)

        turn_state = AgentTurnState(now_ms=_now_ms)
        turn_started_at_ms = _now_ms()
        partial_persist_lock = asyncio.Lock()
        last_partial_persisted_at = 0.0

        async def _persist_partial_turn(*, force: bool = False) -> None:
            """Checkpoint the current typed turn without writing every token."""
            nonlocal last_partial_persisted_at
            if not _accepts_projection_events():
                return
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
                    if not _accepts_projection_events():
                        return
                    async with self._conversation_projection_lock(conversation.id):
                        if not _accepts_projection_events():
                            return
                        await asyncio.to_thread(upsert, conversation.id, partial_message)
                        saved_snapshot = run_context_builder.export_snapshot()
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
                        last_partial_persisted_at = time.monotonic()
                except Exception:
                    logger.exception(
                        "Failed to persist partial assistant transcript projection for conversation %s",
                        conversation.id,
                    )
                    return

        async def _maybe_emit_source_citation(data: dict[str, Any]) -> None:
            citation = turn_state.record_source_citation(data)
            if citation is None:
                return
            await self._send_event(AgentEvent(type="citation.add", data={
                "conversation_id": conversation.id,
                "message_id": assistant_message_id,
                **citation,
            }))

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
            return bool(
                callable(checker)
                and checker(conversation.id, run_task_id)
            )

        def _durable_terminal_projection() -> tuple[str, str]:
            """Return the canonical terminal status/reason already committed.

            QueryEngine/TurnKernel owns the durable transition.  This transport
            may add conversation/message correlation, but it must never infer a
            different terminal outcome from a provider ``done`` event.
            """
            payload = query_run_completed_payload
            if not payload:
                return "", ""
            raw_status = str(payload.get("status") or "").strip().lower()
            status = (
                "cancelled"
                if raw_status == "interrupted"
                else raw_status
                if raw_status in {"completed", "partial", "failed", "cancelled"}
                else ""
            )
            reason = str(
                payload.get("terminal_reason")
                or payload.get("summary")
                or payload.get("error")
                or ""
            ).strip()
            return status, reason

        async def _send_done_once() -> bool:
            nonlocal done_event_sent
            if not _owns_query_claim():
                return False
            if done_event_sent or _terminal_delivery_complete():
                done_event_sent = True
                return True
            if not query_done_payload:
                logger.error(
                    "Canonical done projection missing for conversation %s",
                    conversation.id,
                )
                return False
            done_event = AgentEvent(type="done", data=dict(query_done_payload))
            done_event.data.setdefault(
                "duration_ms",
                round((time.monotonic() - start_time) * 1000),
            )
            done_event.data["conversation_id"] = conversation.id
            done_event.data["message_id"] = assistant_message_id
            if isinstance(run_failure_recoverable, bool) and "failure_recoverable" not in done_event.data:
                done_event.data["failure_recoverable"] = run_failure_recoverable
            await self._send_event(done_event)
            done_event_sent = True
            return True

        from backend.ws.reasoning_batcher import (
            ReasoningEventBatcher,
            ReasoningFlushDeadline,
        )

        reasoning_batcher = ReasoningEventBatcher()
        reasoning_flush_lock = asyncio.Lock()
        reasoning_deadline: ReasoningFlushDeadline

        async def _flush_pending_reasoning(*, from_deadline: bool = False) -> None:
            async with reasoning_flush_lock:
                if not from_deadline:
                    await reasoning_deadline.disarm()
                pending = reasoning_batcher.flush_if_pending()
                if pending is not None:
                    await self._send_event(pending)
                    await _persist_partial_turn()

        async def _flush_reasoning_at_deadline() -> None:
            await _flush_pending_reasoning(from_deadline=True)

        reasoning_deadline = ReasoningFlushDeadline(
            reasoning_batcher.max_delay_seconds,
            _flush_reasoning_at_deadline,
        )

        async def _push_reasoning(
            event: AgentEvent,
            content: str,
            metadata: dict[str, Any],
        ) -> None:
            async with reasoning_flush_lock:
                turn_state.append_thinking(content, metadata)
                emitted = reasoning_batcher.push(event)
                for reasoning_event in emitted:
                    await self._send_event(reasoning_event)
                    await _persist_partial_turn()

                if reasoning_batcher.has_pending:
                    # An emitted batch followed by another pending batch means
                    # metadata/lifecycle changed. Give the new owner its own
                    # full deadline instead of inheriting the old timer.
                    if emitted:
                        await reasoning_deadline.disarm()
                    reasoning_deadline.arm()
                else:
                    await reasoning_deadline.disarm()

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
                    session=run_agent_session,
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
                        lifecycle_runtime=lifecycle_runtime,
                        agent_session=run_agent_session,
                    ),
                )
            ):
                if not _owns_query_claim():
                    logger.info(
                        "Stopping stale event projection for conversation %s",
                        conversation.id,
                    )
                    break
                if event.type == "context_compacted":
                    summary_text = str(event.data.get("summary", "")).strip()
                    await _commit_automatic_compaction(
                        self.conversation_repo,
                        conversation_id=conversation.id,
                        context_builder=run_context_builder,
                        summary=summary_text,
                        projection_lock=self._conversation_projection_lock(
                            conversation.id
                        ),
                    )
                    await self._send_ws_payload(
                        {
                            "type": "conversation.compaction.updated",
                            "conversation_id": conversation.id,
                            "state": "compacted",
                            "summary": summary_text,
                        },
                        log_context="conversation.compaction.updated",
                    )
                    event.data.setdefault("conversation_id", conversation.id)
                    await self._send_event(event)
                    continue

                if event.type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                    event.data.setdefault("message_id", assistant_message_id)
                    event_turn_id = str(event.data.get("turn_id") or "").strip()
                    if event_turn_id:
                        stream_state["turn_id"] = event_turn_id

                # Provider-managed progress is yielded by the main query
                # stream rather than the runtime callback used by local tools.
                # Persist both surfaces through the same reducer so a refresh
                # restores the complete Context timeline instead of only the
                # transcript's collapsed last progress message.
                if event.type in _UI_AGENT_STATE_EVENT_TYPES and _accepts_projection_events():
                    self._persist_ui_agent_state_event(
                        conversation.id,
                        event.type,
                        event.data,
                    )

                if event.type in {"thinking_delta", "thinking"}:
                    thinking_chunk = str(event.data.get("content", ""))
                    thinking_metadata = {
                        key: event.data[key]
                        for key in ("source", "visibility", "phase")
                        if key in event.data
                    }
                    await _push_reasoning(event, thinking_chunk, thinking_metadata)
                    continue

                # Reasoning must never be reordered across text, tool, progress,
                # error, or terminal boundaries.
                await _flush_pending_reasoning()

                if event.type in {"item.started", "agent_message.delta", "item.completed"}:
                    _project_agent_message_event(turn_state, event.type, event.data)
                elif event.type == "image_chunk":
                    raw_image_data = str(event.data.get("image_data") or "").strip()
                    raw_media_type = str(
                        event.data.get("media_type") or "image/png"
                    ).strip() or "image/png"
                    try:
                        image_data, media_type, decoded_image = _validated_generated_image(
                            raw_image_data,
                            raw_media_type,
                        )
                    except ValueError as exc:
                        logger.warning(
                            "Discarding invalid generated image for conversation %s: %s",
                            conversation.id,
                            exc,
                        )
                        await self._send_event(
                            _generated_image_rejection_notice(
                                conversation_id=conversation.id,
                                message_id=assistant_message_id,
                                media_type=raw_media_type,
                                encoded_characters=len(raw_image_data),
                                reason=str(exc),
                            )
                        )
                    else:
                        artifact_id = self.artifact_store.save(
                            image_data,
                            source="generated_image",
                            type="image",
                            preview_lines=1,
                            conversation_id=conversation.id,
                            workspace_root=str(run_workspace_root or ""),
                            media_type=media_type,
                        )
                        artifact, artifact_event = _generated_image_projection(
                            artifact_id=artifact_id,
                            media_type=media_type,
                            decoded_image=decoded_image,
                            conversation_id=conversation.id,
                            message_id=assistant_message_id,
                            text_offset=_utf16_code_unit_length(turn_state.content()),
                        )
                        assistant_artifacts.append(artifact)
                        assistant_image_contexts.append(
                            {
                                "artifact_id": artifact_id,
                                "image_data": image_data,
                                "media_type": media_type,
                                "size_bytes": len(decoded_image),
                            }
                        )
                        await self._send_ws_payload(
                            artifact_event,
                            log_context="artifact.preview",
                        )
                    # The provider image has been converted into a typed
                    # artifact event.  Do not also forward it as answer text.
                    continue
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
                    provider_terminal_received = True
                    query_done_payload = dict(event.data)
                    usage_payload = turn_state.record_done(event.data)
                    raw_terminal_status = str(event.data.get("status") or "").strip().lower()
                    durable_status, durable_reason = _durable_terminal_projection()
                    effective_terminal_status = durable_status or raw_terminal_status
                    if effective_terminal_status in {"completed", "partial", "failed", "cancelled", "interrupted"}:
                        query_terminal_status = effective_terminal_status
                    if durable_reason:
                        query_terminal_reason = durable_reason
                    if durable_status:
                        query_done_payload["status"] = durable_status
                        if durable_reason:
                            query_done_payload["reason"] = durable_reason
                    if effective_terminal_status == "completed":
                        run_failed_message = ""
                        run_failure_recoverable = None
                    elif isinstance(event.data.get("failure_recoverable"), bool):
                        run_failure_recoverable = bool(event.data["failure_recoverable"])
                    if not durable_reason:
                        query_terminal_reason = str(event.data.get("reason") or "").strip()
                    provider_raw = event.data.get("provider_raw")
                    request_summary = provider_raw.get("request_summary") if isinstance(provider_raw, dict) else None
                    usage_provider = str(
                        (request_summary.get("wire_api") if isinstance(request_summary, dict) else "")
                        or (provider_raw.get("provider") if isinstance(provider_raw, dict) else "")
                        or run_provider
                    )
                    active_usage_llm = _active_run_llm()
                    from backend.llm.capabilities import capabilities_for_adapter

                    active_capabilities = capabilities_for_adapter(active_usage_llm)
                    usage_provider = str(
                        (request_summary.get("wire_api") if isinstance(request_summary, dict) else "")
                        or (provider_raw.get("provider") if isinstance(provider_raw, dict) else "")
                        or active_capabilities.provider
                        or usage_provider
                    )
                    tracker.record_usage(
                        input_tokens=usage_payload.get("input_tokens", 0),
                        output_tokens=usage_payload.get("output_tokens", 0),
                        cache_creation_input_tokens=usage_payload.get("cache_creation_input_tokens", 0),
                        cache_read_input_tokens=usage_payload.get("cache_read_input_tokens", 0),
                        ordinary_input_tokens=(
                            usage_payload.get("ordinary_input_tokens")
                            if "ordinary_input_tokens" in usage_payload
                            else None
                        ),
                        prompt_cache_total_tokens=(
                            usage_payload.get("prompt_cache_total_tokens")
                            if "prompt_cache_total_tokens" in usage_payload
                            else None
                        ),
                        reasoning_output_tokens=usage_payload.get("reasoning_output_tokens", 0),
                        elapsed_sec=time.monotonic() - start_time,
                        model_id=(
                            getattr(active_capabilities, "model", "")
                            or getattr(active_usage_llm, "_model", None)
                            or getattr(
                                getattr(active_usage_llm, "_settings", None),
                                "model",
                                None,
                            )
                        ),
                        provider=usage_provider,
                        session_id=self.session_id,
                        input_includes_cache_read=bool(
                            usage_payload.get("input_includes_cache_read", True)
                        ),
                        input_includes_cache_write=bool(
                            usage_payload.get("input_includes_cache_write", True)
                        ),
                        cost_usd=float(usage_payload.get("cost_usd") or 0.0),
                    )
                elif event.type == "error":
                    run_failed_message = turn_state.record_error(event.data)
                    if isinstance(event.data.get("recoverable"), bool):
                        run_failure_recoverable = bool(event.data["recoverable"])
                    # Emit rate_limit event if this is a rate-limit error
                    provider_error_type = str(event.data.get("provider_error_type") or "")
                    if provider_error_type in ("rate_limit", "busy"):
                        await self._send_event(AgentEvent.rate_limit(
                            provider=str(getattr(self, "provider", "") or event.data.get("provider") or ""),
                            error_type=provider_error_type,
                            message=str(event.data.get("message") or ""),
                            recoverable=bool(event.data.get("recoverable", True)),
                            conversation_id=conversation.id,
                        ))

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
            if not provider_terminal_received:
                query_terminal_status = "failed"
                query_terminal_reason = "provider_terminal_missing"
                run_failed_message = (
                    "The provider stream ended without its required terminal event."
                )
                run_failure_recoverable = False
                turn_state.record_error({"message": run_failed_message})
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
        except AttachmentUnavailableError as exc:
            run_failed_message = str(exc)
            run_failure_recoverable = True
            await _cancel_child_subagents_for_run("attachment_unavailable")
            await self._send_event(
                AgentEvent(
                    type="error",
                    data={
                        "message": run_failed_message,
                        "recoverable": True,
                        "error_type": "attachment",
                        "error_code": "attachment_unavailable",
                        "attachments": [dict(item) for item in exc.attachments],
                        "conversation_id": conversation.id,
                        "message_id": assistant_message_id,
                    },
                )
            )
        except Exception as exc:
            run_failed_message = f"Chat run failed: {exc}"
            run_failure_recoverable = False
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
            # The deadline is owned by this run. Wait for an in-flight flush or
            # cancel a sleeping timer before terminal projection can complete.
            async with reasoning_flush_lock:
                await reasoning_deadline.close()

            # Clear streaming metadata
            streams = getattr(self, "_conversation_streams", {})
            if streams.get(conversation.id) is stream_state:
                streams.pop(conversation.id, None)

            terminal_projection_owned = _owns_query_claim()
            if not terminal_projection_owned:
                logger.info(
                    "Discarding stale terminal projection for conversation %s",
                    conversation.id,
                )

            # Determine terminal status BEFORE using it (was previously
            # referenced before assignment, causing UnboundLocalError when
            # the finally block ran after an exception path).
            durable_status, durable_reason = _durable_terminal_projection()
            if durable_status:
                query_terminal_status = durable_status
                if durable_reason:
                    query_terminal_reason = durable_reason
            terminal_status = (
                durable_status if durable_status
                else "cancelled" if run_interrupted
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

            _finalize_generated_image_text_offsets(
                assistant_artifacts,
                assistant_content,
            )

            assistant_tool_calls = turn_snapshot.tool_calls
            detected_pollution_sources = pollution_sources_from_tool_calls(
                assistant_tool_calls
            )
            memory_polluted = bool(getattr(conversation, "memory_polluted", False))
            memory_pollution_sources = list(
                getattr(conversation, "memory_pollution_sources", [])
            )
            memory_pollution_updated_at = conversation.updated_at
            pollution_state_changed = False
            if terminal_projection_owned and detected_pollution_sources:
                previous_sources = {
                    str(source).casefold()
                    for source in getattr(
                        conversation,
                        "memory_pollution_sources",
                        [],
                    )
                }
                memory_polluted = True
                try:
                    polluted_conversation = await asyncio.to_thread(
                        self.conversation_repo.mark_memory_polluted,
                        conversation.id,
                        detected_pollution_sources,
                    )
                    if polluted_conversation is not None:
                        memory_polluted = bool(
                            polluted_conversation.memory_polluted
                        )
                        next_sources = {
                            str(source).casefold()
                            for source in polluted_conversation.memory_pollution_sources
                        }
                        memory_pollution_sources = list(
                            polluted_conversation.memory_pollution_sources
                        )
                        memory_pollution_updated_at = polluted_conversation.updated_at
                        pollution_state_changed = (
                            not bool(getattr(conversation, "memory_polluted", False))
                            or next_sources != previous_sources
                        )
                except Exception:
                    # A persistence failure must not let external content enter
                    # generated facts during this turn.
                    logger.exception(
                        "Failed to persist memory isolation for conversation %s",
                        conversation.id,
                    )
                if memory_polluted:
                    try:
                        from backend.memory.generation import schedule_memory_forgetting

                        memory_llm = _active_run_llm()
                        memory_task = schedule_memory_forgetting(
                            repository=self.conversation_repo,
                            llm=memory_llm,
                            workspace_root=run_workspace_root,
                            conversation_id=conversation.id,
                            token_budget=int(
                                getattr(run_agent_session.token_budget, "total", 0) or 0
                            ),
                        )
                        _lease_session_llm_for_task(self, memory_llm, memory_task)
                    except Exception:
                        logger.exception(
                            "Failed to schedule memory forgetting for conversation %s",
                            conversation.id,
                        )
            run_manager = getattr(self, "_run_manager", None)
            conversation_summary_payload: dict[str, Any] | None = None
            assistant_message: dict[str, Any] | None = None
            new_summary: str | None = None
            if terminal_projection_owned and (
                assistant_content
                or assistant_blocks
                or assistant_tool_calls
                or assistant_artifacts
            ):
                completed_at = _now_ms()
                assistant_message = {
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
                    if isinstance(run_failure_recoverable, bool):
                        assistant_message["failure_recoverable"] = run_failure_recoverable
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

                if assistant_content and not parent_notification_only:
                    new_summary = build_conversation_summary(
                        user_message=user_message,
                        attachments=normalized_attachments,
                        assistant_content=assistant_content,
                        compaction_summary=conversation.compaction_summary or "",
                    )

            if terminal_projection_owned:
                terminal_projection_started = True
                terminal_fences = getattr(self, "_terminal_projection_fences", None)
                if not isinstance(terminal_fences, dict):
                    terminal_fences = self._terminal_projection_fences = {}
                terminal_fences[conversation.id] = assistant_message_id
                try:
                    async with self._conversation_projection_lock(conversation.id):
                        await self._flush_ui_agent_state_now_unlocked(conversation.id)
                        for image_context in assistant_image_contexts:
                            try:
                                run_context_builder.append_generated_image_context(
                                    artifact_id=str(image_context.get("artifact_id") or ""),
                                    image_data=str(image_context.get("image_data") or ""),
                                    media_type=str(image_context.get("media_type") or "image/png"),
                                    size_bytes=int(image_context.get("size_bytes") or 0),
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to persist generated image context for conversation %s",
                                    conversation.id,
                                )

                        saved_snapshot = run_context_builder.export_snapshot()
                        latest_conversation = await asyncio.to_thread(
                            self.conversation_repo.get_conversation,
                            conversation.id,
                        )
                        if latest_conversation is None:
                            raise RuntimeError(
                                "conversation disappeared during terminal projection"
                            )
                        _merge_ui_agent_state_into_snapshot(
                            saved_snapshot,
                            getattr(latest_conversation, "context_snapshot", None),
                        )
                        terminal_journal = run_metadata.get("_execution_journal")
                        append_lifecycle = getattr(
                            terminal_journal,
                            "append_lifecycle",
                            None,
                        )
                        if not callable(append_lifecycle):
                            raise RuntimeError(
                                "terminal conversation projection has no execution journal owner"
                            )
                        pending_projection = append_lifecycle(
                            "conversation_projection_pending",
                            {
                                "conversation_id": conversation.id,
                                "assistant_message": assistant_message,
                                "context_snapshot": saved_snapshot,
                                "summary": new_summary,
                                "expected_revision": int(
                                    getattr(latest_conversation, "revision", 0) or 0
                                ),
                            },
                        )
                        updated_conversation = await asyncio.to_thread(
                            self.conversation_repo.commit_turn_projection,
                            conversation.id,
                            assistant_message=assistant_message,
                            context_snapshot=saved_snapshot,
                            summary=new_summary,
                            expected_revision=int(
                                getattr(latest_conversation, "revision", 0) or 0
                            ),
                        )
                        if updated_conversation is None:
                            raise RuntimeError("conversation disappeared during terminal commit")
                        append_lifecycle(
                            "conversation_projection_committed",
                            {
                                "conversation_id": conversation.id,
                                "pending_event_id": pending_projection.event_id,
                                "conversation_revision": int(
                                    getattr(updated_conversation, "revision", 0) or 0
                                ),
                                "message_id": assistant_message_id,
                            },
                        )
                        if conversation.id == self.active_conversation_id:
                            self._load_active_conversation_snapshot(conversation.id, saved_snapshot)
                        if new_summary is not None:
                            conversation_summary_payload = {
                                "type": "conversation.summary.updated",
                                "conversation_id": conversation.id,
                                "summary": new_summary,
                                "title": getattr(updated_conversation, "title", conversation.title),
                                "updated_at": getattr(updated_conversation, "updated_at", conversation.updated_at),
                                "memory_mode": str(
                                    getattr(
                                        updated_conversation,
                                        "memory_mode",
                                        "polluted" if memory_polluted else "enabled",
                                    )
                                ),
                                "memory_polluted": memory_polluted,
                                "memory_pollution_sources": memory_pollution_sources,
                            }
                except Exception:
                    logger.exception(
                        "Failed to atomically persist terminal conversation projection for %s",
                        conversation.id,
                    )
                    await self._send_event(
                        AgentEvent(
                            type="error",
                            data={
                                "message": (
                                    "The response finished, but its terminal conversation "
                                    "projection could not be saved."
                                ),
                                "recoverable": True,
                                "error_type": "persistence",
                                "error_code": "conversation.persistence_failed",
                                "conversation_id": conversation.id,
                                "message_id": assistant_message_id,
                            },
                        )
                    )
            if pollution_state_changed and conversation_summary_payload is None:
                conversation_summary_payload = {
                    "type": "conversation.summary.updated",
                    "conversation_id": conversation.id,
                    "summary": conversation.summary or "",
                    "title": conversation.title,
                    "updated_at": memory_pollution_updated_at,
                    "memory_mode": "polluted" if memory_polluted else str(
                        getattr(conversation, "memory_mode", "enabled")
                    ),
                    "memory_polluted": memory_polluted,
                    "memory_pollution_sources": memory_pollution_sources,
                }
            # QueryEngine/TurnKernel already committed the canonical terminal.
            # Transcript and UI state are recoverable projections and may emit
            # their own persistence errors, but cannot rewrite the run result.
            mark_terminal_status = getattr(run_manager, "mark_terminal_status", None)
            if terminal_projection_owned and callable(mark_terminal_status):
                mark_terminal_status(conversation.id, terminal_status)
            if (
                terminal_projection_owned
                and query_run_completed_payload
                and not _terminal_delivery_complete()
            ):
                run_completed_payload = dict(query_run_completed_payload)
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
            terminal_delivered = await _send_done_once()
            mark_delivery_complete = getattr(run_manager, "mark_delivery_complete", None)
            if terminal_projection_owned and terminal_delivered and callable(mark_delivery_complete):
                mark_delivery_complete(conversation.id, run_task_id)
            if terminal_projection_owned and terminal_delivered:
                await self._send_event(AgentEvent.session_state_changed(
                    state="idle",
                    conversation_id=conversation.id,
                    reason=("completed" if terminal_status == "completed"
                            else terminal_status),
                ))
            if terminal_projection_owned and conversation_summary_payload is not None:
                await self._send_ws_payload(
                    conversation_summary_payload,
                    log_context="conversation.summary.updated",
                )

