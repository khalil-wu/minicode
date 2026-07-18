from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any, Callable

from backend.agent.tool_common import WEB_SEARCH_TOOL_NAMES, WEB_TOOL_NAMES, _text_arg



# ---------------------------------------------------------------------------
# Hermes-style progressive tool-call guardrail (warn → block → halt)
# ---------------------------------------------------------------------------


IDEMPOTENT_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "grep_files", "glob_files", "fuzzy_search",
    "web_search", "web_fetch", "read_artifact", "read_memory",
})

MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "run_command", "write_file", "edit_file", "git_commit",
    "save_memory",
})

MAX_WEB_OPS_PER_TURN = 5
# After this many web ops in one turn, further web calls are blocked (warn at
# MAX_WEB_OPS_PER_TURN first). Stops the over-research death spiral where the
# agent keeps fetching until the iteration cap truncates the turn into a recovery
# summary. Web-specific: other tools are still allowed so the agent can act/answer.
MAX_WEB_OPS_HALT = 10
SINGLE_OUTPUT_WRITE_TOOL_NAMES = frozenset({"write_file"})
SINGLE_OUTPUT_DOCUMENT_EXTENSIONS = frozenset({
    ".md",
    ".markdown",
    ".txt",
    ".html",
    ".htm",
    ".doc",
    ".docx",
    ".pdf",
    ".rtf",
})

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
_DUPLICATE_STEM_SUFFIX_RE = re.compile(
    r"(?:"
    r"\s*[\(\uff08\[]\s*(?:copy|副本|版本?\s*\d+|v\s*\d+|[0-9]+|[一二两三四五六七八九十]+)\s*[\)\uff09\]]"
    r"|[\s_\-]+(?:copy|副本|版本?\s*\d+|v\s*\d+|[0-9]+|[一二两三四五六七八九十]+(?:版|份|稿)?)"
    r")+$",
    re.I,
)
_MULTIPLE_OUTPUT_REQUEST_RE = re.compile(
    r"("
    r"(?:[2-9]|1[0-9])\s*(?:个|份|版|版本|文件|文档|方案|计划|页面|稿)"
    r"|[二两三四五六七八九十]\s*(?:个|份|版|版本|文件|文档|方案|计划|页面|稿)"
    r"|多(?:个|份|版|版本|文件|文档|方案|计划|页面|稿)"
    r"|几个(?:文件|文档|版本|方案|计划)"
    r"|分别(?:写|生成|创建|输出|保存)"
    r"|(?:two|three|four|five|several|multiple)\s+(?:files?|versions?|drafts?|plans?|pages?)"
    r")",
    re.I,
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


def _canonical_tool_args(tool_name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    if tool_name in WEB_SEARCH_TOOL_NAMES:
        query = _text_arg((args or {}).get("query")) or _text_arg(args or {})
        tokens = sorted(_search_query_tokens(query))
        if tokens:
            return {"query_tokens": tokens}
    return dict(args or {})


def _path_arg(args: dict[str, Any] | None) -> str:
    args = args or {}
    return str(args.get("file_path") or args.get("path") or args.get("target") or args.get("filename") or "").strip()


def _output_path_parts(path_value: str) -> tuple[str, str, str]:
    normalized = str(path_value or "").replace("\\", "/").strip()
    if not normalized:
        return "", "", ""
    path = PurePosixPath(normalized)
    directory = str(path.parent)
    if directory == ".":
        directory = ""
    suffix = path.suffix.casefold()
    stem = path.stem.casefold().strip()
    while True:
        updated = _DUPLICATE_STEM_SUFFIX_RE.sub("", stem).strip()
        if updated == stem:
            break
        stem = updated
    comparable = re.sub(r"[\s_\-\(\)\[\]\uff08\uff09（）【】]+", "", stem)
    return directory.casefold(), comparable, suffix


def _user_requested_multiple_output_artifacts(message: str) -> bool:
    text = str(message or "")
    return bool(_MULTIPLE_OUTPUT_REQUEST_RE.search(text))


def _output_stems_are_similar(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter = min(len(left), len(right))
    longer = max(len(left), len(right))
    if shorter < 4:
        return False
    if longer and shorter / longer < 0.72:
        return False
    return SequenceMatcher(a=left, b=right).ratio() >= 0.86


def duplicate_output_write_guard_reason(state: Any, tc: Any) -> str:
    """Prevent one turn from drifting into multiple sibling output copies.

    This is intentionally conservative: it only targets successful prior
    write_file calls in the same turn with the same directory, same extension,
    and a near-identical stem after stripping copy/version suffixes.
    """
    if getattr(tc, "name", "") not in SINGLE_OUTPUT_WRITE_TOOL_NAMES:
        return ""
    if _user_requested_multiple_output_artifacts(getattr(state, "user_message", "")):
        return ""

    target_path = _path_arg(getattr(tc, "arguments", None))
    target_dir, target_stem, target_suffix = _output_path_parts(target_path)
    if not target_stem or target_suffix not in SINGLE_OUTPUT_DOCUMENT_EXTENSIONS:
        return ""

    for record in reversed(getattr(state, "tool_calls", []) or []):
        if getattr(record, "tool_name", "") not in SINGLE_OUTPUT_WRITE_TOOL_NAMES:
            continue
        if getattr(record, "status", "") != "success":
            continue
        prior_path = _path_arg(getattr(record, "tool_input", None))
        prior_dir, prior_stem, prior_suffix = _output_path_parts(prior_path)
        if not prior_stem:
            continue
        if (prior_dir, prior_suffix) != (target_dir, target_suffix):
            continue
        if not _output_stems_are_similar(target_stem, prior_stem):
            continue
        return (
            f"Skipped duplicate output write: this turn already wrote a similar output file '{prior_path}'. "
            f"Do not create another sibling copy such as '{target_path}' unless the user explicitly asked for multiple files or versions. "
            "Read or verify the existing file, edit that target if needed, then give the final answer."
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
        self._web_halt: bool = False
        self._call_counts: dict[ToolCallSignature, int] = {}
        self._blocked_repeat_counts: dict[ToolCallSignature, int] = {}

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

        # Web over-use halt: block further web calls once MAX_WEB_OPS_HALT is
        # reached. Other tools remain allowed so the agent can still act/answer.
        if self._web_halt and tool_name in ("web_search", "web_fetch"):
            return ToolGuardrailDecision(
                action="halt",
                code="many_web_operations_halt",
                message=(
                    f"本轮网络工具已调用 {self._total_web_calls} 次，超过上限，已拦截此次网络调用。"
                    "请立即基于已抓取的来源用 [1]、[2] 引用标记作答，不要再发起网络请求。"
                ),
                tool_name=tool_name,
                count=self._total_web_calls,
                signature=signature,
            )

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
                    blocked_count = self._blocked_repeat_counts.get(signature, 0) + 1
                    self._blocked_repeat_counts[signature] = blocked_count
                    action = "halt" if blocked_count >= 2 else "block"
                    decision = ToolGuardrailDecision(
                        action=action,
                        code="idempotent_no_progress",
                        message=(
                            f"{msg} 已再次提交相同调用，现已终止本轮工具循环。"
                            if action == "halt"
                            else msg
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    if action == "halt":
                        self._halt_decision = decision
                    return decision

        # Dimension 4: repeated executed calls (catches same-args retries even
        # with varying results). The count is updated in after_call(), not here:
        # pre-call checks must be pure observations of already-executed work.
        # Counting attempted-but-rejected calls here makes queued tools,
        # permission denials, or guardrail blocks look like real progress loops.
        if self._is_idempotent(tool_name, args):
            call_count = self._call_counts.get(signature, 0)
            if call_count >= self.config.repeated_call_block_after:
                is_web = tool_name in ("web_search", "web_fetch")
                msg = (
                    f"这个 {tool_name} 调用已用相同参数实际执行 {call_count} 次，"
                    "上下文中已有这些结果。"
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
            if self._total_web_calls >= MAX_WEB_OPS_HALT:
                # Further web calls are blocked in before_call; force the agent
                # to answer with evidence on hand instead of fetching until the
                # iteration cap truncates the turn into a recovery summary.
                self._web_halt = True

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

        self._call_counts[signature] = self._call_counts.get(signature, 0) + 1
        self._blocked_repeat_counts.pop(signature, None)

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

        # Web over-use: warn at MAX_WEB_OPS_PER_TURN, escalate to a halt warning
        # at MAX_WEB_OPS_HALT (further web calls are blocked in before_call).
        if tool_name in _WEB_TOOLS:
            if self._total_web_calls >= MAX_WEB_OPS_HALT:
                return ToolGuardrailDecision(
                    action="warn",
                    code="many_web_operations_halt",
                    message=(
                        f"本轮网络工具已调用 {self._total_web_calls} 次，超过上限。"
                        "停止搜索，立即基于已抓取的来源用 [1]、[2] 引用标记作答；下一次网络调用将被拦截。"
                    ),
                    tool_name=tool_name,
                    count=self._total_web_calls,
                    signature=signature,
                )
            if self._total_web_calls >= MAX_WEB_OPS_PER_TURN:
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
