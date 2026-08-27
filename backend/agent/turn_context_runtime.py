"""Build one provider request context and its cache-stability metadata."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.agent.loop_runtime_helpers import epoch_ms
from backend.agent.prompt_cache import (
    build_prompt_cache_safe_params,
    prompt_cache_fork_diagnostic,
)
from backend.agent.provider_protocol import (
    annotate_request_metadata_with_prompt_cache_fork,
)
from backend.llm.base import LLMAdapter, LLMMessage
from backend.agent.lifecycle_observer import resolve_lifecycle_runtime

logger = logging.getLogger(__name__)


def _extension_message_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text") or "")
        return str(value.get("text") or value.get("content") or "")
    if isinstance(value, (list, tuple)):
        return "\n".join(
            part
            for item in value
            if (part := _extension_message_content(item)).strip()
        )
    return str(value or "")


def _coerce_extension_context_messages(
    values: Any,
    fallback: list[LLMMessage],
) -> list[LLMMessage]:
    """Keep context transforms inside MiniCode's typed provider contract."""

    if not isinstance(values, (list, tuple)):
        return fallback
    result: list[LLMMessage] = []
    for value in values:
        if isinstance(value, LLMMessage):
            result.append(value)
            continue
        if not isinstance(value, dict):
            return fallback
        role = str(value.get("role") or "user").strip().lower()
        if role == "custom":
            role = "user"
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            return fallback
        result.append(
            LLMMessage(
                role=role,
                content=_extension_message_content(value.get("content", "")),
                name=(str(value["name"]) if value.get("name") is not None else None),
                tool_call_id=(
                    str(value["tool_call_id"])
                    if value.get("tool_call_id") is not None
                    else (str(value["toolCallId"]) if value.get("toolCallId") is not None else None)
                ),
                is_error=bool(value.get("is_error", value.get("isError", False))),
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class PreparedTurnContext:
    messages: list[LLMMessage]
    prompt_cache_safe_params: dict[str, Any]
    prompt_cache_fork: dict[str, Any] | None


async def prepare_turn_context(
    *,
    context: Any,
    state: Any,
    llm: LLMAdapter,
    tool_schemas: list[dict[str, Any]],
    request_metadata: dict[str, Any],
    metadata: dict[str, Any],
    external_metadata: dict[str, Any] | None,
    tool_context: Any,
    turn_kernel: Any,
    run_id: str,
) -> PreparedTurnContext:
    """Reconcile history, render messages, and project cache-fork metadata."""
    extension_system_prompt = metadata.get("_extension_system_prompt")
    set_extension_prompt = getattr(context, "set_extension_system_prompt", None)
    if callable(set_extension_prompt):
        set_extension_prompt(
            str(extension_system_prompt)
            if extension_system_prompt is not None
            else None
        )

    # before_agent_start custom messages are inserted after the canonical user
    # prompt, matching the runtime's message-to-user conversion. Apply
    # the result once; later iterations only rebuild the provider projection.
    if not metadata.get("_extension_before_agent_messages_applied"):
        raw_extension_messages = metadata.get("_extension_before_agent_messages")
        if isinstance(raw_extension_messages, (list, tuple)):
            append_user_context = getattr(context, "append_user_context", None)
            if callable(append_user_context):
                for raw_message in raw_extension_messages:
                    if not isinstance(raw_message, dict):
                        continue
                    content = _extension_message_content(
                        raw_message.get("content", "")
                    ).strip()
                    if content:
                        append_user_context(content)
        metadata["_extension_before_agent_messages_applied"] = True

    pending_extension_messages = metadata.pop("_extension_pending_messages", [])
    if isinstance(pending_extension_messages, list):
        append_user_context = getattr(context, "append_user_context", None)
        if callable(append_user_context):
            for pending in pending_extension_messages:
                raw_message = (
                    pending.get("message")
                    if isinstance(pending, dict)
                    else pending
                )
                content = _extension_message_content(
                    raw_message.get("content", "")
                    if isinstance(raw_message, dict)
                    else raw_message
                ).strip()
                if content:
                    append_user_context(content)

    # Appended entries land in the transcript where the model
    # can see them; project them as user context so they stop vanishing in a
    # metadata list nobody reads.
    pending_extension_entries = metadata.pop("_extension_entries", [])
    if isinstance(pending_extension_entries, list):
        append_user_context = getattr(context, "append_user_context", None)
        if callable(append_user_context):
            for pending in pending_extension_entries:
                if not isinstance(pending, dict):
                    continue
                custom_type = str(pending.get("custom_type") or "").strip()
                data = pending.get("data")
                if not custom_type:
                    continue
                if data is None:
                    continue
                if isinstance(data, str):
                    data_text = data.strip()
                else:
                    import json as _json

                    try:
                        data_text = _json.dumps(data, ensure_ascii=False)
                    except (TypeError, ValueError):
                        data_text = str(data)
                if data_text:
                    append_user_context(f"<{custom_type}>{data_text}</{custom_type}>")

    # Labels attach a human label to an entry; surface it as context
    # so the label is not silently dropped.
    pending_extension_labels = metadata.pop("_extension_labels", [])
    if isinstance(pending_extension_labels, list):
        append_user_context = getattr(context, "append_user_context", None)
        if callable(append_user_context):
            for pending in pending_extension_labels:
                if not isinstance(pending, dict):
                    continue
                entry_id = str(pending.get("entry_id") or "").strip()
                label = pending.get("label")
                label_text = (
                    label.strip() if isinstance(label, str) else str(label or "")
                ).strip()
                if not label_text:
                    continue
                if entry_id:
                    append_user_context(f"<entry-label id=\"{entry_id}\">{label_text}</entry-label>")
                else:
                    append_user_context(f"<entry-label>{label_text}</entry-label>")

    # Fallback user messages queued by extensions when no run manager was
    # bound; deliver them the next context build instead of losing them.
    pending_extension_user_messages = metadata.pop("_extension_pending_user_messages", [])
    if isinstance(pending_extension_user_messages, list):
        append_user_context = getattr(context, "append_user_context", None)
        if callable(append_user_context):
            for pending in pending_extension_user_messages:
                content = _extension_message_content(
                    pending.get("content", "") if isinstance(pending, dict) else pending
                ).strip()
                if content:
                    append_user_context(content)

    reconcile = getattr(context, "reconcile_dangling_tool_calls", None)
    if callable(reconcile):
        reconcile()

    span_id = f"context:{run_id}:{state.iterations + 1}"
    started_at = epoch_ms()
    await turn_kernel.emit_runtime_span(
        "context.build.started",
        span_id=span_id,
        phase="context",
        status="running",
        label="context",
        summary="Building model context",
        started_at=started_at,
        ui_visible=False,
    )
    messages = await context.build(state)

    lifecycle_runtime = resolve_lifecycle_runtime(metadata)
    emit_context = getattr(lifecycle_runtime, "emit_context", None)
    if callable(emit_context):
        try:
            transformed = await emit_context(messages)
            messages = _coerce_extension_context_messages(transformed, messages)
        except Exception as exc:
            # ExtensionRunner normally isolates handler failures. Keep this
            # boundary fail-open for a third-party runner implementation so a
            # context transform cannot strand the model turn — but the
            # skipped transform must be visible evidence, never silent.
            logger.warning(
                "Extension context transform failed; sending untransformed context: %s",
                exc,
            )
            await turn_kernel.emit_runtime_span(
                "context.build.extension_transform_failed",
                span_id=f"{span_id}:extension",
                phase="context",
                status="failed",
                label="context",
                summary="Extension context transform failed; untransformed context sent",
                started_at=epoch_ms(),
                ended_at=epoch_ms(),
                ui_visible=False,
                data={"detail": str(exc)},
            )

    completed_at = epoch_ms()
    await turn_kernel.emit_runtime_span(
        "context.build.completed",
        span_id=span_id,
        phase="context",
        status="completed",
        label="context",
        summary="Model context ready",
        started_at=started_at,
        ended_at=completed_at,
        duration_ms=completed_at - started_at,
        ui_visible=False,
        data={"message_count": len(messages)},
    )

    section_summary = state.prompt_context.get("prompt_section_summary")
    safe_params = build_prompt_cache_safe_params(
        messages=messages,
        tool_schemas=tool_schemas,
        request_metadata=request_metadata,
        prompt_section_summary=(
            dict(section_summary) if isinstance(section_summary, dict) else {}
        ),
    )
    fork = prompt_cache_fork_diagnostic(
        metadata.get("parent_prompt_cache_safe_params"),
        safe_params,
    )
    if fork:
        annotate_request_metadata_with_prompt_cache_fork(request_metadata, fork)
        safe_params = {**safe_params, "fork_context": fork}
        metadata["prompt_cache_fork"] = fork

    metadata["prompt_cache_safe_params"] = safe_params
    tool_context.metadata["prompt_cache_safe_params"] = safe_params
    if fork:
        tool_context.metadata["prompt_cache_fork"] = fork
    if external_metadata is not None:
        external_metadata["prompt_cache_safe_params"] = dict(safe_params)
        if fork:
            external_metadata["prompt_cache_fork"] = dict(fork)

    return PreparedTurnContext(
        messages=messages,
        prompt_cache_safe_params=safe_params,
        prompt_cache_fork=fork,
    )
