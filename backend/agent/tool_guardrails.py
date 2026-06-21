from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from backend.agent.tool_common import WEB_SEARCH_TOOL_NAMES, WEB_TOOL_NAMES, _text_arg



# ---------------------------------------------------------------------------
# Hermes-style progressive tool-call guardrail (warn → block → halt)
# ---------------------------------------------------------------------------


IDEMPOTENT_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "grep_files", "glob_files", "fuzzy_search",
    "web_search", "web_fetch", "read_artifact", "read_memory", "recall_memory",
})

MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "run_command", "write_file", "edit_file", "git_commit",
    "save_memory", "remember_memory",
})

MAX_WEB_OPS_PER_TURN = 8
MAX_WEB_SEARCH_CALLS_PER_TURN = 5
SIMILAR_WEB_SEARCH_BLOCK_AFTER = 2
SIMILAR_EMPTY_WEB_SEARCH_BLOCK_AFTER = 2

_SEARCH_QUERY_STOP_TOKENS = {
    "http",
    "https",
    "www",
    "com",
    "org",
    "net",
    "site",
    "search",
    "query",
}
_EMPTY_SEARCH_MARKERS = (
    "no results",
    "no search results",
    "returned no results",
    "no result returned",
    "未返回结果",
    "没有返回结果",
)


def _search_query_tokens(query: str) -> frozenset[str]:
    text = str(query or "").lower()
    text = re.sub(r"\bsite\s*:\s*", " ", text)
    raw_tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text)
    tokens = {
        token
        for token in raw_tokens
        if token not in _SEARCH_QUERY_STOP_TOKENS
        and (len(token) > 1 or token.isdigit() or re.match(r"[\u4e00-\u9fff]", token))
    }
    return frozenset(tokens)


def _search_query_similarity(left: str, right: str) -> float:
    left_tokens = _search_query_tokens(left)
    right_tokens = _search_query_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    if left_tokens == right_tokens:
        return 1.0
    intersection = len(left_tokens & right_tokens)
    overlap = intersection / max(min(len(left_tokens), len(right_tokens)), 1)
    jaccard = intersection / max(len(left_tokens | right_tokens), 1)
    return max(overlap, jaccard)


def _search_queries_are_similar(left: str, right: str) -> bool:
    return _search_query_similarity(left, right) >= 0.75


def _record_search_query(record: Any) -> str:
    args = getattr(record, "tool_input", None)
    if not isinstance(args, dict):
        args = getattr(record, "arguments", None)
    return _text_arg((args or {}).get("query")) or _text_arg(args or {})


def _search_output_is_empty(record: Any) -> bool:
    output = str(getattr(record, "tool_output", "") or "").lower()
    extraction = str(getattr(record, "extraction_status", "") or "").lower()
    return extraction == "failed" or any(marker in output for marker in _EMPTY_SEARCH_MARKERS)


