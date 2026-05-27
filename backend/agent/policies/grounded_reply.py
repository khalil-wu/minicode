"""Stop quality gate — detects weak final replies and returns feedback for retry.

Design principle (Claude Code alignment):
  harness 负责协议、证据、权限、生命周期。
  LLM 负责理解、选择工具、综合答案。
  Stop gate 只判断质量，不替模型写答案。

This module does NOT call the LLM to generate a replacement answer.
It returns a feedback string that the agent loop injects as a user-role
message, causing the model to retry with awareness of what went wrong.

Web evidence rules (domain-agnostic):
  1. search-only: model searched but never fetched → must fetch before answering
  2. all-fetch-failed: every fetch failed → must try alternative or state evidence gap
  3. partial-overcertain: partial extraction + overly confident reply → must hedge + cite
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_WEB_RESULT_TOOL_NAMES = {
    "mcp__websearch__search",
    "mcp__websearch__fetch_page",
    "web_search",
    "web_fetch",
    "read_artifact",
}

_SEARCH_TOOL_NAMES = {"web_search", "mcp__websearch__search"}
_FETCH_TOOL_NAMES = {"web_fetch", "mcp__websearch__fetch_page"}

_PLACEHOLDER_SUBSTRINGS = (
    "我先查一下",
    "我来查一下",
    "我先帮你查一下",
    "没有直接提取到具体数值",
    "没有直接提取",
    "你可以先看这个",
    "如果你愿意",
    "我可以继续",
    "可以继续帮你",
    "let me check",
    "let me look that up",
    "i'll check",
    "i can continue",
    "if you want",
)

_FALSE_CAPABILITY_SUBSTRINGS = (
    "不能联网",
    "无法联网",
    "没有搜索工具",
    "无法获取实时",
    "无法查询实时",
    "cannot browse",
    "can't browse",
    "cannot access realtime",
    "cannot access real-time",
    "search is unavailable",
)


class StopQualityGate(Protocol):
    """Protocol for stop quality gates."""

    def evaluate(self, user_message: str, draft_reply: str, state: Any) -> str | None:
        """Evaluate draft reply quality.

        Returns None if the reply passes, or a feedback string to inject
        as a user message for the model to retry.
        """
        ...


def _successful_tool_results(state: Any) -> list[Any]:
    return [
        tool_call
        for tool_call in getattr(state, "tool_calls", [])
        if getattr(tool_call, "status", "") == "success"
        and str(getattr(tool_call, "tool_output", "") or "").strip()
    ]


def _has_web_grounding_result(state: Any) -> bool:
    return any(
        getattr(tool_call, "tool_name", "") in _WEB_RESULT_TOOL_NAMES
        for tool_call in _successful_tool_results(state)
    )


def _is_empty_or_placeholder(draft_reply: str) -> bool:
    stripped = draft_reply.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return any(item in lowered for item in _PLACEHOLDER_SUBSTRINGS)


def _claims_missing_capability(draft_reply: str, state: Any) -> bool:
    lowered = draft_reply.strip().lower()
    return _has_web_grounding_result(state) and any(
        item in lowered for item in _FALSE_CAPABILITY_SUBSTRINGS
    )


def _build_tool_summary(state: Any, limit: int = 5) -> str:
    lines: list[str] = []
    for tool_call in _successful_tool_results(state)[-limit:]:
        name = getattr(tool_call, "tool_name", "?")
        output = str(getattr(tool_call, "tool_output", "") or "").strip()
        if len(output) > 300:
            output = f"{output[:300]}..."
        lines.append(f"- {name}: {output}")
    return "\n".join(lines) if lines else ""


# ── Web evidence helpers (domain-agnostic) ──────────────────────────────────


def _get_evidence_type(record: Any) -> str | None:
    """Read structured evidence_type from ToolCallRecord (set by Codex's store_result)."""
    return getattr(record, "evidence_type", None)


def _get_extraction_status(record: Any) -> str | None:
    """Read structured extraction_status from ToolCallRecord."""
    return getattr(record, "extraction_status", None)


def _classify_web_evidence(state: Any) -> dict[str, Any]:
    """Classify web tool usage into structured evidence categories.

    Returns dict with:
      has_search: bool — any search tool was called successfully
      has_fetch: bool — any fetch tool was called
      fetch_statuses: list[str] — extraction_status of each fetch ("ok"/"partial"/"failed")
      search_only: bool — searched but never fetched
      all_fetch_failed: bool — fetched but all failed
      has_partial: bool — at least one fetch returned partial
    """
    has_search = False
    has_fetch = False
    fetch_statuses: list[str] = []

    for record in getattr(state, "tool_calls", []):
        if getattr(record, "status", "") != "success":
            continue
        tool_name = getattr(record, "tool_name", "")

        # Detect search tools
        if tool_name in _SEARCH_TOOL_NAMES:
            has_search = True
            # Also check evidence_type field (once Codex adds it)
            if _get_evidence_type(record) == "candidate":
                has_search = True

        # Detect fetch tools
        if tool_name in _FETCH_TOOL_NAMES:
            has_fetch = True
            status = _get_extraction_status(record)
            if status:
                fetch_statuses.append(status)
            else:
                # Fallback: infer from tool_output if structured field not yet available
                output = str(getattr(record, "tool_output", "") or "")
                if "extraction: failed" in output:
                    fetch_statuses.append("failed")
                elif "extraction: partial" in output:
                    fetch_statuses.append("partial")
                elif "extraction: ok" in output:
                    fetch_statuses.append("ok")
                elif output.strip():
                    fetch_statuses.append("ok")

    # Also check by evidence_type (covers MCP tools with non-standard names)
    for record in getattr(state, "tool_calls", []):
        if getattr(record, "status", "") != "success":
            continue
        ev_type = _get_evidence_type(record)
        if ev_type == "candidate" and not has_search:
            has_search = True
        if ev_type == "fetched" and not has_fetch:
            has_fetch = True
            status = _get_extraction_status(record)
            if status:
                fetch_statuses.append(status)

    return {
        "has_search": has_search,
        "has_fetch": has_fetch,
        "fetch_statuses": fetch_statuses,
        "search_only": has_search and not has_fetch,
        "all_fetch_failed": has_fetch and bool(fetch_statuses) and all(s == "failed" for s in fetch_statuses),
        "has_partial": "partial" in fetch_statuses,
    }


# Hedging/uncertainty markers (domain-agnostic)
_HEDGING_PATTERNS = (
    "根据.*来源",
    "根据.*搜索",
    "据.*显示",
    "来源.*显示",
    "参考.*链接",
    "可能",
    "大约",
    "约",
    "似乎",
    "看起来",
    "不确定",
    "仅供参考",
    "according to",
    "based on",
    "approximately",
    "appears to",
    "seems",
    "may be",
    "might be",
    "source:",
    "来源:",
    "参考:",
)

_SOURCE_CITATION_PATTERN = re.compile(
    r"https?://|来源[：:]|参考[：:]|source[：:]|ref[：:]",
    re.IGNORECASE,
)

# Citation markers for Rule 6 (fetched evidence requires citation)
_CITATION_MARKER_PATTERN = re.compile(
    r"\[\d+\]"           # [1], [2], etc.
    r"|https?://"        # full URL
    r"|来源[：:\s]"      # 来源: / 来源：
    r"|参考[：:\s]"      # 参考: / 参考：
    r"|引用[：:\s]"      # 引用: / 引用：
    r"|sources?\b"       # source / sources
    r"|references?\b",   # reference / references
    re.IGNORECASE,
)


def _reply_has_source_marker(draft_reply: str) -> bool:
    """Check if the reply contains at least one source citation marker.

    This is stricter than _reply_has_hedging_or_citation — it only looks for
    explicit source markers, not hedging words like '可能' or '大约'.
    """
    return bool(_CITATION_MARKER_PATTERN.search(draft_reply))


def _reply_has_hedging_or_citation(draft_reply: str) -> bool:
    """Check if the reply contains uncertainty markers or source citations."""
    lowered = draft_reply.lower()
    if _SOURCE_CITATION_PATTERN.search(draft_reply):
        return True
    return any(marker in lowered for marker in _HEDGING_PATTERNS)


def _reply_presents_as_fact(draft_reply: str) -> bool:
    """Heuristic: reply states information confidently without hedging or citation."""
    if not draft_reply.strip():
        return False
    if len(draft_reply.strip()) < 20:
        return False
    return not _reply_has_hedging_or_citation(draft_reply)


def _has_citable_web_evidence(state: Any) -> bool:
    """Check if state has fetched web evidence that should be cited.

    Returns True if any tool call has:
      - evidence_type == "fetched"
      - extraction_status is "ok", "partial", or absent (but not "failed")
      - status is not error
    """
    for record in getattr(state, "tool_calls", []):
        if getattr(record, "status", "") != "success":
            continue

        ev_type = _get_evidence_type(record)
        tool_name = getattr(record, "tool_name", "")

        # Match by evidence_type field (preferred, set by Codex)
        if ev_type == "fetched":
            ext_status = _get_extraction_status(record)
            if ext_status != "failed":
                return True

        # Fallback: match by tool name for fetch tools
        if tool_name in _FETCH_TOOL_NAMES:
            ext_status = _get_extraction_status(record)
            if ext_status == "failed":
                continue
            # If no structured field, check output string
            if ext_status is None:
                output = str(getattr(record, "tool_output", "") or "")
                if "extraction: failed" in output:
                    continue
            return True

    return False


class DefaultStopQualityGate:
    """Detect weak final replies and produce feedback for model retry.

    Does NOT generate answers. Returns a feedback string that tells the model
    what went wrong and what tool results are available.
    """

    def __init__(self, max_retries: int = 2) -> None:
        self._max_retries = max_retries

    def evaluate(self, user_message: str, draft_reply: str, state: Any) -> str | None:
        retry_count = getattr(state, "stop_gate_retries", 0)
        if retry_count >= self._max_retries:
            return None

        # Rule 1: empty/placeholder reply with available tool results
        if _is_empty_or_placeholder(draft_reply):
            tool_summary = _build_tool_summary(state)
            if not tool_summary:
                return None
            state.stop_gate_retries = retry_count + 1
            return (
                "[系统反馈] 你的回答为空或只是占位语句。"
                "工具已经返回了结果，请直接基于以下工具输出回答用户问题，不要再询问是否继续：\n"
                f"{tool_summary}"
            )

        # Rule 2: claims missing capability despite successful tool results
        if _claims_missing_capability(draft_reply, state):
            tool_summary = _build_tool_summary(state)
            state.stop_gate_retries = retry_count + 1
            return (
                "[系统反馈] 你声称无法联网或没有搜索工具，但工具已经成功执行并返回了结果。"
                "请直接基于以下工具输出回答用户问题：\n"
                f"{tool_summary}"
            )

        # Web evidence rules (domain-agnostic)
        evidence = _classify_web_evidence(state)

        # Rule 3: search-only — searched but never fetched actual content
        if evidence["search_only"] and draft_reply.strip():
            state.stop_gate_retries = retry_count + 1
            return (
                "[系统反馈] 你只执行了搜索（返回候选来源列表），但没有用 web_fetch 抓取任何页面的实际内容。"
                "搜索摘要不可靠，请对最相关的候选 URL 调用 web_fetch 获取一手内容后再回答。"
            )

        # Rule 4: all fetches failed — no usable first-hand content
        if evidence["all_fetch_failed"]:
            state.stop_gate_retries = retry_count + 1
            return (
                "[系统反馈] 所有 web_fetch 调用均返回 failed 状态，没有获取到可用的一手内容。"
                "请尝试其他候选 URL，或明确告知用户当前无法获取可靠信息并说明已尝试的来源。"
                "不要基于搜索摘要编造答案。"
            )

        # Rule 5: partial extraction + overly confident reply
        if evidence["has_partial"] and _reply_presents_as_fact(draft_reply):
            state.stop_gate_retries = retry_count + 1
            return (
                "[系统反馈] 抓取的网页内容为 partial（不完整提取），但你的回答过于确定，没有标注不确定性或来源。"
                '请在回答中注明信息来源 URL，并对不完整提取的部分使用适当的不确定性表述（如「根据...显示」「可能」等）。'
            )

        # Rule 6: fetched web evidence exists but reply has no source citation
        if _has_citable_web_evidence(state) and not _reply_has_source_marker(draft_reply):
            if draft_reply.strip() and len(draft_reply.strip()) >= 20:
                state.stop_gate_retries = retry_count + 1
                return (
                    "[系统反馈] 你的回答基于已抓取的网页内容，但没有标注任何来源引用。"
                    "请在回答中添加来源标记（如 [1] URL 或「来源：...」），"
                    "让用户能追溯信息出处。"
                )

        return None


# Backward compatibility: keep the old protocol name as an alias
class GroundedReplyPolicy(Protocol):
    """Deprecated — use StopQualityGate instead."""

    async def maybe_produce_grounded_reply(
        self,
        user_message: str,
        draft_reply: str,
        state: Any,
        llm: Any,
    ) -> str | None: ...


class DefaultGroundedReplyPolicy:
    """Adapter: wraps DefaultStopQualityGate in the old async interface.

    Returns None (pass) or feedback string. The agent loop is responsible
    for injecting feedback as a user message — this class no longer calls
    the LLM to generate replacement answers.
    """

    def __init__(self) -> None:
        self._gate = DefaultStopQualityGate()

    async def maybe_produce_grounded_reply(
        self,
        user_message: str,
        draft_reply: str,
        state: Any,
        llm: Any,
    ) -> str | None:
        return self._gate.evaluate(user_message, draft_reply, state)
