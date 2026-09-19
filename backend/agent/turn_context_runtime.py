"""Build one provider request context and its cache-stability metadata."""

from __future__ import annotations

from copy import deepcopy
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
from backend.llm.base import LLMAdapter, LLMMessage, ToolCallEvent
from backend.agent.lifecycle_observer import resolve_lifecycle_runtime

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
) -> list[LLMMessage]:
    """Decode and validate the complete extension message contract."""

    if not isinstance(values, (list, tuple)):
        raise TypeError("extension context transform must return a message sequence")
    result: list[LLMMessage] = []
    for value in values:
        if isinstance(value, LLMMessage):
            result.append(deepcopy(value))
            continue
        if not isinstance(value, dict):
            raise TypeError("extension context messages must be LLMMessage or mapping")
        role = str(value.get("role") or "").strip().lower()
        if role == "custom":
            role = "user"
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"invalid extension context message role: {role!r}")
        raw_tool_calls = value.get("tool_calls", value.get("toolCalls"))
        tool_calls: list[ToolCallEvent] | None = None
        if raw_tool_calls is not None:
            if not isinstance(raw_tool_calls, (list, tuple)):
                raise TypeError("extension message tool_calls must be a sequence")
            tool_calls = []
            for raw_call in raw_tool_calls:
                if isinstance(raw_call, ToolCallEvent):
                    tool_calls.append(deepcopy(raw_call))
                    continue
                if not isinstance(raw_call, dict):
                    raise TypeError("extension tool call must be ToolCallEvent or mapping")
                call_id = str(raw_call.get("id") or "").strip()
                call_name = str(raw_call.get("name") or "").strip()
                arguments = raw_call.get("arguments", raw_call.get("input"))
                if not call_id or not call_name or not isinstance(arguments, dict):
                    raise ValueError(
                        "extension tool call requires id, name, and object arguments"
                    )
                tool_calls.append(
                    ToolCallEvent(
                        id=call_id,
                        name=call_name,
                        arguments=deepcopy(arguments),
                        arguments_repaired=bool(
                            raw_call.get(
                                "arguments_repaired",
                                raw_call.get("argumentsRepaired", False),
                            )
                        ),
                        duplicate_id=bool(
                            raw_call.get(
                                "duplicate_id",
                                raw_call.get("duplicateId", False),
                            )
                        ),
                    )
                )
        result.append(
            LLMMessage(
                role=role,
                content=_extension_message_content(value.get("content", "")),
                name=(str(value["name"]) if value.get("name") is not None else None),
                tool_calls=tool_calls,
                tool_call_id=(
                    str(value["tool_call_id"])
                    if value.get("tool_call_id") is not None
                    else (str(value["toolCallId"]) if value.get("toolCallId") is not None else None)
                ),
                is_error=bool(value.get("is_error", value.get("isError", False))),
                phase=str(value.get("phase") or ""),
                provider_items=deepcopy(
                    value.get("provider_items", value.get("providerItems", [])) or []
                ),
                images=deepcopy(value.get("images") or []),
                documents=deepcopy(value.get("documents") or []),
                attachment_refs=deepcopy(
                    value.get("attachment_refs", value.get("attachmentRefs", [])) or []
                ),
                runtime_context=str(
                    value.get(
                        "runtime_context",
                        value.get("runtimeContext", ""),
                    )
                    or ""
                ),
                timestamp_ms=(
                    int(value.get("timestamp_ms", value.get("timestampMs")))
                    if value.get("timestamp_ms", value.get("timestampMs")) is not None
                    else None
                ),
            )
        )
    _validate_extension_tool_protocol(result)
    return result


def _validate_extension_tool_protocol(messages: list[LLMMessage]) -> None:
    pending: dict[str, str] = {}
    seen_call_ids: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            if pending:
                raise ValueError("extension context contains dangling tool calls")
            for call in message.tool_calls or []:
                call_id = str(call.id or "").strip()
                if not call_id or call_id in seen_call_ids:
                    raise ValueError(
                        f"extension context contains duplicate tool call id: {call_id!r}"
                    )
                seen_call_ids.add(call_id)
                pending[call_id] = call.name
            continue
        if message.role == "tool":
            call_id = str(message.tool_call_id or "").strip()
            if call_id not in pending:
                raise ValueError(
                    f"extension context contains orphan tool result: {call_id!r}"
                )
            pending.pop(call_id)
            continue
        if pending:
            raise ValueError(
                "extension context separates tool calls from their results"
            )
    if pending:
        raise ValueError("extension context contains dangling tool calls")


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

    extension_actions = tool_context.run_context.extension_actions
    extension_messages: list[str] = []
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
                        extension_messages.append(content)
        metadata["_extension_before_agent_messages_applied"] = True

    pending_extension_messages = metadata.pop("_extension_pending_messages", [])
    queued_extension_messages = context.extension_state.pop("pending_messages", [])
    extension_messages.extend(queued_extension_messages)
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
                    extension_messages.append(content)

    # Old host bindings may still hand over these lists at admission. They
    # migrate into extension storage, never into the provider message stream.
    import json
    from uuid import uuid4
    entries = metadata.pop("_extension_entries", [])
    labels = metadata.pop("_extension_labels", [])
    for entry in entries:
        if extension_actions is not None:
            extension_actions.append_entry(entry["custom_type"], entry.get("data"))
        else:
            context.extension_state.setdefault("entries", []).append({
                "id": "ext_" + uuid4().hex, "type": "custom",
                "custom_type": entry["custom_type"],
                "data": json.loads(json.dumps(entry.get("data"), ensure_ascii=False, allow_nan=False)),
            })
    for item in labels:
        if extension_actions is not None:
            extension_actions.set_label(item["entry_id"], item.get("label"))
        else:
            stored_labels = context.extension_state.setdefault("labels", {})
            if item.get("label") is None:
                stored_labels.pop(item["entry_id"], None)
            else:
                stored_labels[item["entry_id"]] = str(item["label"])

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
                    extension_messages.append(content)

    history_start = len(context._history)
    for content in extension_messages:
        context.append_user_context(content)
    if extension_actions is not None:
        if extension_messages:
            # The queue handoff and its provider-visible messages are one
            # durable change. A crash must not leave only the queue deletion.
            extension_actions.changed({
                "fields": {}, "remove": ["pending_messages"] if queued_extension_messages else [],
                "context_messages": [
                    {"role": message.role, "content": message.content,
                     "timestamp_ms": message.timestamp_ms, "is_user_input": False}
                    for message in context._history[history_start:]
                ],
            })
        if extension_messages or entries or labels:
            await extension_actions.flush()

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

    lifecycle_runtime = resolve_lifecycle_runtime(
        run_context=tool_context.run_context,
    )
    emit_context = getattr(lifecycle_runtime, "emit_context", None)
    if callable(emit_context):
        transformed = await emit_context(messages)
        messages = _coerce_extension_context_messages(transformed)

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
