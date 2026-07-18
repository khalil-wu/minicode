from __future__ import annotations

import re
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.progress import agent_progress
from backend.agent.state import AgentState
from backend.agent.tool_projection import (
    sanitize_internal_tool_names_for_user_text,
    user_facing_tool_name,
)


def _iteration_id(state: AgentState) -> str:
    return f"iter:{max(1, state.iterations)}"


def timeout_tool_result_reply(state: AgentState) -> str:
    return tool_result_fallback_reply(
        state,
        reason="模型在工具执行完成后响应超时。",
    )


def successful_tool_result_records(state: AgentState) -> list[Any]:
    successful = [
        tc for tc in state.tool_calls
        if getattr(tc, "status", "") in {"success", "partial"}
        and is_user_visible_tool_output(str(getattr(tc, "tool_output", "") or ""))
    ]
    return successful


_NON_FATAL_TOOL_ERROR_KINDS = {
    "missing_generated_content",
    "routing_error",
    "stale_evidence",
    "repeat_guard",
    "tool_disabled",
}


def is_nonfatal_tool_record(record: Any) -> bool:
    if str(getattr(record, "projection", "") or "") in {"silent", "status", "warning"}:
        return True
    return str(getattr(record, "error_kind", "") or "") in _NON_FATAL_TOOL_ERROR_KINDS


def is_failed_tool_record(record: Any) -> bool:
    status = str(getattr(record, "status", "") or "")
    return status in {"error", "failed", "blocked"} and not is_nonfatal_tool_record(record)


def is_user_visible_tool_output(output: str) -> bool:
    text = output.strip()
    if not text:
        return False
    lower = text.lower()
    internal_markers = (
        " is disabled for this turn.",
        "invalid tool call",
        "invalid web_search call",
        "invalid web_fetch call",
        "do not call web",
        "ask one concise clarification question",
    )
    return not any(marker in lower for marker in internal_markers)


_CJK_TEXT_RE = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
    r"\u3040-\u309F\u30A0-\u30FF"
    r"\uAC00-\uD7AF]"
)
_TIMEOUT_AFTER_TOOLS_REASON = "The model response timed out after the tools completed."


def prefers_chinese_fallback(state: AgentState) -> bool:
    return bool(_CJK_TEXT_RE.search(str(getattr(state, "user_message", "") or "")))


def fallback_copy(state: AgentState, *, reason: str = "") -> dict[str, str]:
    stock_reason = reason.strip()
    if prefers_chinese_fallback(state):
        if stock_reason == _TIMEOUT_AFTER_TOOLS_REASON:
            intro = "\u5de5\u5177\u5b8c\u6210\u540e\uff0c\u6a21\u578b\u54cd\u5e94\u8d85\u65f6\u4e86\u3002"
        else:
            intro = stock_reason or "\u6a21\u578b\u5728\u751f\u6210\u6700\u7ec8\u56de\u590d\u524d\u88ab\u4e2d\u65ad\u3002"
        return {
            "intro": intro,
            "retrieved": "\u57fa\u4e8e\u5df2\u5b8c\u6210\u5de5\u5177\u7ed3\u679c\u7684\u6062\u590d\u6458\u8981\uff1a",
            "read_file": "\u6587\u4ef6\u5185\u5bb9\u5df2\u8bfb\u53d6\uff1b\u7531\u4e8e\u5185\u5bb9\u8f83\u957f\uff0c\u5b8c\u6574\u5185\u5bb9\u5df2\u4fdd\u5b58\u4e3a\u5185\u90e8\u4ea7\u7269\u3002",
            "read_artifact": "\u5185\u90e8\u4ea7\u7269\u5df2\u8bfb\u53d6\uff1b\u5185\u5bb9\u8f83\u957f\uff0c\u6062\u590d\u6458\u8981\u4e2d\u5df2\u7701\u7565\u539f\u59cb\u5185\u5bb9\uff0c\u907f\u514d\u628a\u539f\u59cb\u6587\u4ef6\u5185\u5bb9\u5f53\u6210\u6700\u7ec8\u56de\u7b54\u3002",
            "candidate": "\u90e8\u5206\u7ed3\u679c\u4ec5\u4e3a\u5019\u9009\u8bc1\u636e\uff0c\u8bf7\u4f5c\u4e3a\u53c2\u8003\u7ebf\u7d22\u800c\u975e\u5b8c\u5168\u786e\u8ba4\u7684\u7ed3\u8bba\u3002",
        }

    return {
        "intro": stock_reason or "The model was interrupted before producing a final reply.",
        "retrieved": "Recovery summary based on completed tool results:",
        "read_file": "File content was read; due to length, the full content is saved as an internal artifact.",
        "read_artifact": "Internal artifact was read; raw content is omitted from this recovery summary to avoid treating the original file content as the final answer.",
        "candidate": "Some of these results are only candidate evidence; treat them as reference clues, not fully confirmed conclusions.",
    }


