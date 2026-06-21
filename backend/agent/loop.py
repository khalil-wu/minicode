"""
Agent Loop - Single-loop + Recovery-ladder architecture.

Inspired by Claude Code's single-query-loop pattern:
  1. Context Pipeline  (before the call)
  2. Streaming Execution (during the call)
  3. Recovery Paths    (after the call)
  4. Termination Conditions (when to stop)
  5. State Threading   (across iterations)

The model decides: tool_calls -> execute -> loop; no tool_calls -> done.
"""

from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import logging
import random
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.query_chain import QueryChainTracking
from backend.agent.error_withholding import ErrorWithholdingController, RecoveryStrategy
from backend.agent.message import AgentEvent
from backend.agent.checkpoint import save_run_checkpoint, load_latest_checkpoint, clear_checkpoints
from backend.agent.prompting import build_tool_runtime_guidance
from backend.agent.policies import (
    DefaultReflectionPolicy,
    DefaultStreamRetryPolicy,
    MultiPerspectiveReflectionPolicy,
)
from backend.hooks.manager import HookEvent, get_hook_manager
from backend.agent.progress import (
    agent_progress as _agent_progress,
)
from backend.agent.state import AgentState
from backend.agent.runtime import AgentRunStatus, AgentRuntime, default_runtime
from backend.agent.tool_execution import (
    StreamingToolExecutor,
    execute_tool_batch as _execute_tool_batch,
    repair_tool_call_sequence,
    tool_call_is_safe_for_model_history,
)
from backend.agent.tool_guardrails import (
    ToolCallGuardrailController,
    guardrail_halt_response,
)
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import (
    LLMAdapter,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Constants

_POST_COMPACTION_HARD_LIMIT = 0.98
LLM_STREAM_TIMEOUT_SECONDS = 120.0
_CLARIFICATION_TOOL_NAMES = {"ask_user"}
_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3
_MAX_OUTPUT_FINISH_REASONS = {
    "length",
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
    "incomplete",
}
_MAX_OUTPUT_RECOVERY_PROMPT = (
    "Output token limit hit. Resume directly; no apology, no recap of what you were doing. "
    "Pick up exactly where the previous output was cut off. Break remaining work into smaller pieces."
)
_EXPLICIT_WORKSPACE_REQUIRED_TOOL_PATTERNS = (
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "grep_files",
    "glob_files",
    "fuzzy_search",
    "go_to_definition",
    "find_references",
    "git_*",
    "run_command",
    "terminal_*",
    "worktree_*",
    "workspace_*",
    "preview.*",
    "todo_write",
    "task",
)
_NO_WORKSPACE_GUIDANCE = (
    "No workspace folder is open in this desktop session. Answer from conversation context only. "
    "If the request needs local files, shell, git, preview, or workspace inspection, ask the user to open a folder first."
)
DELIVERY_REQUEST_RE = re.compile(
    r"(write|create|edit|modify|update|delete|rename|run|test|build|install|"
    r"implement|fix|generate|save|"
    r"\u5199|\u521b\u5efa|\u4fee\u6539|\u66f4\u65b0|\u5220\u9664|\u91cd\u547d\u540d|"
    r"\u8fd0\u884c|\u6d4b\u8bd5|\u6784\u5efa|\u5b89\u88c5|\u5b9e\u73b0|\u4fee\u590d|"
    r"\u751f\u6210|\u4fdd\u5b58)",
    re.I,
)
INTENTION_ONLY_RE = re.compile(
    r"^\s*(i(?:'ll| will| am going to)\s+(?:do|check|look|try|work|handle|take|get|start|begin|make|create|write|edit|run|search|find|fix|implement|generate)\s|"
    r"\u6211\u4f1a|\u6211\u5c06|\u6211\u6765|\u6211\u5148|\u63a5\u4e0b\u6765\u6211)",
    re.I,
)
WEATHER_REQUEST_RE = re.compile(
    r"\b(weather|forecast|temperature)\b|"
    r"(\u5929\u6c14|\u6c14\u6e29|\u6e29\u5ea6|\u9884\u62a5|\u964d\u96e8|\u4e0b\u96e8)",
    re.I,
)
LOCATION_HINT_RE = re.compile(
    r"\b(?:in|at|for|near)\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\b|"
    r"\b(?:Beijing|Shanghai|Guangzhou|Shenzhen|Hangzhou|Chengdu|Wuhan|Nanjing|Tianjin|Chongqing|"
    r"Tokyo|Seoul|London|Paris|Berlin|New York|San Francisco|Los Angeles|Singapore)\b|"
    r"(\u5317\u4eac|\u4e0a\u6d77|\u5e7f\u5dde|\u6df1\u5733|\u676d\u5dde|\u6210\u90fd|\u6b66\u6c49|"
    r"\u5357\u4eac|\u5929\u6d25|\u91cd\u5e86|\u82cf\u5dde|\u897f\u5b89|\u957f\u6c99|\u4e1c\u4eac|"
    r"\u9996\u5c14|\u4f26\u6566|\u5df4\u9ece|\u7ebd\u7ea6|\u65b0\u52a0\u5761)",
    re.I,
)
LOCATION_CLARIFICATION_RE = re.compile(
    r"\b(?:which|what)\b.{0,40}\b(?:city|location|area)\b|"
    r"\b(?:city|location|area)\b.{0,40}\?|"
    r"(\u54ea\u4e2a|\u54ea\u91cc|\u4ec0\u4e48).{0,24}(\u57ce\u5e02|\u5730\u70b9|\u5730\u533a)|"
    r"(\u57ce\u5e02|\u5730\u70b9|\u5730\u533a).{0,24}(\u54ea\u4e2a|\u54ea\u91cc|\u4ec0\u4e48)",
    re.I,
)
DEICTIC_LOCATION_RE = re.compile(
    r"\b(?:near me|nearby|around me|around here|close by|in my area)\b|"
    r"(\u9644\u8fd1|\u5468\u8fb9|\u8eab\u8fb9|\u8fd9\u9644\u8fd1)",
    re.I,
)
LOCAL_RECOMMENDATION_RE = re.compile(
    r"\b(?:restaurant|food|eat|meal|cafe|coffee|hotel|store|shop|things to do)\b|"
    r"(\u597d\u5403|\u5403\u7684|\u9910\u5385|\u996d\u5e97|\u5496\u5561|\u9152\u5e97|"
    r"\u8d85\u5e02|\u5546\u5e97|\u666f\u70b9|\u53bb\u54ea|\u73a9)",
    re.I,
)
FAILURE_ACK_MARKERS = (
    "\u5931\u8d25",
    "\u65e0\u6cd5",
    "\u672a\u80fd",
    "\u4e0d\u80fd",
    "\u51fa\u9519",
    "\u9519\u8bef",
    "\u88ab\u62d2\u7edd",
    "\u6743\u9650",
    "blocked",
    "failed",
    "failure",
    "error",
    "could not",
    "cannot",
    "can't",
    "unable",
    "permission",
)
WRITE_SUCCESS_MARKERS = (
    "\u5df2\u521b\u5efa",
    "\u5df2\u5199\u5165",
    "\u5df2\u4fee\u6539",
    "\u5df2\u66f4\u65b0",
    "created",
    "wrote",
    "updated",
    "modified",
)
NONFATAL_ERROR_KINDS = {
    "missing_generated_content",
    "routing_error",
    "stale_evidence",
    "repeat_guard",
    "tool_disabled",
}
NONFATAL_PROJECTIONS = {"silent", "status", "warning"}
MUTATION_TOOLS = {"write_file", "edit_file"}
WEB_TOOLS = {"web_search", "search_web", "web_fetch", "fetch_web", "fetch_page", "fetch_url"}
INLINE_CITATION_RE = re.compile(r"\[(?:\d+)\]")
SNIPPET_UNCERTAINTY_RE = re.compile(
    r"snippet-only|search snippets?|candidate evidence|unverified|could not verify|"
    r"may|might|appears?|seems?|uncertain|no results?|could not find|couldn't find|not find|"
    r"\u7247\u6bb5|\u5019\u9009|\u672a\u6838\u5b9e|\u65e0\u6cd5\u6838\u5b9e|\u4e0d\u786e\u5b9a|\u53ef\u80fd|\u4f3c\u4e4e|"
    r"\u6ca1\u6709\u627e\u5230|\u672a\u627e\u5230|\u641c\u7d22\u7ed3\u679c\u4e0d\u8db3|\u8bc1\u636e\u4e0d\u8db3",
    re.I,
)

# Session Context


@dataclass(slots=True)
class AgentLoopSessionContext:
    """Per-session runtime dependencies bag."""

    skill_manager: Any | None = None
    vector_memory: Any | None = None
    permission_context: PermissionContext | None = None
    workspace_root: Path | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: Any | None = None
    background_manager: Any | None = None
    terminal_manager: Any | None = None
    stream_callback: Any | None = None
    emit_event: Any | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AnswerGateResult:
    ok: bool
    feedback: str = ""
    reason: str = ""


def user_message_missing_required_location(user_message: str) -> bool:
    """Return true when using tools would require a location the user omitted."""
    text = user_message or ""
    if LOCATION_HINT_RE.search(text):
        return False
    if WEATHER_REQUEST_RE.search(text):
        return True
    return bool(DEICTIC_LOCATION_RE.search(text) and LOCAL_RECOMMENDATION_RE.search(text))


class AnswerGate:
    """Final-answer integrity checks owned by the main loop."""

    def __init__(self, max_retries: int = 2, *, enabled: bool = True) -> None:
        self.max_retries = max_retries
        self.enabled = enabled

    def evaluate(self, user_message: str, draft_reply: str, state: Any) -> AnswerGateResult:
        if not self.enabled:
            return AnswerGateResult(ok=True)
        retry_count = int(getattr(state, "answer_gate_retries", 0) or 0)
        if retry_count >= self.max_retries:
            return AnswerGateResult(ok=True)

        text = (draft_reply or "").strip()
        if not text:
            return AnswerGateResult(ok=True)

        if self._weather_request_missing_location(user_message or "", text, state):
            return self._retry(
                state,
                retry_count,
                reason="missing_weather_location",
                feedback=(
                    "The weather request is missing a city or area. "
                    "Ask one concise city/location question first; do not assume a location."
                ),
            )

        if self._looks_like_unexecuted_commitment(user_message or "", text, state):
            return self._retry(
                state,
                retry_count,
                reason="unexecuted_commitment",
                feedback=(
                    "The draft only promises or plans the work without executing it. "
                    "If tools can complete it, call them now. Otherwise, state the concrete limitation."
                ),
            )

        if self._claims_success_without_mutation(text, state):
            return self._retry(
                state,
                retry_count,
                reason="unverified_file_change",
                feedback=(
                    "Do not claim a file was created or edited without a successful "
                    "write_file/edit_file result. Perform the write or say no file was written."
                ),
            )

        if self._unacknowledged_recent_tool_failure(text, state):
            failure = self._last_fatal_tool_failure(state)
            return self._retry(
                state,
                retry_count,
                reason="unacknowledged_tool_failure",
                feedback=(
                    "A recent tool failure must be acknowledged before giving a final answer. "
                    f"Tool: {self._record_field(failure, 'tool_name') or 'unknown'}; "
                    f"kind: {self._record_field(failure, 'error_kind') or 'unknown'}; "
                    f"reason: {self._record_field(failure, 'tool_output') or self._record_field(failure, 'user_summary') or 'no details'}."
                ),
            )

        if self._web_answer_lacks_citation(text, state):
            return self._retry(
                state,
                retry_count,
                reason="missing_web_citation",
                feedback=(
                    "Your answer relies on web evidence but has no inline citation marker. "
                    "Add [1]/[2] markers tied to the relevant web_search/web_fetch result. "
                    "Do not add a separate Sources/References section or raw URLs; the UI renders source links from tool metadata. "
                    "If you only have search snippets, say the evidence is snippet-only and answer with uncertainty."
                ),
            )

        return AnswerGateResult(ok=True)

    @staticmethod
    def _retry(state: Any, retry_count: int, *, reason: str, feedback: str) -> AnswerGateResult:
        setattr(state, "answer_gate_retries", retry_count + 1)
        return AnswerGateResult(ok=False, reason=reason, feedback=feedback)

    @classmethod
    def _weather_request_missing_location(cls, user_message: str, text: str, state: Any) -> bool:
        if not user_message_missing_required_location(user_message):
            return False
        if cls._has_fetched_web_evidence(state):
            return False
        if LOCATION_CLARIFICATION_RE.search(text or ""):
            return False
        return True

    @staticmethod
    def _records(state: Any) -> list[Any]:
        records = getattr(state, "tool_calls", []) or []
        return list(records)

    @staticmethod
    def _record_field(record: Any, field: str) -> str:
        if record is None:
            return ""
        value = getattr(record, field, "")
        return str(value or "").strip()

    @staticmethod
    def _looks_like_unexecuted_commitment(user_message: str, text: str, state: Any) -> bool:
        if not DELIVERY_REQUEST_RE.search(user_message):
            return False
        if any(getattr(tc, "status", "") == "success" for tc in (getattr(state, "tool_calls", None) or [])):
            return False
        if not INTENTION_ONLY_RE.search(text):
            return False
        return not any(marker in text.lower() for marker in FAILURE_ACK_MARKERS)

    @classmethod
    def _claims_success_without_mutation(cls, text: str, state: Any) -> bool:
        lowered = text.lower()
        if not any(marker in lowered for marker in WRITE_SUCCESS_MARKERS):
            return False
        return not any(
            cls._record_field(record, "status") == "success"
            and cls._record_field(record, "tool_name") in MUTATION_TOOLS
            for record in cls._records(state)
        )

    @classmethod
    def _last_fatal_tool_failure(cls, state: Any) -> Any | None:
        for record in reversed(cls._records(state)):
            status = cls._record_field(record, "status")
            if status not in {"error", "failed", "blocked"}:
                continue
            if cls._record_field(record, "projection") in NONFATAL_PROJECTIONS:
                continue
            if cls._record_field(record, "error_kind") in NONFATAL_ERROR_KINDS:
                continue
            return record
        return None

    @classmethod
    def _unacknowledged_recent_tool_failure(cls, text: str, state: Any) -> bool:
        if cls._last_fatal_tool_failure(state) is None:
            return False
        lowered = text.lower()
        return not any(marker in lowered for marker in FAILURE_ACK_MARKERS)

    @classmethod
    def _has_fetched_web_evidence(cls, state: Any) -> bool:
        return any(
            cls._record_field(record, "status") == "success"
            and (
                cls._record_field(record, "evidence_type") == "fetched"
                or cls._record_field(record, "tool_name") == "web_fetch"
            )
            for record in cls._records(state)
        )

    @classmethod
    def _has_successful_web_evidence(cls, state: Any) -> bool:
        return any(
            cls._record_field(record, "status") == "success"
            and (
                cls._record_field(record, "tool_name") in WEB_TOOLS
                or cls._record_field(record, "evidence_type") in {"candidate", "fetched"}
            )
            for record in cls._records(state)
        )

    @classmethod
    def _web_answer_lacks_citation(cls, text: str, state: Any) -> bool:
        if not cls._has_successful_web_evidence(state):
            return False
        if INLINE_CITATION_RE.search(text):
            return False
        if SNIPPET_UNCERTAINTY_RE.search(text):
            return False
        return True

@dataclass(frozen=True, slots=True)
class _RecoveryProfile:
    event_id: str
    completed_message: str
    completed_summary: str
    recovered_summary: str
    failed_message: str
    failed_summary: str
    partial_stopped_reason: str
    recovered_stopped_reason: str
    failed_stopped_reason: str
    error_message: str
    error_type: str
    recoverable: bool
    provider_error_type: str = ""
    emit_failed_progress: bool = True
    allow_last_resort: bool = True
    allow_partial_text_commit: bool = True
    live_text_streaming: bool = False


# Recovery helpers


def _format_llm_error(message: str) -> str:
    return sanitize_llm_error_message(message, classify_llm_error(message))


async def _run_verify_command(command: str, cwd: Path, timeout: float) -> tuple[bool, str]:
    """Run the configured verify command; return (passed, output tail).

    Used by the action-level verification gate: a turn that mutated the
    workspace must pass this command before its final answer is accepted.
    The command is user-configured (settings.json `agent.verify_command`),
    so it runs without per-call approval, like a hook.
    """
    from backend.runtime_env import sanitized_subprocess_env

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=sanitized_subprocess_env(),
        )
        data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        return False, f"Verify command timed out after {int(timeout)}s."
    except Exception as exc:
        return False, f"Verify command failed to start: {exc}"
    output = (data or b"").decode("utf-8", errors="replace").strip()
    if len(output) > 2000:
        output = "...\n" + output[-2000:]
    return proc.returncode == 0, output


