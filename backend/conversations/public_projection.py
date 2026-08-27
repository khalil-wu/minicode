"""Public, secret-redacted projections for conversation persistence and UI payloads."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from backend.agent.provider_protocol import provider_raw_for_projection
from backend.agent.public_projection import project_public_usage, public_text
from backend.secret_redaction import is_sensitive_field_name


_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_PUBLIC_DATA_URL_CHARS = 8 * 1024 * 1024
_MAX_PUBLIC_JSON_TEXT_CHARS = 8 * 1024 * 1024
_PUBLIC_DATA_URL_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/gif;base64,",
    "data:image/webp;base64,",
    "data:application/pdf;base64,",
)
_MESSAGE_ROLES = frozenset(
    {
        "user",
        "assistant",
        "system",
        "developer",
        "tool",
        "tool_result",
        "toolResult",
        "bashExecution",
        "custom",
        "branchSummary",
        "compactionSummary",
    }
)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(numeric)
        or numeric < 0
        or not numeric.is_integer()
        or numeric > _MAX_SAFE_INTEGER
    ):
        return None
    return int(numeric)


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _public_json(
    value: Any,
    *,
    preserve_data_urls: bool = False,
    depth: int = 0,
    max_depth: int = 16,
    data_url_budget: list[int] | None = None,
    text_budget: list[int] | None = None,
) -> Any:
    """Bound arbitrary extension/tool data before it reaches a public record."""

    if depth > max_depth:
        return "[public value omitted: nesting limit exceeded]"
    if preserve_data_urls and data_url_budget is None:
        data_url_budget = [_MAX_PUBLIC_DATA_URL_CHARS]
    if text_budget is None:
        text_budget = [_MAX_PUBLIC_JSON_TEXT_CHARS]
    if isinstance(value, str):
        if preserve_data_urls and value[:5].casefold() == "data:":
            folded = value[:64].casefold()
            if (
                len(value) <= _MAX_PUBLIC_DATA_URL_CHARS
                and data_url_budget is not None
                and len(value) <= data_url_budget[0]
                and any(folded.startswith(prefix) for prefix in _PUBLIC_DATA_URL_PREFIXES)
            ):
                data_url_budget[0] -= len(value)
                return value
            return "[public data URL omitted: unsupported type or size limit exceeded]"
        if text_budget[0] <= 0:
            return "[public value omitted: message text budget exceeded]"
        rendered = public_text(value, max_chars=min(262_144, text_budget[0]))
        text_budget[0] = max(0, text_budget[0] - len(rendered))
        return rendered
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value if abs(value) <= _MAX_SAFE_INTEGER else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [
            _public_json(
                item,
                preserve_data_urls=preserve_data_urls,
                depth=depth + 1,
                max_depth=max_depth,
                data_url_budget=data_url_budget,
                text_budget=text_budget,
            )
            for item in value[:4_096]
        ]
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:4_096]:
            if is_sensitive_field_name(raw_key):
                continue
            key = public_text(raw_key, max_chars=256, single_line=True)
            if not key:
                continue
            projected[key] = _public_json(
                item,
                preserve_data_urls=preserve_data_urls,
                depth=depth + 1,
                max_depth=max_depth,
                data_url_budget=data_url_budget,
                text_budget=text_budget,
            )
        return projected
    return public_text(value, max_chars=4_096)


def _string_list(value: Any, *, maximum: int = 256) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:maximum]:
        text = public_text(item, max_chars=4_096, single_line=True)
        if text and text not in result:
            result.append(text)
    return result


def _public_diff(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    projected: dict[str, Any] = {}
    for key in ("plus", "minus"):
        count = _nonnegative_int(value.get(key))
        if count is not None:
            projected[key] = count
    patch = public_text(value.get("patch"), max_chars=262_144)
    if patch:
        projected["patch"] = patch
    raw_files = value.get("files")
    if isinstance(raw_files, list):
        files: list[dict[str, Any]] = []
        for raw_file in raw_files[:2_048]:
            if not isinstance(raw_file, Mapping):
                continue
            path = public_text(raw_file.get("path"), max_chars=4_096)
            if not path:
                continue
            item: dict[str, Any] = {"path": path}
            old_path = public_text(raw_file.get("oldPath", raw_file.get("old_path")), max_chars=4_096)
            if old_path:
                item["oldPath"] = old_path
            for key in ("plus", "minus"):
                count = _nonnegative_int(raw_file.get(key))
                if count is not None:
                    item[key] = count
            file_patch = public_text(raw_file.get("patch"), max_chars=262_144)
            if file_patch:
                item["patch"] = file_patch
            status = public_text(raw_file.get("status"), max_chars=64, single_line=True)
            if status:
                item["status"] = status
            files.append(item)
        if files:
            projected["files"] = files
    return projected or None


def project_public_tool_call(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key, maximum in (
        ("id", 1_024),
        ("name", 512),
        ("status", 64),
        ("transition", 128),
        ("waitingOn", 2_048),
        ("blockingReason", 12_000),
        ("summary", 50_000),
        ("artifactId", 2_048),
        ("sourceUrl", 4_096),
        ("extractionStatus", 128),
        ("contentPreview", 60_000),
        ("evidenceType", 128),
        ("displaySummary", 12_000),
        ("resultKind", 128),
        ("activityKind", 128),
        ("visibility", 32),
        ("limitation", 12_000),
        ("provider", 256),
        ("providerErrorType", 256),
        ("errorKind", 256),
        ("userSummary", 12_000),
        ("developerDetail", 12_000),
        ("projection", 256),
        ("displayHint", 2_048),
        ("inputSummary", 12_000),
        ("groupId", 1_024),
        ("stepId", 1_024),
        ("taskId", 1_024),
        ("turnId", 1_024),
        ("iterationId", 1_024),
        ("phase", 128),
        ("outputPreview", 60_000),
        ("stdoutPreview", 60_000),
        ("stderrPreview", 60_000),
    ):
        raw = source.get(key)
        if raw is None:
            snake_key = "".join(("_" + char.lower()) if char.isupper() else char for char in key)
            raw = source.get(snake_key)
        text = public_text(raw, max_chars=maximum, single_line=maximum <= 2_048)
        if text:
            projected[key] = text
    arguments = source.get("args")
    if not isinstance(arguments, Mapping):
        arguments = source.get("arguments")
    projected["args"] = _public_json(arguments if isinstance(arguments, Mapping) else {})
    for key in ("durationMs", "seq", "startedAt", "finishedAt"):
        raw = source.get(key)
        if raw is None:
            snake_key = "".join(("_" + char.lower()) if char.isupper() else char for char in key)
            raw = source.get(snake_key)
        count = _nonnegative_int(raw)
        if count is not None:
            projected[key] = count
    for key in ("recoverable", "temporaryRemoved"):
        raw = source.get(key)
        if raw is None:
            snake_key = "".join(("_" + char.lower()) if char.isupper() else char for char in key)
            raw = source.get(snake_key)
        if isinstance(raw, bool):
            projected[key] = raw
    cleanup_receipt = source.get("cleanupReceipt", source.get("cleanup_receipt"))
    if isinstance(cleanup_receipt, Mapping):
        projected["cleanupReceipt"] = _public_json(cleanup_receipt)
    for source_key, target_key in (
        ("supersededToolCallIds", "supersededToolCallIds"),
        ("superseded_tool_call_ids", "supersededToolCallIds"),
        ("removedFilePaths", "removedFilePaths"),
        ("removed_file_paths", "removedFilePaths"),
    ):
        values = _string_list(source.get(source_key), maximum=2_048)
        if values:
            projected[target_key] = values
    error_info = source.get("errorInfo", source.get("error_info"))
    if isinstance(error_info, Mapping):
        projected["errorInfo"] = _public_json(error_info)
    diff = _public_diff(source.get("diff"))
    if diff:
        projected["diff"] = diff
    raw_files = source.get("outputFiles", source.get("output_files"))
    if isinstance(raw_files, list):
        files: list[dict[str, Any]] = []
        for raw_file in raw_files[:2_048]:
            if not isinstance(raw_file, Mapping):
                continue
            path = public_text(raw_file.get("path"), max_chars=4_096)
            if not path:
                continue
            item: dict[str, Any] = {"path": path, "size": _nonnegative_int(raw_file.get("size")) or 0}
            for source_key, target_key in (("name", "name"), ("mimeType", "mimeType"), ("mime_type", "mimeType")):
                text = public_text(raw_file.get(source_key), max_chars=512, single_line=True)
                if text:
                    item[target_key] = text
            image = raw_file.get("isImage", raw_file.get("is_image"))
            if isinstance(image, bool):
                item["isImage"] = image
            files.append(item)
        if files:
            projected["outputFiles"] = files
    return projected


def _project_block(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    block_type = public_text(value.get("type"), max_chars=64, single_line=True)
    if block_type == "tool_call":
        record = project_public_tool_call(value.get("record"))
        return {"type": "tool_call", "record": record} if record.get("id") and record.get("name") else None
    if block_type == "text":
        block: dict[str, Any] = {
            "type": "text",
            "content": public_text(value.get("content"), max_chars=4_194_304),
        }
        for source_key, target_key, maximum in (
            ("itemId", "itemId", 1_024),
            ("item_id", "itemId", 1_024),
            ("source", "source", 128),
            ("status", "status", 64),
            ("finishReason", "finishReason", 128),
            ("finish_reason", "finishReason", 128),
        ):
            text = public_text(value.get(source_key), max_chars=maximum, single_line=True)
            if text:
                block[target_key] = text
        streaming = value.get("isStreaming", value.get("is_streaming"))
        if isinstance(streaming, bool):
            block["isStreaming"] = streaming
        provider_raw = value.get("providerRaw", value.get("provider_raw"))
        safe_provider_raw = provider_raw_for_projection(
            dict(provider_raw) if isinstance(provider_raw, Mapping) else None
        )
        if safe_provider_raw:
            block["providerRaw"] = safe_provider_raw
        return block
    if block_type == "thinking":
        visibility = public_text(value.get("visibility"), max_chars=64, single_line=True).lower()
        if visibility in {"hidden", "internal", "redacted", "debug"} or bool(
            value.get("is_raw_provider_reasoning")
        ):
            return None
        content = public_text(value.get("content"), max_chars=262_144)
        if not content:
            return None
        block = {"type": "thinking", "content": content}
        for key, maximum in (("source", 128), ("visibility", 64), ("phase", 128), ("item_id", 1_024), ("lifecycle", 64)):
            text = public_text(value.get(key), max_chars=maximum, single_line=True)
            if text:
                block[key] = text
        content_index = _nonnegative_int(value.get("content_index"))
        if content_index is not None:
            block["content_index"] = content_index
        return block
    if block_type in {"process", "progress"}:
        allowed = {
            "process": (
                "id", "itemKind", "content", "title", "summary", "source", "status", "role",
                "visibility", "loopId", "iterationId", "parentId", "groupId", "stepId", "skillName",
                "triggerMode", "sourceLevel", "reason",
            ),
            "progress": (
                "id", "stage", "phase", "status", "message", "label", "summary", "visibility",
                "detail", "toolCallId", "toolName", "groupId", "stepId", "iterationId",
            ),
        }[block_type]
        block = {"type": block_type}
        for key in allowed:
            raw = value.get(key)
            if raw is None:
                snake_key = "".join(("_" + char.lower()) if char.isupper() else char for char in key)
                raw = value.get(snake_key)
            text = public_text(raw, max_chars=50_000 if key in {"content", "message", "detail", "summary"} else 2_048)
            if text:
                block[key] = text
        for key in ("timestamp", "seq", "order", "tokenEstimate", "count"):
            raw = value.get(key)
            if raw is None:
                snake_key = "".join(("_" + char.lower()) if char.isupper() else char for char in key)
                raw = value.get(snake_key)
            count = _nonnegative_int(raw)
            if count is not None:
                block[key] = count
        for key in ("defaultCollapsed", "ephemeral"):
            raw = value.get(key)
            if raw is None:
                snake_key = "".join(("_" + char.lower()) if char.isupper() else char for char in key)
                raw = value.get(snake_key)
            if isinstance(raw, bool):
                block[key] = raw
        tool_ids = value.get("toolCallIds", value.get("tool_call_ids"))
        if isinstance(tool_ids, (list, tuple)):
            block["toolCallIds"] = _string_list(tool_ids, maximum=2_048)
        return block
    return None


def _project_context_snapshot(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    ledger = source.get("context_ledger")
    if isinstance(ledger, Mapping):
        safe_ledger: dict[str, Any] = {}
        for key in (
            "schema_version",
            "estimated_tokens",
            "actual_tokens",
            "compaction_count",
            "native_attachment_tokens",
            "native_attachment_count",
        ):
            count = _nonnegative_int(ledger.get(key))
            if count is not None:
                safe_ledger[key] = count
        entries: list[dict[str, Any]] = []
        raw_entries = ledger.get("entries")
        if isinstance(raw_entries, list):
            for raw_entry in raw_entries[:128]:
                if not isinstance(raw_entry, Mapping):
                    continue
                entry: dict[str, Any] = {}
                for key in ("category", "label"):
                    text = public_text(raw_entry.get(key), max_chars=512, single_line=True)
                    if text:
                        entry[key] = text
                for key in ("estimated_tokens", "item_count", "source_count"):
                    count = _nonnegative_int(raw_entry.get(key))
                    if count is not None:
                        entry[key] = count
                sources = _string_list(raw_entry.get("sources"), maximum=256)
                if sources:
                    entry["sources"] = sources
                if entry:
                    entries.append(entry)
        safe_ledger["entries"] = entries
        projected["context_ledger"] = safe_ledger

    ui_state = source.get("ui_agent_state")
    if isinstance(ui_state, Mapping):
        safe_state: dict[str, Any] = {
            "plan": None,
            "todos": [],
            "subagents": [],
            "agentProgress": [],
        }
        plan = ui_state.get("plan")
        if isinstance(plan, Mapping):
            safe_plan: dict[str, Any] = {}
            for source_key, target_key in (("threadId", "threadId"), ("turnId", "turnId"), ("explanation", "explanation")):
                text = public_text(plan.get(source_key), max_chars=12_000 if source_key == "explanation" else 2_048)
                if text:
                    safe_plan[target_key] = text
            raw_steps = plan.get("plan")
            if isinstance(raw_steps, list):
                safe_plan["plan"] = [
                    {
                        "step": public_text(step.get("step"), max_chars=12_000),
                        "status": public_text(step.get("status"), max_chars=64, single_line=True) or "pending",
                    }
                    for step in raw_steps[:256]
                    if isinstance(step, Mapping) and str(step.get("step") or "").strip()
                ]
            safe_state["plan"] = safe_plan
        raw_todos = ui_state.get("todos")
        if isinstance(raw_todos, list):
            for todo in raw_todos[:512]:
                if not isinstance(todo, Mapping):
                    continue
                todo_id = public_text(todo.get("id"), max_chars=2_048, single_line=True)
                content = public_text(todo.get("content"), max_chars=12_000)
                if not todo_id or not content:
                    continue
                safe_state["todos"].append(
                    {
                        "id": todo_id,
                        "content": content,
                        "activeForm": public_text(todo.get("activeForm", todo.get("active_form", content)), max_chars=12_000),
                        "status": public_text(todo.get("status"), max_chars=64, single_line=True) or "pending",
                    }
                )
        raw_subagents = ui_state.get("subagents")
        if isinstance(raw_subagents, list):
            for row in raw_subagents[-20:]:
                if not isinstance(row, Mapping):
                    continue
                safe_row = _public_json(row)
                if isinstance(safe_row, dict) and safe_row.get("id"):
                    safe_state["subagents"].append(safe_row)
        raw_progress = ui_state.get("agentProgress")
        if isinstance(raw_progress, list):
            for row in raw_progress[-80:]:
                block = _project_block({"type": "progress", **dict(row)}) if isinstance(row, Mapping) else None
                if block:
                    # ui_agent_state.agentProgress already supplies the record
                    # kind through its container. Keep the persisted/public
                    # shape stable instead of injecting a transcript block tag.
                    block.pop("type", None)
                    safe_state["agentProgress"].append(block)
        projected["ui_agent_state"] = safe_state
    return projected


def project_public_transcript_message(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    data_url_budget = [_MAX_PUBLIC_DATA_URL_CHARS]
    public_json_text_budget = [_MAX_PUBLIC_JSON_TEXT_CHARS]
    role = public_text(source.get("role"), max_chars=64, single_line=True)
    if not role:
        role = "system"
    projected: dict[str, Any] = {
        "id": public_text(source.get("id"), max_chars=1_024, single_line=True),
        "role": role,
        "content": (
            _public_json(
                source.get("content"),
                preserve_data_urls=True,
                data_url_budget=data_url_budget,
                text_budget=public_json_text_budget,
            )
            if isinstance(source.get("content"), (list, tuple, Mapping))
            else public_text(source.get("content"), max_chars=4_194_304)
        ),
    }
    timestamp = source.get("timestamp")
    if isinstance(timestamp, str):
        projected["timestamp"] = public_text(timestamp, max_chars=128, single_line=True)
    else:
        count = _nonnegative_int(timestamp)
        if count is not None:
            projected["timestamp"] = count
    for key, maximum in (
        ("thinking", 262_144),
        ("terminal_status", 64),
        ("termination_reason", 256),
        ("failure_message", 12_000),
        ("steer_target_message_id", 1_024),
        ("turn_id", 1_024),
        ("api", 256),
        ("wire_api", 256),
        ("provider", 256),
        ("model", 512),
        ("model_id", 512),
        ("stop_reason", 128),
        ("stopReason", 128),
        ("tool_call_id", 1_024),
        ("toolCallId", 1_024),
        ("toolName", 512),
        ("name", 512),
        ("command", 262_144),
        ("output", 262_144),
        ("fullOutputPath", 4_096),
        ("customType", 512),
        ("summary", 262_144),
        ("fromId", 1_024),
    ):
        text = public_text(source.get(key), max_chars=maximum, single_line=maximum <= 1_024)
        if text:
            projected[key] = text
    for source_key, target_key in (
        ("completed_at", "completed_at"),
        ("completedAt", "completed_at"),
        ("duration_ms", "duration_ms"),
        ("durationMs", "duration_ms"),
        ("queue_position", "queue_position"),
        ("exitCode", "exitCode"),
        ("tokensBefore", "tokensBefore"),
    ):
        number = _finite_number(source.get(source_key))
        if number is not None:
            projected[target_key] = number
    for source_key, target_key in (
        ("failure_recoverable", "failure_recoverable"),
        ("failureRecoverable", "failure_recoverable"),
        ("steered", "steered"),
        ("is_error", "is_error"),
        ("cancelled", "cancelled"),
        ("truncated", "truncated"),
        ("excludeFromContext", "excludeFromContext"),
        ("display", "display"),
        ("isError", "isError"),
    ):
        if isinstance(source.get(source_key), bool):
            projected[target_key] = bool(source.get(source_key))
    usage = project_public_usage(source.get("usage"))
    if usage:
        projected["usage"] = usage
    raw_blocks = source.get("blocks")
    if isinstance(raw_blocks, list):
        blocks = [
            block
            for raw_block in raw_blocks[:4_096]
            if (block := _project_block(raw_block)) is not None
        ]
        if blocks:
            projected["blocks"] = blocks
    raw_calls = source.get("tool_calls")
    if isinstance(raw_calls, list):
        calls = [
            project_public_tool_call(raw_call)
            for raw_call in raw_calls[:4_096]
            if isinstance(raw_call, Mapping)
        ]
        calls = [call for call in calls if call.get("id") and call.get("name")]
        if calls:
            projected["tool_calls"] = calls
    for source_key, target_key, preserve_data_urls in (
        ("artifacts", "artifacts", True),
        ("attachments", "attachments", True),
        ("attachmentRefs", "attachmentRefs", True),
        ("context_refs", "context_refs", False),
        ("contextRefs", "context_refs", False),
        ("reply_attachments", "reply_attachments", False),
        ("replyAttachments", "reply_attachments", False),
        ("citations", "citations", False),
        ("tool_result_details", "tool_result_details", False),
        ("details", "details", False),
    ):
        raw = source.get(source_key)
        if isinstance(raw, (list, tuple, Mapping)):
            projected[target_key] = _public_json(
                raw,
                preserve_data_urls=preserve_data_urls,
                data_url_budget=data_url_budget,
                text_budget=public_json_text_budget,
            )
    added_tools = _string_list(source.get("added_tool_names"), maximum=256)
    if not added_tools:
        added_tools = _string_list(source.get("addedToolNames"), maximum=256)
    if added_tools:
        projected["added_tool_names"] = added_tools
        projected["addedToolNames"] = added_tools
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping) and str(metadata.get("source") or "") == "scheduled_task":
        safe_metadata: dict[str, Any] = {"source": "scheduled_task"}
        for key in ("scheduled_task_id", "scheduled_run_id", "stopped_reason"):
            text = public_text(metadata.get(key), max_chars=2_048, single_line=True)
            if text:
                safe_metadata[key] = text
        iterations = _nonnegative_int(metadata.get("iterations"))
        if iterations is not None:
            safe_metadata["iterations"] = iterations
        errors = _string_list(metadata.get("errors"), maximum=64)
        if errors:
            safe_metadata["errors"] = errors
        projected["metadata"] = safe_metadata
    return projected


def project_public_transcript(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        project_public_transcript_message(message)
        for message in value
        if isinstance(message, Mapping)
    ]


def project_public_conversation(value: Any, *, include_transcript: bool = True) -> dict[str, Any]:
    source = value.to_dict() if hasattr(value, "to_dict") else value
    source = source if isinstance(source, Mapping) else {}
    projected: dict[str, Any] = {}
    for key, maximum in (
        ("id", 1_024),
        ("title", 512),
        ("created_at", 128),
        ("updated_at", 128),
        ("conversation_type", 64),
        ("memory_mode", 64),
        ("permission_mode", 64),
        ("summary", 50_000),
        ("compaction_state", 64),
        ("compaction_summary", 262_144),
        ("archived_at", 128),
        ("workspace_root", 4_096),
        ("git_branch", 1_024),
        ("worktree_path", 4_096),
        ("parent_conversation_id", 1_024),
        ("fork_id", 1_024),
        ("branch_kind", 128),
        ("merged_into_conversation_id", 1_024),
        ("merged_at", 128),
    ):
        text = public_text(source.get(key), max_chars=maximum, single_line=maximum <= 1_024)
        if text or key in {"summary", "compaction_summary", "archived_at", "workspace_root", "git_branch", "worktree_path"}:
            projected[key] = text
    for key in ("revision", "message_count"):
        count = _nonnegative_int(source.get(key))
        if count is not None:
            projected[key] = count
    for key in ("memory_polluted", "archived", "git_isolated"):
        if isinstance(source.get(key), bool):
            projected[key] = bool(source.get(key))
    pollution_sources = _string_list(source.get("memory_pollution_sources"), maximum=256)
    projected["memory_pollution_sources"] = pollution_sources
    deny_rules = _string_list(source.get("permission_deny_rules"), maximum=512)
    if deny_rules:
        projected["permission_deny_rules"] = deny_rules
    overrides = source.get("permission_overrides")
    if isinstance(overrides, Mapping):
        projected["permission_overrides"] = {
            public_text(key, max_chars=4_096): public_text(level, max_chars=64, single_line=True)
            for key, level in list(overrides.items())[:512]
            if public_text(key, max_chars=4_096)
        }
    goal = source.get("goal")
    if isinstance(goal, Mapping):
        safe_goal: dict[str, Any] = {}
        for key, maximum in (
            ("id", 1_024),
            ("text", 4_000),
            ("status", 64),
            ("created_at", 128),
            ("updated_at", 128),
            ("source", 256),
        ):
            text = public_text(goal.get(key), max_chars=maximum, single_line=key != "text")
            if text:
                safe_goal[key] = text
        projected["goal"] = safe_goal
    parent_index = source.get("parent_message_index")
    if parent_index is None:
        projected["parent_message_index"] = None
    else:
        count = _nonnegative_int(parent_index)
        if count is not None:
            projected["parent_message_index"] = count
    if include_transcript:
        transcript = project_public_transcript(source.get("transcript"))
        projected["transcript"] = transcript
        projected["message_count"] = len(transcript)
    projected["context_snapshot"] = _project_context_snapshot(source.get("context_snapshot"))
    return projected


def project_public_conversation_summary(value: Any) -> dict[str, Any]:
    return project_public_conversation(value, include_transcript=False)


__all__ = [
    "project_public_conversation",
    "project_public_conversation_summary",
    "project_public_tool_call",
    "project_public_transcript",
    "project_public_transcript_message",
]