def stream_text_events(
    content: str,
    *,
    source: str,
    visibility: str,
    phase: str | None = None,
    chunk_chars: int = 80,
) -> list[AgentEvent]:
    text = str(content or "")
    if not text:
        return [AgentEvent.text_chunk("", source=source, visibility=visibility, phase=phase)]
    chunks: list[str] = []
    current = ""
    for piece in re.split(r"(\s+)", text):
        if not piece:
            continue
        if current and len(current) + len(piece) > chunk_chars:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return [
        AgentEvent.text_chunk(chunk, source=source, visibility=visibility, phase=phase)
        for chunk in chunks
    ]


def fallback_final_text_events(content: str) -> list[AgentEvent]:
    return stream_text_events(content, source="fallback", visibility="final", phase="final")


def fallback_recovery_text_events(content: str) -> list[AgentEvent]:
    return stream_text_events(content, source="fallback", visibility="timeline", phase="recover")


def fallback_recovery_progress_event(
    state: AgentState,
    *,
    event_id: str,
    summary: str,
) -> AgentEvent:
    chinese = prefers_chinese_fallback(state)
    return agent_progress(
        "\u6b63\u5728\u4f7f\u7528\u5df2\u5b8c\u6210\u7684\u5de5\u5177\u7ed3\u679c\u751f\u6210\u6062\u590d\u6458\u8981"
        if chinese else
        "Using completed tool results to produce a recovery answer",
        stage="status",
        status="completed",
        id=event_id,
        phase="recover",
        label="\u6062\u590d\u6458\u8981" if chinese else "Recovery",
        summary=summary,
        visibility="timeline",
        step_id=f"recover:{state.iterations}",
        iteration_id=_iteration_id(state),
    )


def tool_result_fallback_reply(state: AgentState, *, reason: str = "") -> str:
    successful = successful_tool_result_records(state)
    if not successful:
        return ""

    selected = successful[-3:]
    copy = fallback_copy(state, reason=reason)
    cjk = prefers_chinese_fallback(state)
    parts = [
        f"{copy['intro']} {copy['retrieved']}"
    ]
    for index, record in enumerate(selected, start=1):
        name = str(getattr(record, "tool_name", "") or "tool")
        output = str(getattr(record, "tool_output", "") or "").strip()
        if name == "read_file" and str(getattr(record, "artifact_id", "") or "").strip():
            output = copy["read_file"]
        elif name == "read_artifact":
            output = copy["read_artifact"]
        if len(output) > 900:
            output = output[:900].rstrip() + "..."
        output = sanitize_internal_tool_names_for_user_text(output, cjk=cjk)
        metadata: list[str] = []
        source_url = str(getattr(record, "source_url", "") or "").strip()
        evidence_type = str(getattr(record, "evidence_type", "") or "").strip()
        extraction_status = str(getattr(record, "extraction_status", "") or "").strip()
        if source_url:
            metadata.append(f"source: {source_url}")
        if evidence_type:
            metadata.append(f"evidence: {evidence_type}")
        if extraction_status:
            metadata.append(f"extraction: {extraction_status}")
        suffix = f" ({'; '.join(metadata)})" if metadata else ""
        display_name = user_facing_tool_name(name, cjk=cjk)
        parts.append(f"{index}. {display_name}{suffix}\n{output}")
    if any(str(getattr(record, "evidence_type", "") or "") == "candidate" for record in selected):
        parts.append(copy["candidate"])
    return "\n\n".join(parts)


def failed_tool_result_fallback_reply(state: AgentState, *, reason: str = "") -> str:
    failed = [record for record in state.tool_calls if is_failed_tool_record(record)]
    if not failed:
        failed = [
            record for record in state.tool_calls
            if str(getattr(record, "status", "") or "") in {"error", "failed", "blocked"}
        ]
    if not failed:
        return ""

    if prefers_chinese_fallback(state):
        intro = (
            reason.strip()
            or "\u5de5\u5177\u8c03\u7528\u5931\u8d25\u540e\uff0c\u6a21\u578b\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u56de\u590d\u3002"
            "\u8fd9\u8f6e\u6ca1\u6709\u5b8c\u6210\uff0c\u4e0b\u9762\u662f\u5931\u8d25\u539f\u56e0\uff1a"
        )
        no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u5931\u8d25\u7ec6\u8282\u3002"
    else:
        intro = (
            reason.strip()
            or "Tool calls failed and the model did not produce a final reply. "
            "This turn did not complete; here is what failed:"
        )
        no_details = "The tool did not return usable failure details."

    parts = [intro]
    cjk = prefers_chinese_fallback(state)
    for index, record in enumerate(failed[-3:], start=1):
        name = user_facing_tool_name(
            str(getattr(record, "tool_name", "") or "tool"),
            cjk=cjk,
        )
        status = str(getattr(record, "status", "") or "failed")
        output = str(getattr(record, "tool_output", "") or "").strip()
        user_summary = str(getattr(record, "user_summary", "") or "").strip()
        developer_detail = str(getattr(record, "developer_detail", "") or "").strip()
        if user_summary and user_summary not in output:
            output = f"{user_summary}\n{output}" if output else user_summary
        if not output and developer_detail:
            output = developer_detail
        if not output:
            output = no_details
        if len(output) > 700:
            output = output[:700].rstrip() + "..."
        output = sanitize_internal_tool_names_for_user_text(output, cjk=cjk)
        parts.append(f"{index}. {name} [{status}]\n{output}")
    return "\n\n".join(parts)