def _collect_mcp_instructions() -> dict[str, str]:
    """Fetch server-declared MCP instructions, tolerant of an absent manager."""
    try:
        from backend.main import get_mcp_manager

        manager = get_mcp_manager()
        if manager is not None:
            return manager.get_server_instructions()
    except Exception:  # pragma: no cover - manager unavailable / not started
        pass
    return {}


def _mcp_registry_version() -> int:
    """Current MCP registry generation, for tool-schema cache invalidation."""
    try:
        from backend.main import get_mcp_manager

        manager = get_mcp_manager()
        if manager is not None:
            return int(getattr(manager, "registry_version", 0) or 0)
    except Exception:  # pragma: no cover - manager unavailable / not started
        pass
    return 0


def _tool_is_idempotent(tool_registry: ToolRegistry, tool_name: str, args: dict[str, Any] | None) -> bool:
    """Classify idempotent calls from tool-owned runtime metadata."""
    tool = tool_registry.get_tool(tool_name)
    if tool is None:
        return False
    if getattr(tool, "mutates_workspace", False) or getattr(tool, "mutates_external_state", False):
        return False
    try:
        return bool(tool.is_read_only(args) or tool.is_concurrency_safe(args))
    except Exception:
        return bool(getattr(tool, "read_only", False))


# Hermes-style: strip internal reasoning tags from user-visible text.
# Some models (especially via proxies) leak <thinking>/<reasoning> blocks
# into the text stream. Strip them before storing or emitting.
_THINKING_TAG_RE = re.compile(
    r"<(?:thinking|reasoning|internal)[^>]*>.*?</(?:thinking|reasoning|internal)>",
    re.DOTALL | re.IGNORECASE,
)