def _canonical_tool_args(tool_name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    if tool_name in WEB_SEARCH_TOOL_NAMES:
        query = _text_arg((args or {}).get("query")) or _text_arg(args or {})
        tokens = sorted(_search_query_tokens(query))
        if tokens:
            return {"query_tokens": tokens}
    return dict(args or {})


def _legacy_web_guard_reason(
    state: Any,
    tc: Any,
    *,
    queued_tool_calls: list[Any] | None = None,
) -> str:
    """Return soft-stop guidance when web calls exceed per-turn budgets."""
    if tc.name not in WEB_TOOL_NAMES:
        return ""

    prior_web = [
        record
        for record in getattr(state, "tool_calls", [])
        if getattr(record, "tool_name", "") in WEB_TOOL_NAMES
    ]
    queued_web = [
        queued
        for queued in (queued_tool_calls or [])
        if queued.name in WEB_TOOL_NAMES
    ]
    total_web_ops = len(prior_web) + len(queued_web)
    if total_web_ops >= MAX_WEB_OPS_PER_TURN:
        return (
            f"搜索预算已达：本轮已执行或排队 {total_web_ops} 次网络操作。"
            "请使用已有结果回答，或基于已有证据给出带有不确定性的回答。"
        )

    if tc.name not in WEB_SEARCH_TOOL_NAMES:
        return ""

    prior_searches = [
        record
        for record in prior_web
        if getattr(record, "tool_name", "") in WEB_SEARCH_TOOL_NAMES
    ]
    queued_searches = [
        queued
        for queued in queued_web
        if queued.name in WEB_SEARCH_TOOL_NAMES
    ]
    total_searches = len(prior_searches) + len(queued_searches)
    if total_searches >= MAX_WEB_SEARCH_CALLS_PER_TURN:
        return (
            f"搜索预算已达：本轮已执行或排队 {total_searches} 次网页搜索。"
            "请使用已有结果回答，抓取已知来源，或基于不确定性回答。"
        )

    query = _text_arg((tc.arguments or {}).get("query")) or _text_arg(tc.arguments or {})
    if not query:
        return ""
    recent_empty = 0
    for record in reversed(prior_searches):
        output = str(getattr(record, "tool_output", "") or "").lower()
        if not any(
            marker in output
            for marker in (
                "no results",
                "no search results",
                "returned no results",
                "未返回结果",
                "没有返回结果",
            )
        ):
            break
        recent_empty += 1
    if recent_empty >= MAX_WEB_SEARCH_CALLS_PER_TURN:
        return (
            f"连续 {recent_empty} 次网页搜索没有返回结果。"
            "请尝试不同来源类型、抓取已知 URL，或基于当前证据回答。"
        )
    return ""


def web_guard_reason(
    state: Any,
    tc: Any,
    *,
    queued_tool_calls: list[Any] | None = None,
) -> str:
    """Return hard-stop guidance for repeated no-progress web searches.

    Search volume limits are soft guidance emitted after successful calls. This
    pre-call guard only blocks calls that look like the same search path again.
    """
    del queued_tool_calls
    if tc.name not in WEB_SEARCH_TOOL_NAMES:
        return ""

    query = _text_arg((tc.arguments or {}).get("query")) or _text_arg(tc.arguments or {})
    if not query:
        return ""

    prior_searches = [
        record
        for record in getattr(state, "tool_calls", [])
        if getattr(record, "tool_name", "") in WEB_SEARCH_TOOL_NAMES
    ]
    similar_records = [
        record
        for record in prior_searches
        if _search_queries_are_similar(query, _record_search_query(record))
    ]
    empty_similar_records = [
        record for record in similar_records if _search_output_is_empty(record)
    ]

    if len(empty_similar_records) >= SIMILAR_EMPTY_WEB_SEARCH_BLOCK_AFTER:
        return (
            f"\u8fde\u7eed {len(empty_similar_records)} \u6b21\u76f8\u4f3c\u7f51\u9875\u641c\u7d22\u672a\u8fd4\u56de\u7ed3\u679c\u3002"
            "\u8bf7\u6362\u7528\u660e\u663e\u4e0d\u540c\u7684\u5173\u952e\u8bcd\u3001\u4e0d\u540c\u6765\u6e90\u7c7b\u578b\uff0c"
            "\u6293\u53d6\u5df2\u77e5 URL\uff0c\u6216\u57fa\u4e8e\u5f53\u524d\u8bc1\u636e\u56de\u7b54\u3002"
        )

    if len(similar_records) >= SIMILAR_WEB_SEARCH_BLOCK_AFTER:
        return (
            f"\u4f60\u5df2\u7ecf {len(similar_records)} \u6b21\u641c\u7d22\u76f8\u540c\u5173\u952e\u8bcd\u6216\u9ad8\u5ea6\u76f8\u4f3c\u7684\u67e5\u8be2\u3002"
            "\u8bf7\u505c\u6b62\u91cd\u590d\u641c\u7d22\uff1a\u5148\u4f7f\u7528\u5df2\u6709\u7ed3\u679c\u603b\u7ed3\uff0c"
            "\u6216\u6362\u7528\u771f\u6b63\u4e0d\u540c\u7684\u89d2\u5ea6\u3001\u8bed\u8a00\u3001\u65f6\u95f4\u8303\u56f4\u6216\u6743\u5a01\u6765\u6e90\u3002"
        )

    return ""


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Claude Code-aligned thresholds: block early on exact repeats,
    halt when the same tool keeps failing with different args.
    Trust the model to self-correct after 1 warning, but block
    before wasting more than 2-3 identical attempts.

    Three dimensions, each with independent warn/block thresholds:
      1. exact_failure — same tool + same args failing repeatedly
      2. same_tool_failure — same tool name failing with any args
      3. idempotent_no_progress — read-only tool returning identical results
      4. repeated_call — same tool + same args called too many times (regardless of result)
    """

    exact_failure_warn_after: int = 1
    exact_failure_block_after: int = 1
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 5
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 4
    repeated_call_warn_after: int = 2
    repeated_call_block_after: int = 4


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable identity for a tool name + canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: dict[str, Any] | None) -> "ToolCallSignature":
        canonical_args = _canonical_tool_args(tool_name, args)
        canonical = json.dumps(canonical_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return cls(tool_name=tool_name, args_hash=hashlib.sha256(canonical.encode()).hexdigest()[:16])


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Action-oriented guidance for recovering from repeated tool failures."""
    common = (
        f"{tool_name} 在本轮已失败 {count} 次，看起来像循环。"
        "不要切换到纯文本回复；继续使用工具，但请在重试前先诊断问题。"
        "先检查最新错误/输出并验证你的假设。"
    )
    if tool_name in ("run_command", "terminal"):
        return common + (
            "对于终端失败，先运行一个小诊断，再尝试绝对路径、更简单的命令、"
            "不同的工作目录，或 read_file/write_file 等不同工具。"
        )
    if tool_name in ("web_search", "web_fetch"):
        return common + (
            "对于网络失败，尝试不同查询、更简单的搜索，或基于已有证据回答。"
            "如果网络不可达，请诚实说明阻塞原因。"
        )
    return common + (
        "尝试不同参数、更窄的查询，或换一个能推进任务的工具。"
        "如果阻塞来自外部，请说明阻塞原因，而不是重复同一条失败路径。"
    )


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls.

    Hermes-style progressive escalation: warn → block → halt.
    Warnings are appended to tool results so the model can self-correct.
    Blocks/halts prevent execution and break the loop.
    """

    def __init__(
        self,
        config: ToolCallGuardrailConfig | None = None,
        *,
        is_idempotent: Callable[[str, dict[str, Any] | None], bool] | None = None,
    ):
        self.config = config or ToolCallGuardrailConfig()
        self._is_idempotent_override = is_idempotent
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        self._total_web_calls: int = 0
        self._call_counts: dict[ToolCallSignature, int] = {}

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def before_call(self, tool_name: str, args: dict[str, Any] | None) -> ToolGuardrailDecision:
        """Check if a tool call should be blocked BEFORE execution."""
        # If halt was already triggered by after_call, block all subsequent calls
        if self._halt_decision is not None:
            return ToolGuardrailDecision(
                action="block",
                code=self._halt_decision.code,
                message=f"已触发硬停（{self._halt_decision.code}），等待循环退出。",
                tool_name=tool_name,
                count=self._halt_decision.count,
            )

        signature = ToolCallSignature.from_call(tool_name, args)

        # Dimension 2: same-tool failure halt (pre-execution check)
        same_count = self._same_tool_failure_counts.get(tool_name, 0)
        if same_count >= self.config.same_tool_failure_halt_after:
            decision = ToolGuardrailDecision(
                action="halt",
                code="same_tool_failure_halt",
                message=(
                    f"{tool_name} 在本轮已用不同参数失败 {same_count} 次。"
                    "请切换到根本不同的方法，或基于已有上下文回答。"
                ),
                tool_name=tool_name,
                count=same_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        # Dimension 1: exact failure block
        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            return ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure",
                message=(
                    f"这个 {tool_name} 调用已用相同参数失败 {exact_count} 次。"
                    "请换一种方法：修改参数、使用不同工具，或向用户说明阻塞进展的原因。"
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )

        # Dimension 3: idempotent no-progress block
        if self._is_idempotent(tool_name, args):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    is_web = tool_name in ("web_search", "web_fetch")
                    msg = (
                        f"这个搜索已返回相同结果 {repeat_count} 次，"
                        "上下文中的信息可能已经完整。"
                    )
                    if is_web:
                        msg += (
                            "请现在基于已有结果组织回答。"
                            "用 [1]、[2] 标记引用来源。"
                            "如果确实需要不同信息，请尝试根本不同的查询。"
                        )
                    else:
                        msg += (
                            "请使用已提供的结果，或尝试不同方法。"
                        )
                    return ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress",
                        message=msg,
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )

        # Dimension 4: repeated call count (catches same-args retries even with varying results)
        # Increment BEFORE the check so blocked calls are also counted
        if self._is_idempotent(tool_name, args):
            prev = self._call_counts.get(signature, 0)
            self._call_counts[signature] = prev + 1
            call_count = prev + 1
            if call_count > self.config.repeated_call_block_after:
                is_web = tool_name in ("web_search", "web_fetch")
                msg = (
                    f"你已用相同关键词搜索 {call_count} 次，"
                    "上下文中已有足够结果。"
                )
                if is_web:
                    msg += (
                        "请现在基于已有证据组织回答。"
                        "用 [1]、[2] 标记引用来源。"
                        "如果确实需要不同信息，请换用不同关键词，但要先基于当前结果作答。"
                    )
                else:
                    msg += "请使用上下文中已有结果，或尝试明显不同的参数。"
                return ToolGuardrailDecision(
                    action="block",
                    code="repeated_call",
                    message=msg,
                    tool_name=tool_name,
                    count=call_count,
                    signature=signature,
                )

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        result: str | None,
        *,
        failed: bool = False,
    ) -> ToolGuardrailDecision:
        """Record a tool call outcome and return a warning/allow decision."""
        signature = ToolCallSignature.from_call(tool_name, args)

        # Track total web operations for soft guidance
        _WEB_TOOLS = ("web_search", "web_fetch")
        if tool_name in _WEB_TOOLS:
            self._total_web_calls += 1

        if failed:
            # Track exact failure
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            # Track same-tool failure
            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            # Halt check: same-tool failure
            if same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"{tool_name} 在本轮已失败 {same_count} 次。"
                        "现在应尝试根本不同的方法：更换工具、简化任务，或基于已知信息回答。"
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            # Warning: exact failure
            if exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure",
                    message=(
                        f"{tool_name} 已用相同参数失败 {exact_count} 次。"
                        "这看起来像循环；请检查错误并改变策略，"
                        "不要原样重试。"
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            # Warning: same-tool failure
            if same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure",
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        # Success path: clear failure counters for this signature/tool
        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        # Track idempotent no-progress for read-only tools
        if not self._is_idempotent(tool_name, args):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_raw = "\x00NONE" if result is None else (result or "")
        result_hash = hashlib.md5(result_raw.encode()).hexdigest()[:12]
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress",
                message=(
                    f"{tool_name} 已返回相同结果 {repeat_count} 次。"
                    "请使用已提供的结果，或改变查询，不要原样重复。"
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        # Soft guidance: too many total web operations
        if tool_name in _WEB_TOOLS and self._total_web_calls >= 8:
            return ToolGuardrailDecision(
                action="warn",
                code="many_web_operations",
                message=(
                    f"本轮你已调用网络工具 {self._total_web_calls} 次。"
                    "请判断证据是否已经足够组织有帮助的回答。"
                    "如果足够，请停止搜索并立即用 [1]、[2] 引用标记回答。"
                ),
                tool_name=tool_name,
                count=self._total_web_calls,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str, args: dict[str, Any] | None = None) -> bool:
        if self._is_idempotent_override is not None:
            try:
                return self._is_idempotent_override(tool_name, args)
            except Exception:
                pass
        if tool_name in MUTATING_TOOL_NAMES:
            return False
        return tool_name in IDEMPOTENT_TOOL_NAMES


def append_guardrail_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "工具循环硬停" if decision.action == "halt" else "工具循环警告"
    suffix = (
        f"\n\n[{label}: "
        f"{decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result or "") + suffix


def guardrail_halt_response(decision: ToolGuardrailDecision) -> str:
    """Build a controlled user-facing message when the guardrail halts the loop."""
    tool = decision.tool_name or "某个工具"

    # Map internal codes to user-friendly explanations
    if decision.code == "same_tool_failure_halt":
        if tool in ("web_fetch", "web_search"):
            return (
                f"网页资料抓取连续受阻（已尝试 {decision.count} 次不同方式），我已停止重试。"
                "可以换用已经拿到的来源回答，或明确告知哪些信息目前无法获取。"
            )
        return (
            f"{tool} 连续失败 {decision.count} 次，我已停止重试。"
            "下一步会换一个根本不同的方法，或基于已有信息继续。"
        )

    # Fallback for other halt codes
    return (
        f"我已停止重试 {tool}，因为它在 {decision.count} 次尝试后仍无进展。"
        "下一步会改变策略，而不是重复同一调用。"
    )