def _scrub_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> and similar tags from model output."""
    if not text or "<" not in text:
        return text
    return _THINKING_TAG_RE.sub("", text).strip()


def _is_max_output_finish_reason(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized in _MAX_OUTPUT_FINISH_REASONS


def _iteration_id(state: AgentState) -> str:
    return f"iter:{max(1, state.iterations)}"


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _action_summary_for_tool_calls(tool_calls: list[ToolCallEvent]) -> str:
    names = {tc.name for tc in tool_calls}
    if any(name in names for name in {"web_search", "search_web"}):
        return "正在搜索实时信息并核对来源。"
    if any(name in names for name in {"web_fetch", "fetch_url", "fetch_web"}):
        return "正在打开相关来源，提取可用信息。"
    if any(name in names for name in {"read_file", "list_files", "get_file", "open_file"}):
        return "正在读取相关文件，确认上下文。"
    if any(name in names for name in {"run_command", "shell", "exec_command"}):
        return "正在运行命令验证当前状态。"
    if any(name in names for name in {"edit_file", "apply_patch", "write_file"}):
        return "正在应用代码修改。"
    if any("browser" in name or "playwright" in name for name in names):
        return "正在打开页面进行实际检查。"
    if any("test" in name.lower() for name in names):
        return "正在运行测试验证改动。"
    if any(name.startswith("mcp__") for name in names):
        return "正在调用外部工具获取上下文。"
    return "正在调用工具处理当前步骤。"


def _runtime_action_summary_event(
    tool_calls: list[ToolCallEvent],
    *,
    iteration_id: str,
) -> AgentEvent | None:
    if not tool_calls:
        return None
    content = _action_summary_for_tool_calls(tool_calls)
    return AgentEvent.agent_item(
        id=f"{iteration_id}:runtime-action-summary",
        kind="action_summary",
        content=content,
        loop_id=iteration_id,
        iteration_id=iteration_id,
        role="runtime",
        source="runtime",
        status="completed",
        title="Action",
        summary=content,
        visibility="timeline",
        group_id=iteration_id,
        step_id=f"{iteration_id}:action",
        tool_call_ids=[tc.id for tc in tool_calls],
        display_scope="activity",
        seq=1,
    )


def _model_process_text_event(
    content: str,
    tool_calls: list[ToolCallEvent],
    *,
    iteration_id: str,
    source: str,
) -> AgentEvent | None:
    text = content.strip()
    if not text or not tool_calls:
        return None
    return AgentEvent.agent_item(
        id=f"{iteration_id}:model-process:{source}",
        kind="process_text",
        content=text,
        loop_id=iteration_id,
        iteration_id=iteration_id,
        role="assistant",
        source=source,
        status="completed",
        visibility="timeline",
        group_id=iteration_id,
        step_id=f"{iteration_id}:model-process",
        tool_call_ids=[tc.id for tc in tool_calls],
        display_scope="activity",
        seq=0,
    )


def _filter_disabled_tool_schemas(
    tool_schemas: list[dict[str, Any]],
    disabled_tools: set[str],
) -> list[dict[str, Any]]:
    if not disabled_tools:
        return tool_schemas
    filtered: list[dict[str, Any]] = []
    for schema in tool_schemas:
        name = str((schema.get("function") or {}).get("name") or "")
        if name not in disabled_tools:
            filtered.append(schema)
    return filtered


def _workspace_bound_tool_names(tool_schemas: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for schema in tool_schemas:
        name = str((schema.get("function") or {}).get("name") or "")
        if name and any(fnmatch.fnmatch(name, pattern) for pattern in _EXPLICIT_WORKSPACE_REQUIRED_TOOL_PATTERNS):
            names.add(name)
    return names


def _tool_schema_names(tool_schemas: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for schema in tool_schemas:
        name = str((schema.get("function") or {}).get("name") or "")
        if name:
            names.add(name)
    return names


def _filter_clarification_tool_schemas(
    tool_schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        schema
        for schema in tool_schemas
        if str((schema.get("function") or {}).get("name") or "") in _CLARIFICATION_TOOL_NAMES
    ]


def _timeout_tool_result_reply(state: AgentState) -> str:
    return _tool_result_fallback_reply(
        state,
        reason="模型在工具执行完成后响应超时。",
    )


def _successful_tool_result_records(state: AgentState) -> list[Any]:
    successful = [
        tc for tc in state.tool_calls
        if getattr(tc, "status", "") in {"success", "partial"}
        and _is_user_visible_tool_output(str(getattr(tc, "tool_output", "") or ""))
    ]
    return successful


_NON_FATAL_TOOL_ERROR_KINDS = {
    "missing_generated_content",
    "routing_error",
    "stale_evidence",
    "repeat_guard",
    "tool_disabled",
}


def _is_nonfatal_tool_record(record: Any) -> bool:
    if str(getattr(record, "projection", "") or "") in {"silent", "status", "warning"}:
        return True
    return str(getattr(record, "error_kind", "") or "") in _NON_FATAL_TOOL_ERROR_KINDS


def _is_failed_tool_record(record: Any) -> bool:
    status = str(getattr(record, "status", "") or "")
    return status in {"error", "failed", "blocked"} and not _is_nonfatal_tool_record(record)


def _is_user_visible_tool_output(output: str) -> bool:
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
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"  # Chinese
    r"\u3040-\u309F\u30A0-\u30FF"                 # Japanese (Hiragana, Katakana)
    r"\uAC00-\uD7AF]"                              # Korean (Hangul)
)
_TIMEOUT_AFTER_TOOLS_REASON = "The model response timed out after the tools completed."


def _prefers_chinese_fallback(state: AgentState) -> bool:
    return bool(_CJK_TEXT_RE.search(str(getattr(state, "user_message", "") or "")))


def _fallback_copy(state: AgentState, *, reason: str = "") -> dict[str, str]:
    stock_reason = reason.strip()
    if _prefers_chinese_fallback(state):
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


def _fallback_final_text_event(content: str) -> AgentEvent:
    return AgentEvent.text_chunk(content, source="fallback", visibility="final", phase="final")


def _fallback_recovery_text_event(content: str) -> AgentEvent:
    # A stitched recovery summary (intro + last tool outputs) is not a model
    # answer. Route it as a visible timeline note instead of occupying the
    # final-answer slot — the tool results it restates are already shown as
    # activity cells. Keep Tier 2's real non-streaming answer as final.
    return AgentEvent.text_chunk(content, source="fallback", visibility="timeline", phase="recover")


def _fallback_recovery_progress_event(
    state: AgentState,
    *,
    event_id: str,
    summary: str,
) -> AgentEvent:
    chinese = _prefers_chinese_fallback(state)
    return _agent_progress(
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


def _tool_result_fallback_reply(state: AgentState, *, reason: str = "") -> str:
    successful = _successful_tool_result_records(state)
    if not successful:
        return ""

    selected = successful[-3:]
    copy = _fallback_copy(state, reason=reason)
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
        display_name = "read_file" if name == "read_file" else name
        parts.append(f"{index}. {display_name}{suffix}\n{output}")
    if any(str(getattr(record, "evidence_type", "") or "") == "candidate" for record in selected):
        parts.append(copy["candidate"])
    return "\n\n".join(parts)


def _failed_tool_result_fallback_reply(state: AgentState, *, reason: str = "") -> str:
    failed = [record for record in state.tool_calls if _is_failed_tool_record(record)]
    if not failed:
        failed = [
            record for record in state.tool_calls
            if str(getattr(record, "status", "") or "") in {"error", "failed", "blocked"}
        ]
    if not failed:
        return ""

    if _prefers_chinese_fallback(state):
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
    for index, record in enumerate(failed[-3:], start=1):
        name = str(getattr(record, "tool_name", "") or "tool")
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
        parts.append(f"{index}. {name} [{status}]\n{output}")
    return "\n\n".join(parts)


def _reset_history_after_draft_retry(ctx: ContextBuilder, full_text: str) -> None:
    if not full_text:
        return
    history = getattr(ctx, "_history", None)
    if not isinstance(history, list) or not history:
        return
    last = history[-1]
    if getattr(last, "role", None) != "assistant":
        return
    if str(getattr(last, "content", "") or "") != full_text:
        return
    history.pop()
    rebuild = getattr(ctx, "_rebuild_history_token_cache", None)
    if callable(rebuild):
        rebuild()


def _usage_done_event(usage: UsageInfo) -> AgentEvent:
    return AgentEvent.done(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )


async def _try_emergency_compact(state: AgentState, ctx: ContextBuilder) -> bool:
    """Emergency compaction: summarize history to free up context space."""
    try:
        if hasattr(ctx, 'emergency_compact'):
            result = await ctx.emergency_compact()
            if result:
                logger.info("[ErrorWithholding] Emergency compaction succeeded")
                return True
        elif hasattr(ctx, 'compact'):
            try:
                await ctx.compact(force=True)
            except TypeError:
                return False
            logger.info("[ErrorWithholding] Forced compaction succeeded")
            return True
    except Exception as exc:
        logger.warning("[ErrorWithholding] Compaction failed: %s", exc)
    return False


async def _degrade_and_finish(
    *,
    state: AgentState,
    ctx: ContextBuilder,
    llm: LLMAdapter,
    user_message: str,
    usage: UsageInfo,
    full_text: str,
    pending_tool_calls: list[ToolCallEvent],
    profile: _RecoveryProfile,
) -> AsyncIterator[AgentEvent]:
    """Finish a failed model turn through one shared degradation ladder.

    The three callers differ only in labels and final error metadata. Keeping the
    ladder in one place prevents drift between stream errors, timeouts, and API
    exceptions.
    """

    # Tier 1: if the model already produced plain text, preserve it as the
    # answer rather than throwing it away.
    if profile.allow_partial_text_commit and full_text.strip() and not pending_tool_calls:
        ctx.append_assistant(full_text)
        state.reply = full_text
        state.stopped_reason = profile.partial_stopped_reason
        if profile.live_text_streaming:
            # Partial text already streamed live; seal it as the (partial) final
            # answer without re-emitting content.
            yield AgentEvent.text_chunk(
                "",
                source="partial",
                visibility="final",
                phase="final",
                finalize=True,
            )
        else:
            yield AgentEvent.text_chunk(full_text, source="partial", visibility="final")
        yield _agent_progress(
            profile.completed_message,
            stage="status",
            status="completed",
            id=profile.event_id,
            phase="recover",
            label="Partial",
            summary=profile.completed_summary,
            visibility="timeline",
            step_id=f"recover:{state.iterations}",
            iteration_id=_iteration_id(state),
        )
        yield _usage_done_event(usage)
        return

    # Tier 2: if prior tool calls succeeded, ask the non-streaming path to
    # synthesize from the saved tool results. This mirrors the existing behavior
    # while avoiding three copies of the same fallback.
    if profile.allow_last_resort and _successful_tool_result_records(state):
        snapshot_before_last_resort: dict[str, Any] | None = None
        export_snapshot = getattr(ctx, "export_snapshot", None)
        load_snapshot = getattr(ctx, "load_snapshot", None)
        if callable(export_snapshot) and callable(load_snapshot):
            snapshot_before_last_resort = export_snapshot()
        try:
            ctx.append_user(
                "Use the tool results above to answer the user's original question. "
                "Give the final answer directly and do not call more tools."
            )
            last_resort_messages = await ctx.build(user_message=user_message, state=state)
            last_resort_reply = await asyncio.wait_for(
                llm.simple_chat(last_resort_messages),
                timeout=30.0,
            )
            if last_resort_reply and last_resort_reply.strip():
                if snapshot_before_last_resort is not None and callable(load_snapshot):
                    load_snapshot(snapshot_before_last_resort)
                ctx.append_assistant(last_resort_reply)
                state.reply = last_resort_reply
                state.stopped_reason = profile.recovered_stopped_reason
                yield _fallback_recovery_progress_event(
                    state,
                    event_id=profile.event_id,
                    summary=profile.recovered_summary,
                )
                yield _fallback_final_text_event(last_resort_reply)
                yield _usage_done_event(usage)
                return
        except (asyncio.TimeoutError, Exception) as last_exc:
            logger.debug("Last resort call failed: %s", last_exc)
        finally:
            if snapshot_before_last_resort is not None and callable(load_snapshot):
                current_snapshot = export_snapshot() if callable(export_snapshot) else {}
                if any(
                    str(message.get("content") or "").startswith("Use the tool results above to answer")
                    for message in current_snapshot.get("history", [])
                    if isinstance(message, dict) and message.get("role") == "user"
                ):
                    load_snapshot(snapshot_before_last_resort)

    # Tier 3: no safe degradation path remains. Surface a typed error.
    if profile.emit_failed_progress:
        yield _agent_progress(
            profile.failed_message,
            stage="status",
            status="failed",
            id=profile.event_id,
            phase="recover",
            label="Stopped",
            summary=profile.failed_summary,
            visibility="timeline",
            step_id=f"recover:{state.iterations}",
            iteration_id=_iteration_id(state),
        )
    yield AgentEvent.error(
        message=profile.error_message,
        recoverable=profile.recoverable,
        error_type=profile.error_type,
        provider_error_type=profile.provider_error_type,
    )
    state.stopped_reason = profile.failed_stopped_reason


# Context pipeline


async def _manage_context_budget(
    ctx: ContextBuilder,
    state: AgentState,
    budget: TokenBudget,
    tool_schemas: list[dict[str, Any]],
) -> AsyncIterator[AgentEvent]:
    """Pre-call context pipeline: check budget, compact if needed."""
    try:
        should_compact = ctx.needs_compaction(state, tool_schemas=tool_schemas)
    except TypeError:
        should_compact = ctx.needs_compaction()

    usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)

    if usage_pct > 0.75 and not should_compact:
        yield AgentEvent.budget_warning(
            bucket="total", percent=round(usage_pct, 3),
            will_compact=usage_pct > 0.85,
        )

    # Emergency compaction
    if usage_pct >= 0.95 and hasattr(ctx, "full_compact"):
        # Hook: pre_compact
        hook_mgr = get_hook_manager()
        if hook_mgr and hook_mgr.has_hooks(HookEvent.PRE_COMPACT):
            await hook_mgr.run_pre_compact()
        try:
            summary = await ctx.full_compact(restore_state=state)
        except TypeError:
            summary = await ctx.full_compact()
        logger.info("Emergency compaction: %s", summary[:120] if summary else "(empty)")
        yield AgentEvent.context_compacted(summary=summary)
        if hook_mgr and hook_mgr.has_hooks(HookEvent.POST_COMPACT):
            try:
                await hook_mgr.run_post_compact()
            except Exception as exc:
                logger.warning("post_compact hook failed: %s", exc)
        usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
        if usage_pct >= _POST_COMPACTION_HARD_LIMIT:
            yield AgentEvent.error(
                message="紧急压缩后上下文仍然接近满载。请使用 /clear 或 /compact。",
                recoverable=True, error_type="budget",
            )
            state.stopped_reason = "budget_exceeded"
            return

    # Normal compaction
    elif should_compact:
        # Hook: pre_compact
        hook_mgr = get_hook_manager()
        if hook_mgr and hook_mgr.has_hooks(HookEvent.PRE_COMPACT):
            await hook_mgr.run_pre_compact()
        try:
            summary = await ctx.compact(focus=state.user_message, restore_state=state)
        except TypeError:
            summary = await ctx.compact()
        logger.info("Compaction done: %s", summary[:80] if summary else "(empty)")
        yield AgentEvent.context_compacted(summary=summary)
        if hook_mgr and hook_mgr.has_hooks(HookEvent.POST_COMPACT):
            try:
                await hook_mgr.run_post_compact()
            except Exception as exc:
                logger.warning("post_compact hook failed: %s", exc)
        usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
        if usage_pct >= _POST_COMPACTION_HARD_LIMIT:
            yield AgentEvent.error(
                message="压缩后上下文仍超过安全限制。请使用 /clear 或 /compact。",
                recoverable=True, error_type="budget",
            )
            state.stopped_reason = "budget_exceeded"
            return


# Main loop


async def run_agent_loop(
    user_message: str,
    llm: LLMAdapter,
    tool_registry: ToolRegistry,
    artifact_store: ArtifactStore,
    permission_checker: PermissionChecker,
    agent_settings: AgentSettings | None = None,
    token_budget: TokenBudget | None = None,
    context_builder: ContextBuilder | None = None,
    state: AgentState | None = None,
    approval_handler: Callable | None = None,
    skill_manager: Any | None = None,
    vector_memory: Any | None = None,
    permission_context: PermissionContext | None = None,
    session_id: str = "",
    task_id: str = "",
    task_manager: Any | None = None,
    background_manager: Any | None = None,
    stream_callback: Any | None = None,
    emit_event: Any | None = None,
    metadata: dict[str, Any] | None = None,
    session_context: AgentLoopSessionContext | None = None,
) -> AsyncIterator[AgentEvent]:
    """
    Agent Loop - single while-true with recovery ladder.

    The model decides: has tool_calls -> execute -> loop; no tool_calls -> done.
    """
    # Phase 1: Setup
    if session_context is not None:
        skill_manager = skill_manager or session_context.skill_manager
        vector_memory = vector_memory or session_context.vector_memory
        permission_context = permission_context or session_context.permission_context
        session_id = session_id or session_context.session_id
        task_id = task_id or session_context.task_id
        task_manager = task_manager or session_context.task_manager
        background_manager = background_manager or session_context.background_manager
        stream_callback = stream_callback or session_context.stream_callback
        emit_event = emit_event or session_context.emit_event
        metadata = metadata or session_context.metadata

    metadata = dict(metadata or {})
    settings = agent_settings or AgentSettings()
    if (
        settings.stream_timeout_seconds == AgentSettings.stream_timeout_seconds
        and LLM_STREAM_TIMEOUT_SECONDS != AgentSettings.stream_timeout_seconds
    ):
        settings = dataclasses.replace(settings, stream_timeout_seconds=LLM_STREAM_TIMEOUT_SECONDS)
    budget = token_budget or TokenBudget()
    ctx = context_builder or ContextBuilder(
        token_budget=budget, agent_settings=settings, vector_memory=vector_memory,
    )
    state = state or AgentState(user_message=user_message, max_iterations=settings.max_iterations)
    state.max_total_retries = max(0, int(settings.turn_error_budget))
    runtime: AgentRuntime = metadata.get("agent_runtime") if isinstance(metadata.get("agent_runtime"), AgentRuntime) else default_runtime()
    run_record = runtime.start_run(
        conversation_id=str(getattr(state, "conversation_id", "") or metadata.get("conversation_id", "") or ""),
        parent_run_id=str(metadata.get("parent_run_id", "") or ""),
        role=str(metadata.get("agent_role", "main") or "main"),
        task_id=task_id,
        session_id=session_id,
        budget=budget,
        run_id=str(metadata.get("run_id", "") or "") or None,
    )
    metadata.setdefault("run_id", run_record.run_id)
    metadata.setdefault("agent_runtime", runtime)
    run_completed_emitted = False

    def _complete_run_record(
        status: AgentRunStatus,
        *,
        summary: str = "",
        error: str = "",
    ) -> AgentEvent | None:
        nonlocal run_completed_emitted
        if run_completed_emitted:
            return None
        record = runtime.complete_run(run_record.run_id, status, summary=summary, error=error)
        run_completed_emitted = True
        return AgentEvent.agent_run_completed(record or run_record)

    yield AgentEvent.agent_run_started(run_record)
    yield AgentEvent.agent_phase_updated(
        run_record.run_id,
        "plan",
        summary="Preparing agent context",
        role=run_record.role,
        conversation_id=run_record.conversation_id,
    )

    # 🆕 Preprocess @mention references
    workspace_root_for_refs = None
    if session_context is not None:
        workspace_root_for_refs = session_context.workspace_root
    if workspace_root_for_refs is None and state.workspace_context and hasattr(state.workspace_context, 'root_path'):
        workspace_root_for_refs = state.workspace_context.root_path

    if workspace_root_for_refs:
        try:
            from backend.agent.context_references import expand_all_references, format_reference_summary

            expanded_message, expanded_refs = await expand_all_references(
                user_message,
                workspace_root_for_refs,
                context_limit=budget.total
            )

            if expanded_refs:
                summary = format_reference_summary(expanded_refs)
                logger.info(f"Expanded @references: {summary}")

                # Update user message with expanded content
                user_message = expanded_message
                state.user_message = expanded_message

                # Emit event for frontend display
                if emit_event:
                    await emit_event("reference_expanded", {
                        "count": len(expanded_refs),
                        "summary": summary
                    })
        except Exception as e:
            logger.warning(f"Failed to expand @references: {e}")

    # Query chain tracking: correlate iterations with this user turn
    chain = QueryChainTracking(user_message_preview=user_message[:100], source="user")

    # Clear per-turn ephemeral state that should not leak across user messages.
    state.loop_guidance.clear()
    state.disabled_tools.clear()
    state.blocked_repeat_calls = 0
    state.empty_reply_retries = 0
    state.stop_hook_feedback_used = False
    state.verify_attempts = 0
    state.transition = ""

    # Policies
    stream_retry_policy = settings.stream_retry_policy or DefaultStreamRetryPolicy(settings)
    answer_gate = AnswerGate(enabled=settings.answer_gate_enabled)
    reflection_policy = settings.reflection_policy or (
        MultiPerspectiveReflectionPolicy(settings)
        if getattr(settings, "reflection_multi_perspective", False)
        else DefaultReflectionPolicy(settings)
    )

    # Tool execution context
    workspace_root = session_context.workspace_root if session_context is not None else None
    if workspace_root is None and state.workspace_context and hasattr(state.workspace_context, 'root_path'):
        workspace_root = state.workspace_context.root_path

    effective_permission_context = permission_context or PermissionContext()
    tool_ctx = ToolExecutionContext(
        permission=effective_permission_context,
        session_id=session_id, task_id=task_id,
        metadata=dict(metadata or {}), emit_event=emit_event,
        stream_callback=stream_callback, workspace_root=workspace_root,
        allow_network=effective_permission_context.mode == "bypass",
        task_manager=task_manager, background_manager=background_manager,
        terminal_manager=getattr(session_context, "terminal_manager", None) if session_context is not None else None,
        checkpoint_manager=getattr(state, "checkpoint_manager", None),
        permission_checker=permission_checker,
        conversation_id=getattr(state, "conversation_id", ""),
        llm=llm,
    )

    full_text = ""
    max_output_recovery_count = 0
    max_output_recovered_text = ""
    next_user_message = user_message

    # Skills auto-detect
    if skill_manager:
        try:
            for skill_name in skill_manager.auto_detect(user_message):
                if skill_manager.activate(skill_name):
                    if skill_name not in state.active_skills:
                        state.active_skills.append(skill_name)
                    yield AgentEvent(type="skill_activated",
                                     data={"skill_name": skill_name,
                                           "description": f"Auto-activated skill: {skill_name}"})
        except asyncio.CancelledError:
            state.stopped_reason = "interrupted"
            raise
        except Exception as exc:
            logger.debug("Skills auto-detect failed: %s", exc)

    # Record user message
    ctx.append_user(user_message)

    # Hook: session_start (fires once per session, on the first turn)
    hook_mgr = get_hook_manager()
    if hook_mgr and session_id and hook_mgr.has_hooks(HookEvent.SESSION_START):
        session_hook = await hook_mgr.run_session_start_once(session_id)
        if session_hook.has_feedback:
            ctx.append_user(session_hook.feedback)

    # Hook: user_prompt_submit
    if hook_mgr and hook_mgr.has_hooks(HookEvent.USER_PROMPT_SUBMIT):
        prompt_hook = await hook_mgr.run_user_prompt_submit(user_message)
        if prompt_hook.has_feedback:
            ctx.append_user(prompt_hook.feedback)

    base_tool_schemas = tool_registry.get_schemas(
        budget=budget.tool_schemas,
        permission_checker=permission_checker,
        permission_context=tool_ctx.permission,
        mcp_registry_version=_mcp_registry_version(),
    )
    bypass_full_access = tool_ctx.permission.mode == "bypass"
    if (
        bool((metadata or {}).get("requires_explicit_workspace"))
        and workspace_root is None
        and not bypass_full_access
    ):
        workspace_bound_tools = _workspace_bound_tool_names(base_tool_schemas)
        if workspace_bound_tools:
            state.disable_tools(workspace_bound_tools, _NO_WORKSPACE_GUIDANCE)
    clarification_only = user_message_missing_required_location(user_message)
    if clarification_only:
        state.add_loop_guidance(
            "The user request is missing a city, area, or physical location. "
            "Ask one concise location question before using tools."
        )
    mcp_instructions = _collect_mcp_instructions()
    state.tool_runtime_guidance = build_tool_runtime_guidance(base_tool_schemas, mcp_instructions)

    # Resume from checkpoint if available (long-running task recovery)
    if session_id and (metadata or {}).get("resume_from_checkpoint"):
        checkpoint = load_latest_checkpoint(session_id)
        if checkpoint:
            logger.info(f"Resuming from checkpoint: session={session_id}, iterations={checkpoint.iterations}")
            state.iterations = checkpoint.iterations
            state.max_iterations = max(state.max_iterations, checkpoint.iterations + settings.max_iterations)
            state.reply = checkpoint.reply
            state.active_skills = checkpoint.active_skills
            state.disabled_tools = set(checkpoint.disabled_tools)
            state._last_mutation_index = checkpoint.last_mutation_index
            state.last_verified_mutation_index = checkpoint.last_verified_mutation_index
            # Restore messages history
            ctx.load_snapshot({"history": checkpoint.messages})
            # Restore tool_calls (convert dicts back to ToolCallRecord)
            from backend.agent.state import ToolCallRecord
            state.tool_calls = [ToolCallRecord(**tc) for tc in checkpoint.tool_calls]
            metadata.setdefault("run_id", checkpoint.run_id or run_record.run_id)
            metadata.setdefault("conversation_id", checkpoint.conversation_id or run_record.conversation_id)
            if checkpoint.resume_payload:
                metadata.update({k: v for k, v in checkpoint.resume_payload.items() if k not in {"run_id", "conversation_id"}})
            yield AgentEvent(
                type="system_notice",
                data={
                    "title": "Resumed from checkpoint",
                    "message": f"Continuing from iteration {checkpoint.iterations}. Previous stop reason: {checkpoint.stopped_reason}",
                },
            )

    # Phase 2: Main loop (the kernel)
    guardrail_controller = ToolCallGuardrailController(
        is_idempotent=lambda name, args: _tool_is_idempotent(tool_registry, name, args)
    )
    error_controller = ErrorWithholdingController()
    try:
        while True:
            # Breathing room between iterations (prevents API flooding)
            if state.iterations > 0:
                await asyncio.sleep(0.15)

            depth = chain.next_iteration()
            logger.info("%s Iteration %d", chain.to_log_context(), depth)

            # 进度事件：告知前端当前迭代数（仅调试模式可见）
            yield _agent_progress(
                f"Iteration {state.iterations + 1}/{settings.max_iterations}",
                stage="status",
                status="running",
                id=f"agent:iter:{state.iterations}",
                phase="iteration",
                label="Agent working",
                count=state.iterations + 1,
                visibility="debug",
                iteration_id=_iteration_id(state),
            )

            # Termination: max iterations
            if state.iterations >= settings.max_iterations:
                logger.warning("Max iterations reached: %d", settings.max_iterations)

                # 🔧 修复：强制生成最终总结
                if state.tool_calls:
                    # 有工具结果，生成总结
                    fallback_text = _tool_result_fallback_reply(
                        state,
                        reason=f"已达到最大迭代次数限制（{settings.max_iterations}次）。"
                    )
                    if fallback_text:
                        yield _fallback_recovery_progress_event(
                            state,
                            event_id=f"agent:max-iterations:fallback:{state.iterations}",
                            summary="Max iterations reached; using completed tool results",
                        )
                        yield _fallback_recovery_text_event(fallback_text)
                        ctx.append_assistant(fallback_text)
                        state.reply = fallback_text
                        logger.info("Generated forced summary after max_iterations")

                yield AgentEvent.error(
                    message=f"已达到最大迭代次数限制（{settings.max_iterations}次）。已根据现有工具结果生成总结。",
                    recoverable=True, error_type="budget",
                )
                state.stopped_reason = "max_iterations"
                completed_event = _complete_run_record(
                    "failed",
                    summary="Max iterations reached",
                    error="max_iterations",
                )
                if completed_event is not None:
                    yield completed_event
                break

            # Termination: stagnation hard stop (safety net — guardrail controller handles most cases)
            if state.blocked_repeat_calls >= settings.stagnation_limit:
                detail = state.get_stagnation_detail(settings.stagnation_limit)
                if detail:
                    logger.warning("Stagnation hard stop: %s", detail)
                    yield AgentEvent.error(
                        message=(
                            "已停止：模型持续尝试被阻止的工具调用且未改变策略。"
                            "请重新描述你的请求，或明确指定下一步操作。"
                        ),
                        recoverable=True, error_type="stagnant",
                    )
                    state.stopped_reason = "stagnation"
                    completed_event = _complete_run_record(
                        "failed",
                        summary="Stagnation stop",
                        error="stagnation",
                    )
                    if completed_event is not None:
                        yield completed_event
                    break
                logger.debug(
                    "Continuing after %d blocked calls without repeated-call detail",
                    state.blocked_repeat_calls,
                )

            # Context pipeline: budget check and compaction
            tool_schemas = _filter_disabled_tool_schemas(base_tool_schemas, state.disabled_tools)
            if clarification_only:
                tool_schemas = _filter_clarification_tool_schemas(tool_schemas)
            state.tool_runtime_guidance = build_tool_runtime_guidance(tool_schemas, mcp_instructions)

            async for ev in _manage_context_budget(ctx, state, budget, tool_schemas):
                yield ev
            if state.stopped_reason:
                break

            # Reconcile dangling tool_call_ids before building the next request.
            reconcile = getattr(ctx, "reconcile_dangling_tool_calls", None)
            if callable(reconcile):
                reconcile()

            # Build messages and call the LLM
            active_user_message = next_user_message
            next_user_message = user_message
            messages = await ctx.build(user_message=active_user_message, state=state)

            state.iterations += 1
            iteration_id = _iteration_id(state)
            loop_started_at = _epoch_ms()
            runtime.update_phase(run_record.run_id, "execute", summary="Model deciding next action")
            yield AgentEvent.agent_phase_updated(
                run_record.run_id,
                "execute",
                summary="Model deciding next action",
                role=run_record.role,
                conversation_id=run_record.conversation_id,
            )
            yield AgentEvent.loop_started(
                loop_id=iteration_id,
                iteration_id=iteration_id,
                title="正在思考",
                summary="模型正在确认下一步行动",
                started_at=loop_started_at,
            )

            # Stream the LLM response (with retry ladder)
            full_text = ""
            pending_tool_calls: list[ToolCallEvent] = []
            saw_partial_tool_call = False
            final_tool_batch_received = False
            usage = UsageInfo()
            finish_reason = ""
            stream_attempt = 0
            stream_recovery_attempted = False
            runtime_action_summary_emitted = False
            process_text_emitted = False
            streaming_tool_executor = StreamingToolExecutor(
                state=state,
                tool_registry=tool_registry,
                permission_checker=permission_checker,
                permission_context=permission_context,
                tool_ctx=tool_ctx,
                stagnation_limit=settings.stagnation_limit,
            )
            _thinking_chars = 0  # guard: break out of endless thinking loops

            def _merge_pending_tool_calls(incoming: list[ToolCallEvent]) -> None:
                nonlocal pending_tool_calls
                if not incoming:
                    return
                by_id = {tc.id: index for index, tc in enumerate(pending_tool_calls)}
                for tc in incoming:
                    index = by_id.get(tc.id)
                    if index is None:
                        by_id[tc.id] = len(pending_tool_calls)
                        pending_tool_calls.append(tc)
                    else:
                        pending_tool_calls[index] = tc

            try:
                while True:
                    should_retry = False
                    # Reset the thinking-char guard per stream attempt. It only fires
                    # when full_text/pending_tool_calls are still empty (see line ~632),
                    # which is exactly the retry precondition — so a transient error
                    # mid-thinking must not carry its char count into the fresh stream,
                    # or a healthy retry gets truncated early on a shrunken budget.
                    _thinking_chars = 0
                    stream_iter = llm.stream_chat(messages, tools=tool_schemas).__aiter__()
                    first_event = True
                    while True:
                        timeout = settings.stream_timeout_seconds if first_event else 60.0
                        try:
                            async with asyncio.timeout(timeout):
                                event = await stream_iter.__anext__()
                        except StopAsyncIteration:
                            break
                        first_event = False
                        if event.type == StreamEventType.TEXT_CHUNK:
                            full_text += event.content
                            if settings.live_text_streaming and event.content:
                                yield AgentEvent.text_chunk(_scrub_thinking_tags(event.content))
                        elif event.type == StreamEventType.THINKING_CHUNK:
                            _thinking_chars += len(event.content or "")
                            yield AgentEvent.thinking_chunk(
                                "模型推理已隐藏",
                                source="provider",
                                visibility="debug",
                                is_raw_provider_reasoning=True,
                            )
                            # Safety: if the model has been thinking for >8000 chars
                            # without emitting any text or tool calls, break the stream
                            # so the loop can retry or fall back. This prevents DeepSeek
                            # thinking mode from looping indefinitely.
                            if _thinking_chars > 8000 and not full_text and not pending_tool_calls:
                                logger.warning("Thinking exceeded 8000 chars with no output — breaking stream")
                                break
                        elif event.type == StreamEventType.IMAGE_CHUNK:
                            yield AgentEvent.image_chunk(event.image_data, event.image_media_type)
                        elif event.type == StreamEventType.TOOL_CALL_START:
                            saw_partial_tool_call = True
                        elif event.type == StreamEventType.TOOL_CALL_DELTA:
                            saw_partial_tool_call = True
                        elif event.type == StreamEventType.TOOL_CALL:
                            _merge_pending_tool_calls(event.tool_calls)
                            saw_partial_tool_call = saw_partial_tool_call or bool(pending_tool_calls)
                            if event.tool_calls_final:
                                final_tool_batch_received = True
                            else:
                                streaming_tool_executor.add_tools(event.tool_calls)
                            if pending_tool_calls and full_text.strip() and not process_text_emitted and not settings.live_text_streaming:
                                process_event = _model_process_text_event(
                                    full_text,
                                    pending_tool_calls,
                                    iteration_id=iteration_id,
                                    source="post_tool" if state.tool_calls else "model_preamble",
                                )
                                if process_event is not None:
                                    yield process_event
                                    process_text_emitted = True
                            if pending_tool_calls and not runtime_action_summary_emitted:
                                action_event = _runtime_action_summary_event(
                                    pending_tool_calls,
                                    iteration_id=iteration_id,
                                )
                                if action_event is not None:
                                    yield action_event
                                    runtime_action_summary_emitted = True
                            if pending_tool_calls and event.tool_calls_final:
                                # A complete tool-call block is enough to hand
                                # control to the tool executor. Do not wait for a
                                # trailing provider DONE frame; adapters can emit
                                # that after tool_calls only to carry usage.
                                break
                        elif event.type == StreamEventType.DONE:
                            usage = event.usage
                            finish_reason = event.finish_reason
                        elif event.type == StreamEventType.ERROR:
                            # Recovery ladder: retry transient errors
                            classification = classify_llm_error(event.content)
                            incomplete_tool_stream = saw_partial_tool_call and not pending_tool_calls
                            if not full_text and not pending_tool_calls and not classification.fatal:
                                decision = stream_retry_policy.decide_retry(event.content, stream_attempt)
                                if decision.should_retry:
                                    stream_attempt += 1
                                    logger.warning("Retrying stream (%d): %s", stream_attempt, event.content)
                                    yield _agent_progress(
                                        "模型流中断，正在重试",
                                        stage="status",
                                        status="running",
                                        id=f"agent:recover:{state.iterations}:{stream_attempt}",
                                        phase="recover",
                                        label="模型重试",
                                        summary="模型流中断，正在重试",
                                        visibility="timeline",
                                        count=stream_attempt,
                                        step_id=f"recover:{state.iterations}",
                                        iteration_id=_iteration_id(state),
                                    )
                                    # Hermes: jittered backoff to prevent thundering herd
                                    _jittered_delay = decision.delay_seconds * (1.0 + random.random() * 0.3)
                                    await asyncio.sleep(_jittered_delay)
                                    should_retry = True
                                    break
                            # Error withholding: try recovery before surfacing
                            can_withhold_error = (
                                not stream_recovery_attempted
                                and not incomplete_tool_stream
                            )
                            if can_withhold_error and error_controller.is_withholdable(
                                classification.error_type,
                                has_partial_text=bool(full_text),
                                has_tool_calls=bool(pending_tool_calls),
                            ):
                                withheld = error_controller.withhold(
                                    event.content, classification.error_type,
                                    strategies=[
                                        RecoveryStrategy(
                                            "emergency_compact",
                                            "Emergency compaction to reduce context size",
                                            lambda s=state, c=ctx: _try_emergency_compact(s, c),
                                        ),
                                    ],
                                )
                                recovered = False
                                for strategy in withheld.recovery_strategies:
                                    try:
                                        if await strategy.try_recover(state, ctx):
                                            error_controller.record_recovery(strategy.name, True)
                                            state.transition = f"recovered_{strategy.name}"
                                            recovered = True
                                            break
                                    except Exception as rec_exc:
                                        error_controller.record_recovery(strategy.name, False, str(rec_exc))

                                if recovered:
                                    error_controller.clear()
                                    stream_recovery_attempted = True
                                    should_retry = True
                                    break

                                error_controller.clear()
                                # Fall through to _degrade_and_finish

                            async for ev in _degrade_and_finish(
                                state=state,
                                ctx=ctx,
                                llm=llm,
                                user_message=user_message,
                                usage=usage,
                                full_text=full_text,
                                pending_tool_calls=pending_tool_calls,
                                profile=_RecoveryProfile(
                                    event_id=f"agent:recover:{state.iterations}",
                                    completed_message="Model stream interrupted; saved partial content",
                                    completed_summary="Stream failed after partial text was produced",
                                    recovered_summary="Stream failed; non-streaming fallback produced an answer",
                                    failed_message="Model stream failed",
                                    failed_summary="Model stream failed",
                                    partial_stopped_reason="partial_stream_error",
                                    recovered_stopped_reason="recovered_stream_error",
                                    failed_stopped_reason=(
                                        "incomplete_tool_stream"
                                        if incomplete_tool_stream
                                        else classification.error_type if classification.fatal else "api_error"
                                    ),
                                    error_message=(
                                        "模型开始生成工具调用，但工具参数流没有完整结束。请重试本轮请求。"
                                        if incomplete_tool_stream
                                        else _format_llm_error(event.content)
                                    ),
                                    error_type=(
                                        "incomplete_tool_stream"
                                        if incomplete_tool_stream
                                        else classification.error_type
                                    ),
                                    recoverable=True if incomplete_tool_stream else not classification.fatal,
                                    provider_error_type=classification.provider_error_type,
                                    emit_failed_progress=False,
                                    allow_last_resort=not classification.fatal and not incomplete_tool_stream,
                                    allow_partial_text_commit=not saw_partial_tool_call,
                                    live_text_streaming=settings.live_text_streaming,
                                ),
                            ):
                                yield ev
                            break

                    if should_retry:
                        continue
                    break

            except asyncio.TimeoutError:
                logger.warning("LLM stream timeout: %ss", settings.stream_timeout_seconds)

                async for ev in _degrade_and_finish(
                    state=state,
                    ctx=ctx,
                    llm=llm,
                    user_message=user_message,
                    usage=usage,
                    full_text=full_text,
                    pending_tool_calls=pending_tool_calls,
                    profile=_RecoveryProfile(
                        event_id=f"agent:timeout:{state.iterations}",
                        completed_message="Model response timed out; saved partial content",
                        completed_summary="Timeout after partial text was produced",
                        recovered_summary="Timeout after tool execution; non-streaming fallback produced an answer",
                        failed_message="Model response timed out",
                        failed_summary="Model response timed out",
                        partial_stopped_reason="partial_timeout",
                        recovered_stopped_reason="recovered_timeout",
                        failed_stopped_reason="timeout",
                        error_message="Model response timed out. Tool results already collected remain in context; please retry.",
                        error_type="timeout",
                        recoverable=True,
                        allow_partial_text_commit=not saw_partial_tool_call,
                        live_text_streaming=settings.live_text_streaming,
                    ),
                ):
                    yield ev
                break

            except Exception as exc:
                logger.error("LLM call failed: %s", exc, exc_info=True)
                classification = classify_llm_error(exc)

                async for ev in _degrade_and_finish(
                    state=state,
                    ctx=ctx,
                    llm=llm,
                    user_message=user_message,
                    usage=usage,
                    full_text=full_text,
                    pending_tool_calls=pending_tool_calls,
                    profile=_RecoveryProfile(
                        event_id=f"agent:error:{state.iterations}",
                        completed_message="Model request failed; saved partial content",
                        completed_summary="Request failed after partial text was produced",
                        recovered_summary="Request failed; non-streaming fallback produced an answer",
                        failed_message="Model request failed",
                        failed_summary="Model request failed",
                        partial_stopped_reason="partial_api_error",
                        recovered_stopped_reason="recovered_api_error",
                        failed_stopped_reason=classification.error_type if classification.fatal else "api_error",
                        error_message=_format_llm_error(f"LLM API request failed: {exc}"),
                        error_type=classification.error_type,
                        recoverable=not classification.fatal,
                        provider_error_type=classification.provider_error_type,
                        allow_last_resort=not classification.fatal,
                        allow_partial_text_commit=not saw_partial_tool_call,
                        live_text_streaming=settings.live_text_streaming,
                    ),
                ):
                    yield ev
                break

            if state.stopped_reason:
                break

            record_actual_usage = getattr(ctx, "record_actual_usage", None)
            if callable(record_actual_usage):
                record_actual_usage(usage)
            chain.record_usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
            )

            # Scrub internal reasoning tags from model text (hermes pattern).
            # Some models/proxies leak <thinking> blocks into the text stream.
            if full_text and "<" in full_text:
                scrubbed = _scrub_thinking_tags(full_text)
                if scrubbed != full_text:
                    # Text streamed live with leaked reasoning tags. Keep the
                    # protocol append-only; future message/tombstone semantics
                    # should handle true replacement if needed.
                    full_text = scrubbed

            if saw_partial_tool_call and (not pending_tool_calls or not final_tool_batch_received):
                streaming_tool_executor.cancel_remaining()
                yield AgentEvent.error(
                    message="模型开始生成工具调用，但工具参数流没有完整结束。请重试本轮请求。",
                    recoverable=True,
                    error_type="incomplete_tool_stream",
                )
                state.stopped_reason = "incomplete_tool_stream"
                break

            if _is_max_output_finish_reason(finish_reason):
                if max_output_recovery_count < _MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
                    if full_text:
                        max_output_recovered_text += full_text
                        ctx.append_assistant(full_text)
                    else:
                        ctx.append_assistant("[output truncated before visible text]")
                    next_user_message = _MAX_OUTPUT_RECOVERY_PROMPT
                    max_output_recovery_count += 1
                    state.transition = "max_output_tokens_recovery"
                    yield _agent_progress(
                        "模型输出被截断，正在续写",
                        stage="status",
                        status="running",
                        id=f"agent:max-output:{state.iterations}:{max_output_recovery_count}",
                        phase="recover",
                        label="续写",
                        summary="模型输出被截断，正在续写",
                        visibility="timeline",
                        count=max_output_recovery_count,
                        step_id=f"recover:{state.iterations}",
                        iteration_id=_iteration_id(state),
                    )
                    continue

                yield AgentEvent.error(
                    message="模型输出多次达到长度限制，自动续写已停止。请要求我分段继续，或降低单次输出规模。",
                    recoverable=True,
                    error_type="budget",
                )
                state.stopped_reason = "budget_exceeded"
                break

            # Decision point: tool_calls -> execute -> loop; no tool_calls -> done

            if not pending_tool_calls:
                # Empty response — escalating nudge ladder, then a forced fallback
                # / explicit error to avoid both an infinite loop and a SILENT
                # zero-output "completed". This fires whether or not prior tool
                # calls exist: a model that returns nothing on the very first turn
                # (e.g. a proxy returning an empty 200 body) must surface a signal,
                # never a blank "done" event.
                if not full_text.strip():
                    _had_tool_results = bool(state.tool_calls)
                    if state.empty_reply_retries == 0:
                        state.empty_reply_retries = 1
                        state.transition = "empty_reply_nudge_1"
                        ctx.append_assistant("(empty)")
                        ctx.append_user(
                            "你执行了工具调用但返回了空回复。请根据上面的工具结果提供你的回答。"
                            if _had_tool_results else
                            "你返回了空回复。请直接回答用户的问题。"
                        )
                        full_text = ""
                        continue

                    # Second empty response — force fallback and break.
                    # (Claude Code pattern: don't waste more than 1 retry)
                    state.transition = "empty_reply_fallback"
                    full_text = _tool_result_fallback_reply(state, reason="模型多次返回空回复。")
                    if full_text:
                        yield _fallback_recovery_progress_event(
                            state,
                            event_id=f"agent:empty-reply:fallback:{state.iterations}",
                            summary="Empty model reply; using completed tool results",
                        )
                        yield _fallback_recovery_text_event(full_text)
                        state.stopped_reason = "completed"
                        yield _usage_done_event(usage)
                    else:
                        failed_tool_reply = _failed_tool_result_fallback_reply(state)
                        if failed_tool_reply:
                            full_text = failed_tool_reply
                            yield _fallback_recovery_progress_event(
                                state,
                                event_id=f"agent:empty-reply:failed-tools:{state.iterations}",
                                summary="Empty model reply; surfacing failed tool details",
                            )
                            yield _fallback_recovery_text_event(full_text)
                            yield AgentEvent.error(
                                message="Tool calls failed and the model did not produce a final reply.",
                                recoverable=True,
                                error_type="tool_error",
                            )
                            state.stopped_reason = "tool_error"
                            yield _usage_done_event(usage)
                            break
                        # No tool results to summarize (e.g. empty reply on the very
                        # first turn): emit an explicit error instead of a silent
                        # zero-output "done", so the user knows the turn failed.
                        yield AgentEvent.error(
                            message="模型多次返回空回复，未能生成答案。请重试或换一种提问方式。",
                            recoverable=True, error_type="empty_reply",
                        )
                        state.stopped_reason = "empty_reply"
                        yield _usage_done_event(usage)
                    break

                candidate_text = (
                    f"{max_output_recovered_text}{full_text}"
                    if max_output_recovered_text
                    else full_text
                )

                # Stop hook (user-configured hooks only)
                hook_mgr = get_hook_manager()
                if hook_mgr and hook_mgr.has_hooks(HookEvent.STOP):
                    hook_result = await hook_mgr.run_stop(
                        user_message, candidate_text, tool_results=state.tool_calls
                    )
                    if hook_result.has_feedback and not state.stop_hook_feedback_used:
                        state.stop_hook_feedback_used = True
                        state.transition = "stop_hook_feedback"
                        yield AgentEvent.text_replace("")
                        ctx.append_assistant(candidate_text)
                        ctx.append_user(hook_result.feedback)
                        full_text = ""
                        continue

                # Answer-confidence gate: if recent tool calls all failed and
                # the model is replying without fixing anything, nudge it to
                # retry or acknowledge the failure explicitly.
                if state.tool_calls and state.heal_attempts < state.max_heal_attempts:
                    recent_results = state.tool_calls[-3:]
                    all_failed = all(_is_failed_tool_record(r) for r in recent_results)
                    if all_failed and len(recent_results) >= 2:
                        state.heal_attempts += 1
                        state.total_retries += 1
                        if state.total_retries > state.max_total_retries:
                            yield AgentEvent.error(
                                message=f"重试次数过多（{state.total_retries}次）。请简化请求或换一种方式。",
                                recoverable=False, error_type="max_retries",
                            )
                            state.stopped_reason = "max_retries"
                            yield _usage_done_event(usage)
                            break
                        yield AgentEvent.text_replace("")
                        ctx.append_assistant(candidate_text)
                        ctx.append_user(
                            "你最近的工具调用全部失败了。请修复根本原因并重试，"
                            "或者明确告诉用户哪里出了问题以及你无法完成什么。"
                            "不要假装任务成功了。"
                        )
                        full_text = ""
                        continue

                # Action-level verification: a turn that mutated the workspace
                # must pass the configured verify command before its final
                # answer is accepted. Failures are fed back for self-repair.
                if (
                    settings.verify_command
                    and tool_ctx.workspace_root is not None
                    and state.has_unverified_mutations
                    and state.verify_attempts < state.max_verify_attempts
                ):
                    runtime.update_phase(run_record.run_id, "verify", summary=f"Running {settings.verify_command}")
                    yield AgentEvent.agent_phase_updated(
                        run_record.run_id,
                        "verify",
                        summary=f"Running {settings.verify_command}",
                        role=run_record.role,
                        conversation_id=run_record.conversation_id,
                    )
                    yield AgentEvent.verification_started(
                        run_record.run_id,
                        command=settings.verify_command,
                        conversation_id=run_record.conversation_id,
                    )
                    yield _agent_progress(
                        "正在运行验证命令",
                        stage="status",
                        status="running",
                        id=f"agent:verify:{state.iterations}",
                        phase="verify",
                        label="验证",
                        summary=f"运行 {settings.verify_command}",
                        visibility="timeline",
                        step_id=f"verify:{state.iterations}",
                        iteration_id=_iteration_id(state),
                    )
                    verify_ok, verify_output = await _run_verify_command(
                        settings.verify_command,
                        tool_ctx.workspace_root,
                        settings.verify_timeout_seconds,
                    )
                    yield AgentEvent.verification_result(
                        run_record.run_id,
                        passed=verify_ok,
                        output=verify_output,
                        command=settings.verify_command,
                        conversation_id=run_record.conversation_id,
                    )
                    if verify_ok:
                        state.mark_mutations_verified()
                        yield _agent_progress(
                            "验证通过",
                            stage="status",
                            status="completed",
                            id=f"agent:verify:{state.iterations}",
                            phase="verify",
                            label="验证",
                            summary="验证命令通过",
                            visibility="timeline",
                            step_id=f"verify:{state.iterations}",
                            iteration_id=_iteration_id(state),
                        )
                    else:
                        state.verify_attempts += 1
                        yield _agent_progress(
                            "验证失败，正在修复",
                            stage="status",
                            status="failed",
                            id=f"agent:verify:{state.iterations}",
                            phase="verify",
                            label="验证",
                            summary="验证命令未通过，已反馈给模型修复",
                            visibility="timeline",
                            count=state.verify_attempts,
                            step_id=f"verify:{state.iterations}",
                            iteration_id=_iteration_id(state),
                        )
                        state.transition = "verify_failed_retry"
                        yield AgentEvent.text_replace("")
                        ctx.append_assistant(candidate_text)
                        ctx.append_user(
                            f"你的修改未通过验证命令 `{settings.verify_command}`（非零退出码）。"
                            "请根据下面的输出修复问题后再给最终回答；如果无法修复，请如实说明原因，"
                            "不要假装任务成功了。\n\n"
                            f"验证输出：\n{verify_output}"
                        )
                        full_text = ""
                        continue

                answer_gate_result = answer_gate.evaluate(user_message, candidate_text, state)
                if not answer_gate_result.ok and answer_gate_result.feedback:
                    if candidate_text.strip():
                        yield AgentEvent.stream_resume(
                            getattr(state, "conversation_id", "") or "",
                            None,
                            accumulated_text=candidate_text,
                        )
                    yield AgentEvent.text_replace("")
                    ctx.append_assistant(candidate_text)
                    _reset_history_after_draft_retry(ctx, candidate_text)
                    state.total_retries += 1
                    if state.total_retries > state.max_total_retries:
                        yield AgentEvent.error(
                            message=f"重试次数过多（{state.total_retries}次）。将基于当前结果给出回答。",
                            recoverable=True, error_type="max_retries",
                        )
                        state.stopped_reason = "max_retries"
                        yield _usage_done_event(usage)
                        break
                    # 以系统标记前缀注入用户消息，避免模型混淆系统校验与真实用户意图
                    ctx.append_user(f"[系统完整性校验] {answer_gate_result.feedback}")
                    full_text = ""
                    continue

                final_text = candidate_text
                runtime.update_phase(run_record.run_id, "final", summary="Preparing final answer")
                yield AgentEvent.agent_phase_updated(
                    run_record.run_id,
                    "final",
                    summary="Preparing final answer",
                    role=run_record.role,
                    conversation_id=run_record.conversation_id,
                )
                reflection = await reflection_policy.maybe_reflect(
                    user_message,
                    final_text,
                    state,
                    llm,
                )
                addendum_text = ""
                if reflection and reflection.verdict == "revise" and reflection.addendum:
                    addendum = reflection.addendum
                    if not addendum.startswith("\n"):
                        addendum = f"\n\n{addendum}"
                    final_text = f"{final_text}{addendum}"
                    full_text = final_text
                    addendum_text = addendum

                if final_text:
                    if settings.live_text_streaming:
                        # Base answer already streamed live token-by-token; only
                        # a post-stream reflection addendum needs emitting, then a
                        # contentless finalize seals the streamed block as final.
                        if addendum_text:
                            yield AgentEvent.text_chunk(addendum_text)
                        yield AgentEvent.text_chunk(
                            "",
                            source="model_final",
                            visibility="final",
                            phase="final",
                            finalize=True,
                        )
                    else:
                        yield AgentEvent.text_chunk(final_text, source="model_final", visibility="final")
                    ctx.append_assistant(
                        # Truncation recovery already appended the earlier partial
                        # blocks to history (one per recovery iteration). Append
                        # only the unrecovered tail here so the recovered text
                        # isn't duplicated in context. state.reply keeps the full
                        # concatenation for display.
                        final_text[len(max_output_recovered_text):]
                        if max_output_recovered_text
                        else final_text
                    )
                    state.reply = final_text
                loop_completed_at = _epoch_ms()
                yield AgentEvent.loop_completed(
                    loop_id=iteration_id,
                    iteration_id=iteration_id,
                    title="已处理",
                    summary="已生成最终回答",
                    started_at=loop_started_at,
                    completed_at=loop_completed_at,
                    duration_ms=max(0, loop_completed_at - loop_started_at),
                    tool_call_count=0,
                )
                state.stopped_reason = "completed"
                completed_event = _complete_run_record("completed", summary="Final answer committed")
                if completed_event is not None:
                    yield completed_event
                yield AgentEvent.done(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                )
                break

            # A productive turn (the model emitted tool calls) breaks any run of
            # empty replies, so reset the consecutive-empty counter. Without this
            # the counter accumulates *total* empty replies across the whole turn
            # and the third non-consecutive empty reply trips the forced fallback
            # prematurely in long agentic sessions.
            state.empty_reply_retries = 0

            # Execute tool calls. Only schema-safe calls are written into LLM
            # history; malformed calls still produce UI/state events and
            # runtime guidance, but are not sent back to strict gateways.
            pending_tool_calls = repair_tool_call_sequence(state, pending_tool_calls, tool_registry, tool_ctx)
            for _tc in pending_tool_calls:
                chain.record_tool_call()
            history_tool_calls = [
                tc for tc in pending_tool_calls
                if tool_call_is_safe_for_model_history(tc, tool_registry)
            ]
            if history_tool_calls:
                # Preserve model's reasoning text alongside tool_calls
                # (Anthropic/OpenAI pattern: assistant message = text + tool_use)
                ctx.append_assistant_tool_calls(history_tool_calls, content=full_text)
            elif full_text.strip():
                # Unsafe tool calls still need an assistant message so the
                # history is valid (no orphaned tool_result messages).
                ctx.append_assistant(full_text)

            async for ev in _execute_tool_batch(
                pending_tool_calls,
                ctx=ctx, state=state,
                tool_registry=tool_registry,
                permission_checker=permission_checker,
                approval_handler=approval_handler,
                skill_manager=skill_manager,
                permission_context=tool_ctx.permission,
                tool_ctx=tool_ctx,
                stagnation_limit=settings.stagnation_limit,
                guardrail_controller=guardrail_controller,
                prefetched_results=streaming_tool_executor.prefetched_results,
            ):
                yield ev
            streaming_tool_executor.cancel_remaining()

            loop_completed_at = _epoch_ms()
            tool_call_count = len(pending_tool_calls)
            yield AgentEvent.loop_completed(
                loop_id=iteration_id,
                iteration_id=iteration_id,
                title="已处理",
                summary=(
                    f"已完成 {tool_call_count} 个工具调用"
                    if tool_call_count != 1
                    else "已完成 1 个工具调用"
                ),
                started_at=loop_started_at,
                completed_at=loop_completed_at,
                duration_ms=max(0, loop_completed_at - loop_started_at),
                tool_call_count=tool_call_count,
            )

            # Guardrail halt: progressive stagnation detected by the controller
            if guardrail_controller.halt_decision is not None:
                decision = guardrail_controller.halt_decision
                halt_msg = guardrail_halt_response(decision)
                logger.warning("Guardrail halt: %s (count=%d)", decision.code, decision.count)
                yield AgentEvent.error(
                    message=halt_msg,
                    recoverable=True, error_type="stagnant",
                )
                state.stopped_reason = "stagnation"
                break

    except asyncio.CancelledError:
        state.stopped_reason = "interrupted"
        if full_text:
            ctx.append_assistant(full_text)
        completed_event = _complete_run_record("cancelled", summary="Interrupted", error="cancelled")
        if completed_event is not None:
            yield completed_event
        raise

    # Ensure the run record is always marked complete. Several termination paths
    # (empty_reply, max_retries, budget_exceeded, incomplete_tool_stream,
    # tool_error, and the _degrade_and_finish ladder) break out of the loop
    # without calling _complete_run_record, which would leave the run stuck in
    # "running" and never emit agent.run.completed. _complete_run_record is
    # idempotent (guarded by run_completed_emitted), so this is a no-op when an
    # inner path already completed it (max_iterations, stagnation, final answer).
    if not run_completed_emitted:
        stop = state.stopped_reason or "unknown"
        status = "completed" if stop == "completed" else "failed"
        completed_event = _complete_run_record(
            status,
            summary="Final answer committed" if stop == "completed" else f"Run ended: {stop}",
            error="" if stop == "completed" else stop,
        )
        if completed_event is not None:
            yield completed_event

    # Phase 3: Post-session checkpoint and memory
    # Save checkpoint if the task didn't complete naturally (timeout/error/interrupt)
    # so it can be resumed later with /resume.
    if state.stopped_reason not in ("completed", None) and session_id:
        try:
            save_run_checkpoint(
                session_id=session_id,
                user_message=user_message,
                iterations=state.iterations,
                reply=state.reply,
                messages=ctx.export_snapshot().get("history", []) if ctx else [],
                tool_calls=state.tool_calls,
                active_skills=state.active_skills,
                disabled_tools=state.disabled_tools,
                stopped_reason=state.stopped_reason,
                last_mutation_index=state._last_mutation_index,
                last_verified_mutation_index=state.last_verified_mutation_index,
                run_id=run_record.run_id,
                conversation_id=str(getattr(state, "conversation_id", "") or ""),
                resume_payload={
                    "run_id": run_record.run_id,
                    "conversation_id": str(getattr(state, "conversation_id", "") or ""),
                    "role": run_record.role,
                },
            )
        except Exception as exc:
            logger.debug("Checkpoint save failed: %s", exc)
    elif state.stopped_reason == "completed" and session_id:
        # Clear checkpoints on successful completion
        try:
            clear_checkpoints(session_id)
        except Exception as exc:
            logger.debug("Checkpoint clear failed: %s", exc)

    if ctx and ctx.history_length > 4:
        try:
            await asyncio.to_thread(_save_session_memory, state, vector_memory)
        except Exception as exc:
            logger.debug("Session memory save failed: %s", exc)


def _save_session_memory(state: AgentState, vector_memory: Any | None = None) -> None:
    """Save key session facts to long-term vector memory."""
    if vector_memory is None:
        return

    parts = []
    if state.user_message:
        parts.append(f"User request: {state.user_message[:200]}")
    for tc in state.tool_calls[:5]:
        name = tc.tool_name if hasattr(tc, "tool_name") else tc.get("name", "?")
        output = tc.tool_output if hasattr(tc, "tool_output") else tc.get("result", "")
        parts.append(f"Tool {name}: {(output or '')[:100]}")
    if state.stopped_reason:
        parts.append(f"Stop reason: {state.stopped_reason}")
    if state.active_skills:
        parts.append(f"Active skills: {', '.join(state.active_skills)}")
    if not parts:
        return
    content = "\n".join(parts)
    tags = [f"session:{state.stopped_reason}"] + [f"skill:{s}" for s in state.active_skills]
    vector_memory.remember(content, tags=tags, importance=3)
    vector_memory.flush()
