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
import inspect
import json
import logging
import os
import random
import re
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, cast, get_args

from backend.agent.context import ContextBuilder
from backend.agent.coordinator import (
    COORDINATOR_GUIDANCE,
    coordinator_finalization_feedback,
    coordinator_mode_enabled,
    coordinator_toolset_policy,
    maybe_enable_coordinator_from_user_message,
)
from backend.agent.query_chain import QueryChainTracking
from backend.agent.error_withholding import ErrorWithholdingController, RecoveryStrategy, is_media_size_error
from backend.agent.message import AgentEvent
from backend.agent.cache_metrics import args_signature, cache_metric_event
from backend.agent.skill_events import skill_process_event
from backend.agent.checkpoint import save_run_checkpoint, load_latest_checkpoint, clear_checkpoints
from backend.agent.prompting import build_tool_runtime_guidance, clear_loaded_prompt_packs
from backend.agent.prompt_cache import (
    build_prompt_cache_safe_params,
    observe_prompt_cache_break,
    prompt_cache_fork_diagnostic,
)
from backend.agent.policies import (
    DefaultReflectionPolicy,
    DefaultStreamRetryPolicy,
    MultiPerspectiveReflectionPolicy,
)
from backend.hooks.manager import HookEvent, get_hook_manager
from backend.agent.progress import (
    agent_progress as _agent_progress,
)
from backend.agent.runtime_spans import runtime_span
from backend.agent.state import AgentState, TerminalReason
from backend.services.context_budget import manage_context_budget as _manage_context_budget
from backend.agent.loop_process_events import model_process_text_event as _model_process_text_event
from backend.agent.loop_fallbacks import (
    failed_tool_result_fallback_reply as _failed_tool_result_fallback_reply,
    fallback_copy as _fallback_copy,
    fallback_recovery_progress_event as _fallback_recovery_progress_event,
    fallback_recovery_text_events as _fallback_recovery_text_events,
    is_failed_tool_record as _is_failed_tool_record,
    is_nonfatal_tool_record as _is_nonfatal_tool_record,
    is_user_visible_tool_output as _is_user_visible_tool_output,
    prefers_chinese_fallback as _prefers_chinese_fallback,
    stream_text_events as _stream_text_events,
    successful_tool_result_records as _successful_tool_result_records,
    timeout_tool_result_reply as _timeout_tool_result_reply,
    tool_result_fallback_reply as _tool_result_fallback_reply,
)
from backend.agent.runtime import AgentRunStatus, AgentRuntime, default_runtime
from backend.agent.tool_execution import (
    StreamingToolExecutor,
    execute_tool_batch as _execute_tool_batch,
    prepare_tool_call_sequence,
    tool_call_is_safe_for_model_history,
)
from backend.agent.tool_guardrails import (
    ToolCallGuardrailController,
    guardrail_halt_response,
)
from backend.agent.tool_runtime import tool_is_idempotent
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.feature_flags import feature_enabled
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
    stream_chat_with_request_metadata,
)
from backend.llm.capabilities import capabilities_for_adapter, require_tool_calling
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.tool_search import build_deferred_tools_prompt_block
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import is_subagent_permission_context, subagent_toolset_policy

logger = logging.getLogger(__name__)


# ── Terminal-reason helpers ─────────────────────────────────────────


def _set_terminal_reason(state: AgentState, reason: TerminalReason) -> TerminalReason:
    state.stopped_reason = reason
    return reason


def _terminal_run_status(reason: str | None) -> AgentRunStatus:
    if reason == "completed" or str(reason or "").startswith(("partial_", "recovered_")):
        return "completed"
    if reason == "interrupted":
        return "cancelled"
    return "failed"


def _terminal_run_summary(reason: str | None) -> str:
    if reason == "completed":
        return "Final answer committed"
    if str(reason or "").startswith("partial_"):
        return "Partial answer committed after provider interruption"
    if str(reason or "").startswith("recovered_"):
        return "Recovered answer committed"
    if reason == "interrupted":
        return "Interrupted"
    return f"Run ended: {reason or 'unknown'}"


def _terminal_run_error(reason: str | None) -> str:
    status = _terminal_run_status(reason)
    if status == "completed":
        return ""
    if status == "cancelled":
        return "cancelled"
    return reason or "unknown"


def _terminal_should_save_checkpoint(reason: str | None) -> bool:
    return _terminal_run_status(reason) != "completed"


def _terminal_should_clear_checkpoints(reason: str | None) -> bool:
    return _terminal_run_status(reason) == "completed"


# Constants

LLM_STREAM_TIMEOUT_SECONDS = 180.0
_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3
# cc ESCALATED_MAX_TOKENS: when the first max_tokens truncation is hit while
# still on the small default cap, retry the SAME request once at a much higher
# cap before falling back to multi-turn continuation (query.ts:1195-1221).
_MAX_OUTPUT_ESCALATED_TOKENS = 64000
_MAX_OUTPUT_ESCALATION_THRESHOLD = 16000
# Stop hook feedback may inject more than once (cc stopHookActive allows
# multiple rounds); a small count cap guards against a prompt-too-long death
# loop when a hook keeps blocking.
_STOP_HOOK_FEEDBACK_LIMIT = 3
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
_TOOL_CALL_TRAILING_DONE_GRACE_SECONDS = 2.0
_FINAL_ANSWER_IDLE_DONE_GRACE_SECONDS = 0.0
_FINAL_ANSWER_OPEN_ENDED_IDLE_DONE_GRACE_SECONDS = 0.0
_LAST_RESORT_TIMEOUT_SECONDS = 120.0
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


def _hook_manager_has_hooks(hook_mgr: Any, event: HookEvent) -> bool:
    has_hooks = getattr(hook_mgr, "has_hooks", None)
    if not callable(has_hooks):
        return False
    try:
        return bool(has_hooks(event))
    except Exception as exc:
        logger.debug("hook has_hooks(%s) failed: %s", event, exc)
        return False


def _append_assistant_history(
    ctx: ContextBuilder,
    content: str,
    *,
    phase: str = "",
    provider_items: list[dict[str, Any]] | None = None,
) -> None:
    append_assistant = ctx.append_assistant
    try:
        parameters = inspect.signature(append_assistant).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "phase" in parameters:
        kwargs["phase"] = phase
    if accepts_kwargs or "provider_items" in parameters:
        kwargs["provider_items"] = provider_items
    append_assistant(content, **kwargs)


def _append_assistant_tool_calls_history(
    ctx: ContextBuilder,
    tool_calls: list[ToolCallEvent],
    *,
    content: str = "",
    phase: str = "",
    provider_items: list[dict[str, Any]] | None = None,
) -> None:
    append_assistant_tool_calls = ctx.append_assistant_tool_calls
    try:
        parameters = inspect.signature(append_assistant_tool_calls).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "content" in parameters:
        kwargs["content"] = content
    if accepts_kwargs or "phase" in parameters:
        kwargs["phase"] = phase
    if accepts_kwargs or "provider_items" in parameters:
        kwargs["provider_items"] = provider_items
    append_assistant_tool_calls(tool_calls, **kwargs)


def _looks_like_complete_final_answer(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    # Avoid sealing while a fenced code block is still open.
    if stripped.count("```") % 2:
        return False
    return bool(re.search(r"[.!?。！？…][\"'”’)\]}）】]*$", stripped))


_FUTURE_ACTION_ONLY_ZH_RE = re.compile(
    r"^(?:请?让我|我(?:接下来|下一步|现在|马上|再)|接下来我(?:会|将)|下一步我(?:会|将))"
    r"(?:会|将|来|去)?(?:重新)?"
    r"(?:抓取|获取|查询|搜索|查找|查(?:一)?下|读取|检查|核对|整理|汇总|分析|继续|优化|处理|修复|验证|测试|调查|收集)"
)
_FUTURE_ACTION_ONLY_EN_RE = re.compile(
    r"^(?:let me|i(?:'ll| will| am going to)|next[, ]+i(?:'ll| will))\s+"
    r"(?:re-)?(?:fetch|get|query|search|look up|read|check|review|organize|summari[sz]e|analy[sz]e|continue|optimi[sz]e|fix|verify|test|investigate|gather)\b",
    re.IGNORECASE,
)


def _looks_like_future_action_only_answer(text: str) -> bool:
    """Return true for a promise to act that contains no delivered result."""
    stripped = text.strip().lstrip("-*#> ")
    if not stripped:
        return False
    return bool(
        _FUTURE_ACTION_ONLY_ZH_RE.match(stripped)
        or _FUTURE_ACTION_ONLY_EN_RE.match(stripped)
    )


async def _run_stop_failure_hook(
    error: str,
    *,
    error_details: str = "",
    last_assistant_message: str = "",
) -> None:
    hook_mgr = get_hook_manager()
    if not hook_mgr or not _hook_manager_has_hooks(hook_mgr, HookEvent.STOP_FAILURE):
        return
    try:
        await hook_mgr.run_stop_failure(
            error,
            error_details=error_details,
            last_assistant_message=last_assistant_message,
        )
    except Exception as exc:
        logger.warning("stop_failure hook failed: %s", exc)
_NO_WORKSPACE_GUIDANCE = (
    "No workspace folder is open in this desktop session. Answer from conversation context only. "
    "If the request needs local files, shell, git, preview, or workspace inspection, ask the user to open a folder first."
)
# Session Context


@dataclass
class AgentLoopSessionContext:
    """Per-session runtime dependencies bag.

    All fields have concrete types (plan §8.4 — no ``Any``).
    """

    skill_manager: Any | None = None  # SkillManager — late-typed to avoid cycle
    vector_memory: Any | None = None  # VectorMemoryManager
    permission_context: PermissionContext | None = None
    workspace_root: Path | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: Any | None = None  # TaskScheduler
    background_manager: Any | None = None  # BackgroundTaskManager
    terminal_manager: Any | None = None  # TerminalManager
    cancel_event: asyncio.Event | None = None
    stream_callback: Callable[[str], None] | None = None
    emit_event: Callable[[AgentEvent], None] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _RecoveryProfile:
    event_id: str
    completed_message: str
    completed_summary: str
    recovered_summary: str
    failed_message: str
    failed_summary: str
    partial_stopped_reason: TerminalReason
    recovered_stopped_reason: TerminalReason
    failed_stopped_reason: TerminalReason
    error_message: str
    error_type: str
    recoverable: bool
    provider_error_type: str = ""
    emit_failed_progress: bool = True
    allow_last_resort: bool = True
    allow_partial_text_commit: bool = True
    live_text_streaming: bool = False
    narration_streaming: bool = False
    narration_segment_id: str = ""


# Recovery helpers

_TERMINAL_REASON_VALUES = frozenset(str(value) for value in get_args(TerminalReason))


def _terminal_reason_from_error_type(
    error_type: str | None,
    *,
    fallback: TerminalReason = "api_error",
) -> TerminalReason:
    reason = str(error_type or "").strip()
    if reason in _TERMINAL_REASON_VALUES:
        return cast(TerminalReason, reason)
    return fallback


def _format_llm_error(message: str) -> str:
    return sanitize_llm_error_message(
        message,
        classify_llm_error(message),
        include_provider_details=False,
    )


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and its children on timeout.

    On Windows, ``proc.kill()`` only terminates the main process, leaving
    child processes (e.g. ``npm test`` → ``node``) running as orphans.
    ``taskkill /T /F /PID`` kills the entire tree.
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt" and proc.pid:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


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
            **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}),
        )
        data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            _kill_process_tree(proc)
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
        from backend.api.routes_health import get_mcp_manager

        manager = get_mcp_manager()
        if manager is not None:
            return manager.get_server_instructions()
    except Exception:  # pragma: no cover - manager unavailable / not started
        pass
    return {}


def _mcp_registry_version() -> int:
    """Current MCP registry generation, for tool-schema cache invalidation."""
    try:
        from backend.api.routes_health import get_mcp_manager

        manager = get_mcp_manager()
        if manager is not None:
            return int(getattr(manager, "registry_version", 0) or 0)
    except Exception:  # pragma: no cover - manager unavailable / not started
        pass
    return 0


def _tool_is_idempotent(tool_registry: ToolRegistry, tool_name: str, args: dict[str, Any] | None) -> bool:
    """Classify idempotent calls from tool-owned runtime metadata."""
    return tool_is_idempotent(tool_name, tool_registry, args)


def _subagent_mailbox_participant_id(metadata: dict[str, Any]) -> str:
    if str(metadata.get("agent_mode") or "").strip().lower() != "subagent":
        return "parent" if str(metadata.get("run_id") or "").strip() else ""
    return str(
        metadata.get("run_id")
        or metadata.get("agent_id")
        or metadata.get("task_id")
        or ""
    ).strip()


def _format_subagent_mailbox_injection(messages: list[Any]) -> str:
    lines = [
        "<subagent_mailbox>",
        "New coordination messages addressed to this agent arrived while it was running. Treat them as current parent/teammate instructions and adjust the next step accordingly.",
    ]
    for message in messages:
        seq = int(getattr(message, "seq", 0) or 0)
        sender = str(getattr(message, "sender_id", "") or "unknown")
        recipient = str(getattr(message, "recipient_id", "") or "")
        task_id = str(getattr(message, "task_id", "") or "")
        task_suffix = f" task={task_id}" if task_id else ""
        lines.append(
            f"- seq={seq} from={sender} to={recipient}{task_suffix}: "
            f"{str(getattr(message, 'content', '') or '').strip()}"
        )
    lines.append("</subagent_mailbox>")
    return "\n".join(lines)


async def _inject_subagent_mailbox_updates(
    *,
    ctx: ContextBuilder,
    state: AgentState,
    metadata: dict[str, Any],
    conversation_id: str,
    emit_event: Any | None = None,
) -> int:
    """Pull parent/team messages into a running subagent at iteration boundaries."""
    participant_id = _subagent_mailbox_participant_id(metadata)
    runtime = metadata.get("agent_runtime")
    list_messages = getattr(runtime, "list_swarm_messages", None)
    if not participant_id or not callable(list_messages):
        return 0

    prompt_context = state.prompt_context if isinstance(state.prompt_context, dict) else {}
    highwater_key = f"subagent_mailbox_highwater:{participant_id}"
    try:
        since_seq = int(prompt_context.get(highwater_key) or 0)
    except (TypeError, ValueError):
        since_seq = 0

    messages = [
        message
        for message in list_messages(
            participant_id=participant_id,
            conversation_id=conversation_id,
            since_seq=since_seq,
            limit=20,
        )
        if str(getattr(message, "recipient_id", "") or "") in {participant_id, "all", "*"}
    ]
    if not messages:
        return 0

    max_seq = max(int(getattr(message, "seq", 0) or 0) for message in messages)
    prompt_context[highwater_key] = max_seq
    state.prompt_context = prompt_context
    ctx.append_user(_format_subagent_mailbox_injection(messages))
    state.mark_transition(
        "subagent_mailbox_update",
        message_count=len(messages),
        high_water=max_seq,
    )
    if emit_event is not None:
        try:
            await emit_event(
                "subagent.mailbox",
                {
                    "subagent_id": participant_id,
                    "count": len(messages),
                    "high_water": max_seq,
                },
            )
        except Exception as exc:
            logger.debug("subagent mailbox event failed: %s", exc)
    return len(messages)


def _format_parent_notification_message(item: dict[str, Any]) -> str:
    """Format a parent outbox notification for injection into parent context.

    Aligns with Claude Code task-notification attachments: progress stays
    separate, completion summary is injected into the next parent turn.
    """
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    subagent_id = str(item.get("subagent_id") or payload.get("subagent_id") or "").strip()
    agent_type = str(payload.get("agent_type") or "").strip() or "subagent"
    status = str(payload.get("status") or item.get("status") or "completed").strip()
    summary = str(payload.get("content") or payload.get("error") or "").strip()
    if len(summary) > 4000:
        summary = summary[:4000] + "\n…(truncated)"
    prompt_summary = str(payload.get("prompt_summary") or "").strip()
    duration_ms = payload.get("duration_ms")
    iterations = payload.get("iterations")
    tool_call_count = payload.get("tool_call_count")
    timed_out = bool(payload.get("timed_out", False))
    detach = bool(payload.get("detach_from_parent", False))
    required = payload.get("required_for_final")
    lines = [
        "<task-notification>",
        f"subagent_id: {subagent_id or 'unknown'}",
        f"agent_type: {agent_type}",
        f"status: {status}",
    ]
    if prompt_summary:
        lines.append(f"task: {prompt_summary[:300]}")
    if duration_ms is not None:
        lines.append(f"duration_ms: {duration_ms}")
    if iterations is not None:
        lines.append(f"iterations: {iterations}")
    if tool_call_count is not None:
        lines.append(f"tool_call_count: {tool_call_count}")
    if timed_out:
        lines.append("timed_out: true")
    if detach:
        lines.append("detach_from_parent: true")
    if required is not None:
        lines.append(f"required_for_final: {bool(required)}")
    lines.append("summary:")
    lines.append(summary or "(no summary)")
    lines.append("</task-notification>")
    return "\n".join(lines)


async def _inject_parent_notifications(
    *,
    ctx: ContextBuilder,
    state: AgentState,
    metadata: dict[str, Any],
    runtime: AgentRuntime | None,
    parent_run_id: str = "",
    conversation_id: str = "",
    emit_event: Any | None = None,
) -> int:
    """Inject pending parent-outbox notifications into the parent agent context.

    Closes the Claude Code loop:
      store_subagent_result -> enqueueAgentNotification -> next parent turn sees it.
    Subagent turns skip this path; they use mailbox injection instead.
    """
    agent_mode = str(metadata.get("agent_mode") or metadata.get("agentMode") or "").strip().lower()
    agent_role = str(metadata.get("agent_role") or metadata.get("role") or "main").strip().lower()
    if agent_mode == "subagent" or agent_role in {"subagent", "side_query", "background"}:
        return 0
    if runtime is None:
        return 0

    parent_run_id = str(parent_run_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if not parent_run_id and not conversation_id:
        return 0

    list_notifications = getattr(runtime, "list_parent_notifications", None)
    ack_notification = getattr(runtime, "ack_parent_notification", None)
    if not callable(list_notifications) or not callable(ack_notification):
        return 0

    try:
        notifications = list_notifications(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        logger.debug("list parent notifications failed: %s", exc)
        return 0
    pending = [
        item for item in notifications
        if isinstance(item, dict) and str(item.get("status") or "pending") in {"pending", "failed"}
    ]
    if not pending:
        return 0

    injected = 0
    for item in pending:
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get("notification_id") or "").strip()
        if not notification_id:
            continue
        try:
            ctx.append_user(_format_parent_notification_message(item))
            acked = ack_notification(
                notification_id,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
            )
            if acked is None:
                logger.debug(
                    "parent notification injected but ack returned None: %s",
                    notification_id,
                )
            injected += 1
        except Exception as exc:
            logger.debug("parent notification inject failed for %s: %s", notification_id, exc)

    if injected:
        state.mark_transition(
            "parent_notification_inject",
            message_count=injected,
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
        if emit_event is not None:
            try:
                await emit_event(
                    "parent.notifications",
                    {
                        "count": injected,
                        "parent_run_id": parent_run_id,
                        "conversation_id": conversation_id,
                    },
                )
            except Exception as exc:
                logger.debug("parent notification event failed: %s", exc)
    return injected


# Strip leaked chain-of-thought from user-visible text. Some models/proxies
# leak <thinking>/<reasoning>/<internal> (Hermes-style) or <think>...</think>
# (DeepSeek-R1 / GLM / Qwen) blocks into the text stream. Strip paired blocks,
# any standalone markers, and ChatML/ChatGLM special tokens (<|im_start|> …).
_THINKING_BLOCK_RE = re.compile(
    r"<(?:thinking|reasoning|internal|think)[^>]*>.*?</(?:thinking|reasoning|internal|think)\s*>",
    re.DOTALL | re.IGNORECASE,
)
_THINKING_MARKER_RE = re.compile(
    r"</?(?:thinking|reasoning|internal|think)\b[^>]*>",
    re.IGNORECASE,
)
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_REASONING_TAG_AT_START_RE = re.compile(
    r"^<\s*(/?)\s*(thinking|reasoning|internal|think)\b[^>]*>",
    re.IGNORECASE,
)
_REASONING_CLOSE_RE = re.compile(
    r"</\s*(?:thinking|reasoning|internal|think)\s*>",
    re.IGNORECASE,
)
_SPECIAL_TOKEN_AT_START_RE = re.compile(r"^<\|[^|>]*\|>")
_REASONING_CONTROL_PREFIXES = (
    "<think",
    "</think",
    "<thinking",
    "</thinking",
    "<reasoning",
    "</reasoning",
    "<internal",
    "</internal",
    "<|",
)


class _ThinkingStreamSanitizer:
    """Incrementally remove reasoning tags without leaking split tag chunks."""

    def __init__(self) -> None:
        self._pending = ""
        self._inside_reasoning = False

    @staticmethod
    def _looks_like_control_prefix(value: str) -> bool:
        lowered = value.lower()
        return any(
            prefix.startswith(lowered) or lowered.startswith(prefix)
            for prefix in _REASONING_CONTROL_PREFIXES
        )

    @staticmethod
    def _closing_prefix_length(value: str) -> int:
        lowered = value.lower()
        closing_prefixes = ("</think", "</thinking", "</reasoning", "</internal")
        max_length = min(len(lowered), max(len(prefix) for prefix in closing_prefixes))
        for length in range(max_length, 0, -1):
            suffix = lowered[-length:]
            if any(prefix.startswith(suffix) for prefix in closing_prefixes):
                return length
        return 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._pending += chunk
        visible: list[str] = []

        while self._pending:
            if self._inside_reasoning:
                closing = _REASONING_CLOSE_RE.search(self._pending)
                if closing is None:
                    keep = self._closing_prefix_length(self._pending)
                    self._pending = self._pending[-keep:] if keep else ""
                    break
                self._pending = self._pending[closing.end():]
                self._inside_reasoning = False
                continue

            marker_index = self._pending.find("<")
            if marker_index < 0:
                visible.append(self._pending)
                self._pending = ""
                break
            if marker_index > 0:
                visible.append(self._pending[:marker_index])
                self._pending = self._pending[marker_index:]

            tag = _REASONING_TAG_AT_START_RE.match(self._pending)
            if tag is not None:
                self._inside_reasoning = not bool(tag.group(1))
                self._pending = self._pending[tag.end():]
                continue

            special = _SPECIAL_TOKEN_AT_START_RE.match(self._pending)
            if special is not None:
                self._pending = self._pending[special.end():]
                continue

            if self._looks_like_control_prefix(self._pending):
                break

            visible.append("<")
            self._pending = self._pending[1:]

        return "".join(visible)

    def finish(self) -> str:
        # Any held content is either inside reasoning or a partial control token;
        # dropping it is safer than exposing an incomplete hidden block.
        self._pending = ""
        self._inside_reasoning = False
        return ""


def _scrub_thinking_tags(text: str) -> str:
    """Remove reasoning tags/special tokens leaked into model text output."""
    if not text or "<" not in text:
        return text
    text = _THINKING_BLOCK_RE.sub("", text)
    text = _THINKING_MARKER_RE.sub("", text)
    text = _SPECIAL_TOKEN_RE.sub("", text)
    # Preserve the model's surrounding whitespace. This helper also sanitizes
    # answer fragments before max-output continuation, where trimming the
    # first fragment would join words across provider calls ("part 1part 2").
    return text


def _is_max_output_finish_reason(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized in _MAX_OUTPUT_FINISH_REASONS


def _get_adapter_max_output(llm: Any) -> int | None:
    """Best-effort read of the adapter's configured output-token cap.

    Anthropic stores it on `_max_tokens`; the OpenAI adapter keeps it on
    `_settings.max_tokens`. A value <= 0 means "provider default, no explicit
    cap", which we treat as no cap (None) so escalation only fires when a small
    fixed cap is actually in force.
    """
    for value in (
        getattr(llm, "_max_tokens", None),
        getattr(getattr(llm, "_settings", None), "max_tokens", None),
    ):
        try:
            cap = int(value)
        except (TypeError, ValueError):
            continue
        if cap > 0:
            return cap
    return None


def _set_adapter_max_output(llm: Any, value: int) -> None:
    """Override the adapter's output-token cap in place (both known slots)."""
    if hasattr(llm, "_max_tokens"):
        try:
            llm._max_tokens = value
        except Exception:
            pass
    settings = getattr(llm, "_settings", None)
    if settings is not None and hasattr(settings, "max_tokens"):
        try:
            settings.max_tokens = value
        except Exception:
            pass


def _iteration_id(state: AgentState) -> str:
    return f"iter:{max(1, state.iterations)}"


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _resolve_turn_max_iterations(
    user_message: str,
    settings: AgentSettings,
) -> int:
    """Resolve the iteration budget for this turn.

    When dynamic_max_iterations_enabled is on, the budget starts small so
    simple turns fail fast, then extends after productive tool progress
    (handled by _DynamicIterationBudget in the loop body).  The user message
    is accepted as a parameter for forward-compatibility but currently does
    not change the budget — the dynamic extension logic in the loop body is
    message-agnostic and relies on *runtime* progress signals instead.
    """
    configured = max(1, int(settings.max_iterations or 1))
    if not settings.dynamic_max_iterations_enabled:
        return configured

    min_configured = max(1, int(settings.dynamic_max_iterations_min_configured or 1))
    if configured < min_configured:
        return configured

    target = settings.dynamic_max_iterations_simple
    return max(1, min(configured, int(target or configured)))


def _resolve_turn_iteration_hard_limit(settings: AgentSettings) -> int:
    return max(1, int(settings.max_iterations or 1))


_NON_PROGRESS_ITERATION_TOOLS = frozenset({
    "todo_write",
    "todo_read",
    "update_plan",
    "enter_plan_mode",
    "exit_plan_mode",
})
_NON_PROGRESS_OUTPUT_PREFIXES = (
    "Skipped duplicate tool call",
    "Skipped duplicate output write",
)


@dataclass
class _DynamicIterationBudget:
    """Progress-driven iteration window.

    Starts small so simple turns fail fast, then extends only after tool
    execution adds new information or performs a successful mutation.
    """

    enabled: bool
    hard_limit: int
    current_limit: int
    extension_step: int
    checked_tool_count: int = 0
    checked_mutation_index: int = 0
    seen_success_signatures: set[str] = dataclasses.field(default_factory=set)

    @classmethod
    def from_settings(
        cls,
        settings: AgentSettings,
        *,
        state: AgentState,
        initial_limit: int,
    ) -> "_DynamicIterationBudget":
        hard_limit = _resolve_turn_iteration_hard_limit(settings)
        enabled = bool(settings.dynamic_max_iterations_enabled)
        min_configured = max(1, int(settings.dynamic_max_iterations_min_configured or 1))
        if hard_limit < min_configured:
            enabled = False
        current_limit = hard_limit if not enabled else min(hard_limit, max(initial_limit, state.iterations + initial_limit))
        step = max(1, int(settings.dynamic_max_iterations_simple or initial_limit or 1))
        budget = cls(
            enabled=enabled,
            hard_limit=hard_limit,
            current_limit=max(1, current_limit),
            extension_step=step,
            checked_tool_count=len(state.tool_calls),
            checked_mutation_index=int(getattr(state, "_last_mutation_index", 0) or 0),
        )
        for record in state.tool_calls:
            if budget._record_is_successful_progress(record):
                budget.seen_success_signatures.add(state.call_signature(record.tool_name, record.tool_input))
        return budget

    def maybe_extend(self, state: AgentState) -> bool:
        if not self.enabled or self.current_limit >= self.hard_limit:
            self.checked_tool_count = len(state.tool_calls)
            self.checked_mutation_index = int(getattr(state, "_last_mutation_index", 0) or 0)
            return False

        new_records = state.tool_calls[self.checked_tool_count:]
        mutation_index = int(getattr(state, "_last_mutation_index", 0) or 0)
        progressed = mutation_index > self.checked_mutation_index

        for record in new_records:
            if not self._record_is_successful_progress(record):
                continue
            signature = state.call_signature(record.tool_name, record.tool_input)
            if signature in self.seen_success_signatures:
                continue
            self.seen_success_signatures.add(signature)
            progressed = True

        self.checked_tool_count = len(state.tool_calls)
        self.checked_mutation_index = mutation_index
        if not progressed:
            return False

        previous = self.current_limit
        self.current_limit = min(self.hard_limit, self.current_limit + self.extension_step)
        return self.current_limit > previous

    @staticmethod
    def _record_is_successful_progress(record: Any) -> bool:
        if getattr(record, "tool_name", "") in _NON_PROGRESS_ITERATION_TOOLS:
            return False
        if getattr(record, "status", "") not in {"success", "partial"}:
            return False
        output = str(getattr(record, "tool_output", "") or "")
        if any(output.startswith(prefix) for prefix in _NON_PROGRESS_OUTPUT_PREFIXES):
            return False
        return bool(
            output.strip()
            or getattr(record, "artifact_id", None)
            or getattr(record, "source_url", None)
            or getattr(record, "content_preview", None)
            or getattr(record, "evidence_type", None)
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


@dataclass(frozen=True)
class _TurnToolSchemaDerivation:
    disabled_key: tuple[str, ...]
    permission_key: tuple[Any, ...]
    tool_schemas: list[dict[str, Any]]
    tool_names: list[str]
    runtime_guidance: str
    deferred_tools_prompt_block: str = ""


def _derive_turn_tool_schema_state(
    *,
    base_tool_schemas: list[dict[str, Any]],
    disabled_tools: set[str],
    mcp_instructions: dict[str, str],
    tool_registry: Any | None = None,
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    toolset_policy: Any | None = None,
    previous: _TurnToolSchemaDerivation | None = None,
) -> _TurnToolSchemaDerivation:
    disabled_key = tuple(sorted(disabled_tools))
    permission_key = _permission_context_cache_key(permission_context)
    if (
        previous is not None
        and previous.disabled_key == disabled_key
        and previous.permission_key == permission_key
    ):
        return previous
    tool_schemas = _filter_disabled_tool_schemas(base_tool_schemas, disabled_tools)
    tool_names = sorted(_tool_schema_names(tool_schemas))
    deferred_tools_prompt_block = ""
    if "tool_search" in tool_names and tool_registry is not None:
        deferred_tools_prompt_block = build_deferred_tools_prompt_block(
            tool_registry,
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
    return _TurnToolSchemaDerivation(
        disabled_key=disabled_key,
        permission_key=permission_key,
        tool_schemas=tool_schemas,
        tool_names=tool_names,
        runtime_guidance=build_tool_runtime_guidance(tool_schemas, mcp_instructions),
        deferred_tools_prompt_block=deferred_tools_prompt_block,
    )


def _permission_context_cache_key(permission_context: PermissionContext | None) -> tuple[Any, ...]:
    if permission_context is None:
        return ("", (), (), "")
    return (
        str(getattr(permission_context, "mode", "") or ""),
        tuple(sorted(str(rule) for rule in getattr(permission_context, "tool_deny_rules", []) or [])),
        tuple(
            sorted(
                (
                    str(key),
                    str(getattr(value, "value", value)),
                )
                for key, value in (getattr(permission_context, "session_overrides", {}) or {}).items()
            )
        ),
        str(getattr(permission_context, "source", "") or ""),
    )


def _active_toolset_policy_for_context(
    *,
    coordinator_mode: bool,
    permission_context: PermissionContext,
) -> Any | None:
    if coordinator_mode:
        return coordinator_toolset_policy()
    if is_subagent_permission_context(permission_context):
        return subagent_toolset_policy()
    return None


def _set_context_stateful_history_preference(ctx: ContextBuilder, enabled: bool) -> None:
    setter = getattr(ctx, "set_prefer_stateful_history", None)
    if callable(setter):
        setter(bool(enabled))


def _provider_stateful_history_preferred(llm: LLMAdapter) -> bool:
    try:
        return bool(capabilities_for_adapter(llm).stateful_continuation)
    except Exception:
        return False


def _provider_stateful_history_effective(provider_raw: dict[str, Any]) -> bool | None:
    summary = provider_raw.get("stateful_continuation") if isinstance(provider_raw, dict) else None
    if not isinstance(summary, dict):
        return None
    if summary.get("enabled") is False:
        return False
    if summary.get("stored_response_id_hash") or summary.get("used") is True:
        return True
    if summary.get("configured") is True and summary.get("enabled") is True:
        return False
    return None


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


def _usage_done_event(
    usage: UsageInfo,
    provider_raw: dict[str, Any] | None = None,
    *,
    status: str = "completed",
    reason: str = "",
) -> AgentEvent:
    return AgentEvent.done(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        provider_raw=provider_raw,
        status=status,
        reason=reason,
    )


def _add_usage(left: UsageInfo, right: UsageInfo | None) -> UsageInfo:
    if right is None:
        return left
    return UsageInfo(
        input_tokens=left.input_tokens + int(getattr(right, "input_tokens", 0) or 0),
        output_tokens=left.output_tokens + int(getattr(right, "output_tokens", 0) or 0),
        cache_creation_input_tokens=(
            left.cache_creation_input_tokens
            + int(getattr(right, "cache_creation_input_tokens", 0) or 0)
        ),
        cache_read_input_tokens=(
            left.cache_read_input_tokens
            + int(getattr(right, "cache_read_input_tokens", 0) or 0)
        ),
        reasoning_output_tokens=(
            left.reasoning_output_tokens
            + int(getattr(right, "reasoning_output_tokens", 0) or 0)
        ),
    )


def _deepseek_prompt_cache_provider(provider_raw: dict[str, Any]) -> bool:
    summary = provider_raw.get("request_summary") if isinstance(provider_raw, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    haystack = " ".join(
        str(value or "")
        for value in (
            provider_raw.get("provider") if isinstance(provider_raw, dict) else "",
            provider_raw.get("model") if isinstance(provider_raw, dict) else "",
            summary.get("model"),
            summary.get("provider_host"),
        )
    ).lower()
    return "deepseek" in haystack


def _prompt_cache_settle_delay_seconds(
    *,
    settings: AgentSettings,
    provider_raw: dict[str, Any],
    usage: UsageInfo,
) -> float:
    if not bool(getattr(settings, "prompt_cache_settle_enabled", True)):
        return 0.0
    if not _deepseek_prompt_cache_provider(provider_raw):
        return 0.0

    input_tokens = max(0, int(getattr(usage, "input_tokens", 0) or 0))
    min_prompt_tokens = max(
        0,
        int(getattr(settings, "prompt_cache_settle_min_prompt_tokens", 12_000) or 0),
    )
    if input_tokens < min_prompt_tokens:
        return 0.0

    cache_read_tokens = max(0, int(getattr(usage, "cache_read_input_tokens", 0) or 0))
    hit_rate = (cache_read_tokens / input_tokens) if input_tokens else 0.0
    target_hit_rate = float(getattr(settings, "prompt_cache_settle_target_hit_rate", 0.92) or 0.92)
    target_hit_rate = min(1.0, max(0.0, target_hit_rate))
    if hit_rate >= target_hit_rate:
        return 0.0

    delay = float(getattr(settings, "prompt_cache_settle_delay_seconds", 2.5) or 0.0)
    return min(10.0, max(0.0, delay))


def _build_llm_request_metadata(
    *,
    metadata: dict[str, Any],
    session_id: str,
    task_id: str,
    workspace_root: Path | None,
    run_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    """Build trace-friendly provider request metadata for one agent turn."""
    explicit = metadata.get("llm_request_metadata")
    explicit_metadata: dict[str, Any] = dict(explicit) if isinstance(explicit, dict) else {}
    request_metadata: dict[str, Any] = {}

    def pop_explicit(key: str, fallback: Any = "") -> Any:
        value = explicit_metadata.pop(key, None)
        if value is None:
            return fallback
        if isinstance(value, str) and not value.strip():
            return fallback
        return value

    source = pop_explicit("minicode_source", str(metadata.get("minicode_source") or "desktop"))
    if source:
        request_metadata["minicode_source"] = source
    if session_id or explicit_metadata.get("minicode_session_id"):
        request_metadata["minicode_session_id"] = pop_explicit("minicode_session_id", session_id)
    app_session_id = pop_explicit(
        "minicode_app_session_id",
        request_metadata.get("minicode_session_id", session_id),
    )
    if app_session_id:
        request_metadata["minicode_app_session_id"] = app_session_id
    if task_id or explicit_metadata.get("minicode_task_id"):
        request_metadata["minicode_task_id"] = pop_explicit("minicode_task_id", task_id)
    cwd = str(workspace_root) if workspace_root is not None else metadata.get("cwd")
    if cwd or explicit_metadata.get("cwd"):
        request_metadata["cwd"] = pop_explicit("cwd", cwd)
    if conversation_id or explicit_metadata.get("conversation_id"):
        request_metadata["conversation_id"] = pop_explicit("conversation_id", conversation_id)
    if run_id or explicit_metadata.get("run_id"):
        request_metadata["run_id"] = pop_explicit("run_id", run_id)
    assistant_message_id = str(metadata.get("assistant_message_id") or "").strip()
    if assistant_message_id or explicit_metadata.get("turn_id"):
        request_metadata["turn_id"] = pop_explicit("turn_id", assistant_message_id)
    if assistant_message_id or explicit_metadata.get("assistant_message_id"):
        request_metadata["assistant_message_id"] = pop_explicit("assistant_message_id", assistant_message_id)
    for key, value in explicit_metadata.items():
        request_metadata.setdefault(key, value)
    return request_metadata


def _annotate_request_metadata_with_prompt_cache_fork(
    request_metadata: dict[str, Any],
    fork: dict[str, Any],
) -> None:
    """Add short scalar fork diagnostics to provider request metadata."""
    if not isinstance(fork, dict) or not fork:
        return
    prefix_shadow = fork.get("prefix_shadow")
    if not isinstance(prefix_shadow, dict):
        prefix_shadow = {}
    schema_shadow = fork.get("schema_shadow")
    if not isinstance(schema_shadow, dict):
        schema_shadow = {}
    scalar_fields = {
        "prompt_cache_fork_status": fork.get("status"),
        "prompt_cache_fork_stable_prefix": fork.get("stable_prefix"),
        "prompt_cache_parent_stable_hash": prefix_shadow.get("parent_stable_system_hash"),
        "prompt_cache_child_stable_hash": prefix_shadow.get("child_stable_system_hash"),
        "prompt_cache_parent_tools_hash": schema_shadow.get("parent_tools_hash"),
        "prompt_cache_child_tools_hash": schema_shadow.get("child_tools_hash"),
    }
    for key, value in scalar_fields.items():
        text = str(value or "").strip()
        if text:
            request_metadata[key] = text


def _prompt_cache_tracking_source(
    *,
    run_record: Any,
    session_id: str,
    task_id: str,
) -> str:
    role = str(getattr(run_record, "role", "") or "main")
    conversation_id = str(getattr(run_record, "conversation_id", "") or "").strip()
    if role == "main":
        return f"main:{conversation_id or session_id or 'default'}"
    return f"{role}:{task_id or getattr(run_record, 'run_id', '') or session_id or 'default'}"


def _provider_trace_payload(
    *,
    provider_raw: dict[str, Any],
    usage: UsageInfo,
    finish_reason: str,
    iteration_id: str,
    call_index: int,
    loop_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = dict(provider_raw or {})
    raw_usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    request_summary = raw.get("request_summary") if isinstance(raw.get("request_summary"), dict) else {}
    prompt_cache_safe_params = (
        raw.get("prompt_cache_safe_params")
        if isinstance(raw.get("prompt_cache_safe_params"), dict)
        else {}
    )
    request_summary = _merge_prompt_cache_safe_request_summary(request_summary, prompt_cache_safe_params)
    usage_payload = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
    }
    return {
        "kind": "provider_trace",
        "provider": raw.get("provider") or "",
        "model": raw.get("model") or "",
        "finish_reason": finish_reason or raw.get("finish_reason") or "",
        "event_type": raw.get("event_type") or "",
        "usage": usage_payload,
        "raw_usage": raw_usage,
        "output_items": raw.get("output_items") if isinstance(raw.get("output_items"), list) else [],
        "provider_timeline": raw.get("provider_timeline") if isinstance(raw.get("provider_timeline"), list) else [],
        "request_summary": request_summary,
        "prompt_cache_diagnostic": (
            raw.get("prompt_cache_diagnostic")
            if isinstance(raw.get("prompt_cache_diagnostic"), dict)
            else {}
        ),
        "safety": raw.get("safety") if isinstance(raw.get("safety"), dict) else {"redacted_prompt": True},
        "stateful_continuation": (
            raw.get("stateful_continuation")
            if isinstance(raw.get("stateful_continuation"), dict)
            else {}
        ),
        "loop_metrics": (
            raw.get("loop_metrics")
            if isinstance(raw.get("loop_metrics"), dict)
            else dict(loop_metrics or {})
        ),
        "iteration_id": iteration_id,
        "call_index": call_index,
    }


def _json_char_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return len(repr(value))


def _synthetic_provider_output_items(
    *,
    full_text: str,
    tool_calls: list[ToolCallEvent],
    saw_final_answer_phase: bool,
    provider_response_phase: str = "",
) -> list[dict[str, Any]]:
    """Build redacted output-item summaries when a stream ends without DONE."""
    items: list[dict[str, Any]] = []
    if str(full_text or "").strip():
        phase = "final_answer" if saw_final_answer_phase else (provider_response_phase or "unphased")
        items.append({
            "type": "message",
            "index": len(items),
            "role": "assistant",
            "phase": phase,
            "content_types": ["output_text"],
        })
    for tc in tool_calls:
        items.append({
            "type": "function_call",
            "index": len(items),
            "name": str(tc.name or ""),
            "arguments_chars": _json_char_len(tc.arguments or {}),
        })
    return items


def _merge_prompt_cache_safe_request_summary(
    request_summary: Any,
    prompt_cache_safe_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay local cache-safe prompt facts onto provider diagnostics.

    Adapters report the request shape they actually sent. The loop also knows
    stable/dynamic prompt sections before the provider call. Merging only
    hash/count metadata keeps the Inspector useful without exporting prompt text.
    """
    summary = dict(request_summary) if isinstance(request_summary, dict) else {}
    safe = prompt_cache_safe_params if isinstance(prompt_cache_safe_params, dict) else {}
    if not safe:
        return summary
    field_defaults = {
        "instructions_hash": safe.get("stable_system_hash"),
        "instructions_full_hash": safe.get("full_system_hash"),
        "tools_hash": safe.get("tools_hash"),
        "tool_names": safe.get("tool_names"),
        "tools_chars": safe.get("tools_chars"),
        "largest_tools": safe.get("largest_tools"),
        "metadata_keys": safe.get("metadata_keys"),
    }
    for key, value in field_defaults.items():
        if key not in summary and value not in (None, "", []):
            summary[key] = value
    prompt_section_summary = safe.get("prompt_section_summary")
    if isinstance(prompt_section_summary, dict) and prompt_section_summary:
        summary.setdefault("prompt_section_summary", dict(prompt_section_summary))
    message_count = safe.get("message_count")
    if "message_count" not in summary:
        try:
            summary["message_count"] = int(message_count or 0)
        except (TypeError, ValueError):
            pass
    return summary


def _loop_metrics_payload(
    *,
    turn_started_at: int,
    state: AgentState,
    provider_call_count: int,
    iteration_limit: int,
    iteration_hard_limit: int,
    tool_batch_count: int,
    turn_start_tool_call_count: int,
    pending_tool_call_count: int = 0,
    dynamic_iteration_budget_enabled: bool = False,
) -> dict[str, Any]:
    completed_tool_calls = max(0, len(state.tool_calls) - max(0, int(turn_start_tool_call_count or 0)))
    pending = max(0, int(pending_tool_call_count or 0))
    return {
        "provider_call_count": max(0, int(provider_call_count or 0)),
        "iteration": max(0, int(state.iterations or 0)),
        "iteration_limit": max(0, int(iteration_limit or 0)),
        "iteration_hard_limit": max(0, int(iteration_hard_limit or 0)),
        "tool_batch_count": max(0, int(tool_batch_count or 0)),
        "tool_call_count": completed_tool_calls + pending,
        "completed_tool_call_count": completed_tool_calls,
        "pending_tool_call_count": pending,
        "elapsed_ms": max(0, _epoch_ms() - int(turn_started_at or _epoch_ms())),
        "dynamic_iteration_budget_enabled": bool(dynamic_iteration_budget_enabled),
    }


def _populate_prompt_context(
    *,
    state: AgentState,
    metadata: dict[str, Any],
    workspace_root: Path | None,
    permission_context: PermissionContext,
) -> None:
    """Expose volatile runtime context to the next user-turn prompt block."""
    prompt_context = getattr(state, "prompt_context", None)
    if not isinstance(prompt_context, dict):
        prompt_context = {}
        state.prompt_context = prompt_context

    if session_id := str(metadata.get("session_id") or metadata.get("minicode_session_id") or "").strip():
        prompt_context["session_id"] = session_id
    if conversation_id := str(metadata.get("conversation_id") or "").strip():
        prompt_context["conversation_id"] = conversation_id

    environment = prompt_context.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    cwd = str(workspace_root or metadata.get("cwd") or environment.get("cwd") or Path.cwd())
    environment.setdefault("cwd", cwd)
    if workspace_root is not None:
        environment["workspace_roots"] = [str(workspace_root)]
    elif not isinstance(environment.get("workspace_roots"), list):
        environment["workspace_roots"] = [cwd] if cwd.strip() else []

    permission = environment.get("permission")
    if not isinstance(permission, dict):
        permission = {}
    mode = str(getattr(permission_context, "mode", "") or "default")
    if mode in {"bypass", "full_access", "full-access", "danger-full-access"}:
        file_system_type = "unrestricted"
    elif mode == "plan":
        file_system_type = "read_only"
    elif workspace_root is not None:
        file_system_type = "workspace"
    else:
        file_system_type = "computer"
    permission.update(
        {
            "mode": mode,
            "source": str(getattr(permission_context, "source", "") or "runtime"),
            "workspace_scope": str(
                getattr(permission_context, "workspace_scope", "")
                or ("project" if workspace_root else "computer")
            ),
            "file_system_type": file_system_type,
        }
    )
    environment["permission"] = permission
    prompt_context["environment"] = environment
    prompt_context["collaboration_mode"] = (
        "plan"
        if mode == "plan" or str(metadata.get("collaboration_mode") or "").strip().lower() == "plan"
        else "default"
    )
    agent_mode = str(metadata.get("agent_mode") or metadata.get("agentMode") or "build").strip().lower()
    prompt_context["agent_mode"] = agent_mode if agent_mode in {"build", "plan", "review", "explore"} else "build"
    prompt_context["previous_turn_aborted"] = bool(
        metadata.get("previous_turn_aborted") or metadata.get("turn_aborted")
    )


# Terminal-reason helpers are defined near the top of this module.



async def _sleep_or_cancel(delay_seconds: float, cancel_event: asyncio.Event | None = None) -> None:
    if delay_seconds <= 0:
        return
    if cancel_event is None:
        await asyncio.sleep(delay_seconds)
        return
    if cancel_event.is_set():
        raise asyncio.CancelledError
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay_seconds)
    except asyncio.TimeoutError:
        return
    raise asyncio.CancelledError


async def _try_emergency_compact(state: AgentState, ctx: ContextBuilder) -> bool:
    """Emergency compaction: summarize history to free up context space.

    Reactive prompt-too-long recovery — mirrors CC's reactive-compact fallback.
    ContextBuilder exposes ``full_compact`` (whole-history rewrite) and
    ``compact`` (focused); neither takes a ``force`` kwarg, so call the real
    full compaction directly. The previous ``compact(force=True)`` path always
    raised TypeError and no-op'd, leaving this safety net dead.
    """
    try:
        summary = await ctx.full_compact(restore_state=state)
        if summary:
            logger.info("[ErrorWithholding] Emergency compaction succeeded")
            return True
    except Exception as exc:
        logger.warning("[ErrorWithholding] Compaction failed: %s", exc)
    return False


_CURRENT_WEB_FACT_RE = re.compile(
    r"(?:今天|今日|最新|当前|实时|现在|新闻|天气|股价|汇率|价格|比分|赛况|"
    r"today|latest|current|live|news|weather|price|score)",
    re.IGNORECASE,
)


def _needs_fetched_web_grounding(user_message: str, tool_calls: list[Any]) -> bool:
    if not _CURRENT_WEB_FACT_RE.search(user_message or ""):
        return False
    successful_search = any(
        getattr(record, "tool_name", "") in {"web_search", "search_web"}
        and getattr(record, "status", "") == "success"
        for record in tool_calls
    )
    successful_fetch = any(
        getattr(record, "tool_name", "") in {"web_fetch", "fetch_web"}
        and getattr(record, "status", "") == "success"
        for record in tool_calls
    )
    return successful_search and not successful_fetch


def _plan_stream_retry(
    stream_retry_policy,
    error_content: str,
    stream_attempt: int,
    state: AgentState,
) -> tuple[int, AgentEvent | None, float | None]:
    """Plan a transient stream-error retry: decide via the policy and build the
    recovery progress event. Returns (new_attempt, progress_event, delay_seconds);
    progress_event is None when the policy says not to retry. The caller yields
    the event, runs the cancel-aware jittered backoff, then breaks to restart the
    stream — keeping the yield order (notice before backoff) identical to before.
    """
    decision = stream_retry_policy.decide_retry(error_content, stream_attempt)
    if not decision.should_retry:
        return stream_attempt, None, None
    new_attempt = stream_attempt + 1
    logger.warning("Retrying stream (%d): %s", new_attempt, error_content)
    progress = _agent_progress(
        "模型流中断，正在重试",
        stage="status",
        status="running",
        id=f"agent:recover:{state.iterations}:{new_attempt}",
        phase="recover",
        label="模型重试",
        summary="模型流中断，正在重试",
        visibility="timeline",
        count=new_attempt,
        step_id=f"recover:{state.iterations}",
        iteration_id=_iteration_id(state),
    )
    return new_attempt, progress, decision.delay_seconds


async def _try_strip_historical_media(state: AgentState, ctx: "ContextBuilder") -> bool:
    """Strip historical image/PDF attachments after a media-size rejection."""
    strip = getattr(ctx, "strip_historical_media", None)
    if not callable(strip):
        return False
    try:
        stats = strip(keep_recent_user_turns=1) or {}
    except Exception as exc:
        logger.debug("strip_historical_media failed: %s", exc)
        return False
    stripped = int(stats.get("images", 0) or 0) + int(stats.get("documents", 0) or 0)
    if stripped <= 0:
        return False
    state.mark_transition(
        "media_size_strip",
        images=int(stats.get("images", 0) or 0),
        documents=int(stats.get("documents", 0) or 0),
        messages=int(stats.get("messages", 0) or 0),
    )
    return True


async def _try_error_withholding_recovery(
    *,
    error_controller,
    classification,
    error_content: str,
    state: AgentState,
    ctx: "ContextBuilder",
) -> bool:
    """Try withheld recovery for prompt-too-long / media-size style failures.

    Returns True if a recovery strategy succeeded (caller retries the stream);
    False to fall through to the degradation ladder. Mutates state.transition +
    error_controller records; clears the controller either way (matching the
    previous inline behaviour).
    """
    if not feature_enabled("reactive_compact", True):
        return False
    if state.reactive_compaction_attempted:
        return False
    # Emergency compaction / media strip only help size-class errors; anything
    # else (rate limits, overloads, network) belongs to the stream retry/backoff
    # ladder (cc: reactive compact fires only for prompt-too-long/media-size).
    if not error_controller.is_withholdable(classification.error_type, error_content):
        return False

    state.reactive_compaction_attempted = True

    strategies: list[RecoveryStrategy] = []
    media_size = (
        str(classification.error_type or "") == "media_size"
        or is_media_size_error(error_content)
        or is_media_size_error(classification.error_type)
    )
    if media_size:
        strategies.append(
            RecoveryStrategy(
                "strip_historical_media",
                "Remove historical image/PDF attachments after media-size rejection",
                lambda s=state, c=ctx: _try_strip_historical_media(s, c),
            )
        )
    # Compact remains useful for both prompt-too-long and residual media payloads.
    strategies.append(
        RecoveryStrategy(
            "emergency_compact",
            "Emergency compaction to reduce context size",
            lambda s=state, c=ctx: _try_emergency_compact(s, c),
        )
    )

    withheld = error_controller.withhold(
        error_content,
        "media_size" if media_size else classification.error_type,
        strategies=strategies,
    )
    for strategy in withheld.recovery_strategies:
        try:
            if await strategy.try_recover(state, ctx):
                error_controller.record_recovery(strategy.name, True)
                state.mark_transition(
                    f"recovered_{strategy.name}",
                    error_type=("media_size" if media_size else classification.error_type),
                )
                error_controller.clear()
                return True
        except Exception as rec_exc:
            error_controller.record_recovery(strategy.name, False, str(rec_exc))
    error_controller.clear()
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

    # All three error paths (stream error / timeout / exception) can break out
    # before the normal post-stream scrub runs, so clean partial text before it
    # is committed to history or state.reply.
    if full_text and "<" in full_text:
        full_text = _scrub_thinking_tags(full_text)

    # Tier 1: preserve a partial answer only when it already reached the
    # answer channel as a live draft. Hidden pending text may be commentary or a
    # preamble, so phase-first routing must not promote it during recovery.
    if (
        profile.allow_partial_text_commit
        and (profile.live_text_streaming or profile.narration_streaming)
        and full_text.strip()
        and not pending_tool_calls
    ):
        ctx.append_assistant(full_text)
        state.reply = full_text
        state.stopped_reason = profile.partial_stopped_reason
        # Seal an already-streamed block without re-emitting its content.
        if profile.narration_streaming:
            yield AgentEvent.text_chunk(
                "",
                source="model_narration",
                visibility="final",
                phase="final",
                finalize=True,
                metadata={
                    "segmentId": profile.narration_segment_id,
                    "sealReason": "partial_recovery",
                    "promoteAllUnsealedNarration": True,
                },
            )
        else:
            yield AgentEvent.text_chunk(
                "",
                source="partial",
                visibility="final",
                phase="final",
                finalize=True,
            )
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
        injected_last_resort_message = False
        committed_last_resort_reply = False
        if callable(export_snapshot) and callable(load_snapshot):
            snapshot_before_last_resort = export_snapshot()
        try:
            ctx.append_user(
                "Use the tool results above to answer the user's original question. "
                "Give the final answer directly and do not call more tools."
            )
            injected_last_resort_message = True
            last_resort_messages = await ctx.build(state)
            last_resort_reply = await asyncio.wait_for(
                llm.simple_chat(last_resort_messages),
                timeout=_LAST_RESORT_TIMEOUT_SECONDS,
            )
            if last_resort_reply and last_resort_reply.strip():
                if snapshot_before_last_resort is not None and callable(load_snapshot):
                    load_snapshot(snapshot_before_last_resort)
                ctx.append_assistant(last_resort_reply)
                state.reply = last_resort_reply
                committed_last_resort_reply = True
                state.stopped_reason = profile.recovered_stopped_reason
                yield _fallback_recovery_progress_event(
                    state,
                    event_id=profile.event_id,
                    summary=profile.recovered_summary,
                )
                # Last-resort synthesis from saved tool results is a degraded
                # recovery, not a normal model turn: route it through the
                # timeline/recover channel (not visibility="final") and mark the
                # done event "partial" so it is never presented as a clean answer.
                for event in _fallback_recovery_text_events(last_resort_reply):
                    yield event
                yield _usage_done_event(
                    usage,
                    status="partial",
                    reason="degraded_last_resort_synthesis",
                )
                return
        except Exception as last_exc:
            logger.debug("Last resort call failed: %s", last_exc)
        finally:
            # Restore the snapshot only when the injected "last resort" prompt was
            # added but no reply was committed (call failed or returned empty).
            # On the success branch we already restored above and appended the
            # assistant reply, so re-loading here would wipe it from history.
            if (
                injected_last_resort_message
                and not committed_last_resort_reply
                and snapshot_before_last_resort is not None
                and callable(load_snapshot)
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


# Main loop


async def _resume_from_checkpoint_if_any(
    *,
    session_id: str,
    metadata: dict[str, Any],
    state: AgentState,
    ctx: "ContextBuilder",
    run_record: Any,
    max_iterations_budget: int,
    skill_manager: Any | None = None,
) -> AsyncIterator[AgentEvent]:
    """Resume state/history/tool_calls from the latest checkpoint (long-running
    task recovery). No-op if no checkpoint or resume not requested. Mutates
    state/ctx/metadata in place; yields a system_notice when it resumes.
    """
    if not (session_id and (metadata or {}).get("resume_from_checkpoint")):
        return
    checkpoint = load_latest_checkpoint(session_id)
    if checkpoint is None:
        return
    logger.info(f"Resuming from checkpoint: session={session_id}, iterations={checkpoint.iterations}")
    state.iterations = checkpoint.iterations
    state.max_iterations = max(state.max_iterations, checkpoint.iterations + max_iterations_budget)
    state.reply = checkpoint.reply
    state.active_skills = checkpoint.active_skills
    _rehydrate_active_skills_from_checkpoint(skill_manager, state)
    state.disabled_tools = set(checkpoint.disabled_tools)
    state._last_mutation_index = checkpoint.last_mutation_index
    state.last_verified_mutation_index = checkpoint.last_verified_mutation_index
    ctx.load_snapshot({"history": checkpoint.messages})
    from backend.agent.state import ToolCallRecord
    state.tool_calls = [ToolCallRecord(**tc) for tc in checkpoint.tool_calls]
    # Rebuild the stagnation/repeat hash maps from the restored records so the
    # repeat-call guard and is_stagnant() keep working post-resume (the maps are
    # not persisted). _last_mutation_index above stays as restored from disk.
    state.rebuild_stagnation_accounting()
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


def _rehydrate_active_skills_from_checkpoint(skill_manager: Any | None, state: AgentState) -> None:
    """Reload checkpointed Skill payloads so resumed runs keep SKILL.md context."""
    if skill_manager is None:
        return
    for raw_name in getattr(state, "active_skills", []) or []:
        name = str(raw_name or "").strip()
        if not name:
            continue
        try:
            is_active = getattr(skill_manager, "is_active", None)
            if callable(is_active) and is_active(name):
                continue
            activate = getattr(skill_manager, "activate", None)
            if callable(activate):
                activate(name)
        except Exception as exc:
            logger.debug("Failed to rehydrate checkpoint skill %s: %s", name, exc)
    get_active_names = getattr(skill_manager, "get_active_names", None)
    if callable(get_active_names):
        try:
            active_names = [str(name) for name in get_active_names()]
        except Exception as exc:
            logger.debug("Failed to reconcile checkpoint skills: %s", exc)
            return
        if active_names:
            active_set = set(active_names)
            restored = [name for name in state.active_skills if name in active_set]
            restored.extend(name for name in active_names if name not in restored)
            state.active_skills = restored


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

    external_metadata = metadata if isinstance(metadata, dict) else None
    metadata = dict(metadata or {})
    metadata = maybe_enable_coordinator_from_user_message(metadata, user_message)
    cancel_event = (
        session_context.cancel_event
        if session_context is not None and session_context.cancel_event is not None
        else metadata.get("cancel_event")
    )
    if not isinstance(cancel_event, asyncio.Event):
        cancel_event = None
    settings = agent_settings or AgentSettings()
    budget = token_budget or TokenBudget()
    ctx = context_builder or ContextBuilder(
        token_budget=budget, agent_settings=settings, vector_memory=vector_memory,
    )
    initial_max_iterations_limit = _resolve_turn_max_iterations(user_message, settings)
    max_iterations_limit = initial_max_iterations_limit
    state = state or AgentState(user_message=user_message, max_iterations=max_iterations_limit)
    state.max_total_retries = max(0, int(settings.turn_error_budget))
    runtime: AgentRuntime = metadata.get("agent_runtime") if isinstance(metadata.get("agent_runtime"), AgentRuntime) else default_runtime()
    query_engine_lifecycle = metadata.get("_query_engine_lifecycle") is True
    prepared_run_record = metadata.get("_query_engine_run_record")
    run_record = prepared_run_record if query_engine_lifecycle and prepared_run_record is not None else runtime.start_run(
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
    if session_id:
        metadata.setdefault("session_id", session_id)
        metadata.setdefault("minicode_session_id", session_id)
    if run_record.conversation_id:
        metadata.setdefault("conversation_id", run_record.conversation_id)
    run_completed_emitted = False

    def _complete_run_record(
        status: AgentRunStatus,
        *,
        summary: str = "",
        error: str = "",
    ) -> AgentEvent | None:
        nonlocal run_completed_emitted
        if query_engine_lifecycle:
            return None
        if run_completed_emitted:
            return None
        record = runtime.complete_run(run_record.run_id, status, summary=summary, error=error)
        run_completed_emitted = True
        return AgentEvent.agent_run_completed(record or run_record)

    if not query_engine_lifecycle:
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

    hook_mgr = get_hook_manager()
    session_hook_result = None
    prompt_hook_result = None
    # Run prompt hooks before recording the user message so updatedInput
    # replaces the original turn instead of creating duplicate user messages.
    if hook_mgr and session_id and _hook_manager_has_hooks(hook_mgr, HookEvent.SESSION_START):
        session_hook_result = await hook_mgr.run_session_start_once(session_id)
    if hook_mgr and _hook_manager_has_hooks(hook_mgr, HookEvent.USER_PROMPT_SUBMIT):
        prompt_hook_result = await hook_mgr.run_user_prompt_submit(user_message)
        if prompt_hook_result.has_updated_input:
            user_message = prompt_hook_result.updated_input
            state.user_message = user_message

    # Query chain tracking: correlate iterations with this user turn
    chain = QueryChainTracking(user_message_preview=user_message[:100], source="user")

    # Clear per-turn ephemeral state that should not leak across user messages.
    state.loop_guidance.clear()
    state.disabled_tools.clear()
    state.blocked_repeat_calls = 0
    state.empty_reply_retries = 0
    state.stop_hook_feedback_count = 0
    state.verify_attempts = 0
    state.max_output_recovery_count = 0
    state.max_output_escalated = False
    state.max_output_recovered_text = ""
    state.heal_attempts = 0
    state.reactive_compaction_attempted = False
    state.clear_transition()
    if not isinstance(getattr(state, "prompt_context", None), dict):
        state.prompt_context = {}
    clear_loaded_prompt_packs(state.prompt_context)

    # Policies
    stream_retry_policy = settings.stream_retry_policy or DefaultStreamRetryPolicy(settings)
    reflection_policy = settings.reflection_policy or (
        MultiPerspectiveReflectionPolicy(settings)
        if getattr(settings, "reflection_multi_perspective", False)
        else DefaultReflectionPolicy(settings)
    )

    # Tool execution context
    workspace_root = session_context.workspace_root if session_context is not None else None
    if workspace_root is None and state.workspace_context and hasattr(state.workspace_context, 'root_path'):
        workspace_root = state.workspace_context.root_path

    coordinator_mode = coordinator_mode_enabled(metadata)
    effective_permission_context = permission_context or PermissionContext()
    tool_ctx = ToolExecutionContext(
        permission=effective_permission_context,
        session_id=session_id, task_id=task_id,
        metadata=dict(metadata or {}), cancel_event=cancel_event, emit_event=emit_event,
        approval_handler=approval_handler,
        stream_callback=stream_callback, workspace_root=workspace_root,
        allow_network=effective_permission_context.mode == "bypass",
        task_manager=task_manager, background_manager=background_manager,
        terminal_manager=getattr(session_context, "terminal_manager", None) if session_context is not None else None,
        checkpoint_manager=getattr(state, "checkpoint_manager", None),
        permission_checker=permission_checker,
        conversation_id=getattr(state, "conversation_id", ""),
        llm=llm,
        artifact_store=artifact_store,
    )
    # Codex-style: ensure cwd is available in tool context metadata for debugging
    if workspace_root is not None and "cwd" not in tool_ctx.metadata:
        tool_ctx.metadata["cwd"] = str(workspace_root)
    _populate_prompt_context(
        state=state,
        metadata=metadata,
        workspace_root=workspace_root,
        permission_context=effective_permission_context,
    )
    tool_ctx.metadata["prompt_context"] = state.prompt_context
    tool_ctx.metadata["_context_builder"] = ctx

    async def _emit_runtime_span(
        event: str,
        *,
        span_id: str,
        iteration_id: str = "",
        phase: str = "",
        status: str = "running",
        label: str = "",
        summary: str = "",
        started_at: int | None = None,
        ended_at: int | None = None,
        duration_ms: int | None = None,
        ui_visible: bool = True,
        debug_only: bool = False,
        requires_attention: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        if emit_event is None:
            return
        ev = runtime_span(
            event,
            span_id=span_id,
            run_id=run_record.run_id,
            turn_id=str(metadata.get("turn_id") or metadata.get("assistant_message_id") or ""),
            iteration_id=iteration_id,
            phase=phase,
            status=status,
            label=label,
            summary=summary,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            agent_id=run_record.role,
            ui_visible=ui_visible,
            debug_only=debug_only,
            requires_attention=requires_attention,
            data=data,
        )
        await emit_event(ev.type, dict(ev.data))

    full_text = ""
    next_user_message = user_message
    initial_user_turn_pending = True
    # Max-output escalation state (spans iterations): when active, the adapter's
    # output cap has been temporarily raised for a single escalated retry and
    # must be restored once that retry's stream completes.
    escalation_override_active = False
    escalation_saved_max_tokens: int | None = None
    turn_usage = UsageInfo()
    from backend.llm.cost_tracker import CostTracker
    cost_session_id = str(metadata.get("cost_session_id") or session_id or "").strip()
    _cost_session_token = CostTracker.bind_session(cost_session_id)
    # Bind the current turn usage bucket so non-stream simple_chat side calls
    # (reflection / last-resort / compaction) accumulate into turn_usage.
    _turn_usage_token = LLMAdapter.bind_turn_usage(turn_usage)
    provider_call_index = 0
    try:
        turn_started_at = _epoch_ms()
        turn_start_tool_call_count = 0
        tool_batch_count = 0
        prompt_cache_settle_not_before = 0.0

        # Skills auto-detect
        if skill_manager:
            try:
                detections = (
                    skill_manager.detect(user_message)
                    if hasattr(skill_manager, "detect")
                    else [
                        type("_SkillDetection", (), {
                            "name": skill_name,
                            "trigger_mode": "implicit",
                            "reason": "匹配当前请求",
                        })()
                        for skill_name in skill_manager.auto_detect(user_message)
                    ]
                )
                for detection in detections:
                    skill_name = detection.name
                    trigger_mode = getattr(detection, "trigger_mode", "implicit")
                    reason = getattr(detection, "reason", "匹配当前请求")
                    show_skill_lifecycle = trigger_mode != "implicit"
                    is_active = getattr(skill_manager, "is_active", None)
                    already_active = (
                        is_active(skill_name)
                        if callable(is_active)
                        else skill_name in getattr(skill_manager, "_active", {})
                    )
                    if show_skill_lifecycle:
                        yield skill_process_event(
                            skill_name,
                            lifecycle="selected",
                            trigger_mode=trigger_mode,
                            reason=reason,
                            skill_manager=skill_manager,
                            loop_id="turn:skills",
                        )
                    if already_active:
                        if skill_name not in state.active_skills:
                            state.active_skills.append(skill_name)
                        if show_skill_lifecycle:
                            yield skill_process_event(
                                skill_name,
                                lifecycle="skipped",
                                trigger_mode=trigger_mode,
                                status="info",
                                reason="Skill already active",
                                skill_manager=skill_manager,
                                loop_id="turn:skills",
                            )
                        if trigger_mode == "explicit":
                            yield AgentEvent(type="skill_activated",
                                             data={"skill_name": skill_name,
                                                   "trigger_mode": trigger_mode,
                                                   "description": f"Skill already active: {skill_name}"})
                        continue
                    active_before = set(getattr(skill_manager, "get_active_names", lambda: [])())
                    if skill_manager.activate(skill_name):
                        if skill_name not in state.active_skills:
                            state.active_skills.append(skill_name)
                        active_after = set(getattr(skill_manager, "get_active_names", lambda: [])())
                        for removed_name in sorted(active_before - active_after):
                            state.active_skills = [skill for skill in state.active_skills if skill != removed_name]
                            if show_skill_lifecycle:
                                yield skill_process_event(
                                    removed_name,
                                    lifecycle="skipped",
                                    trigger_mode=trigger_mode,
                                    status="info",
                                    reason=f"与 {skill_name} 冲突，已自动停用",
                                    skill_manager=skill_manager,
                                    loop_id="turn:skills",
                                )
                        if show_skill_lifecycle:
                            yield skill_process_event(
                                skill_name,
                                lifecycle="loaded",
                                trigger_mode=trigger_mode,
                                reason=reason,
                                skill_manager=skill_manager,
                                loop_id="turn:skills",
                            )
                        yield AgentEvent(type="skill_activated",
                                         data={"skill_name": skill_name,
                                               "trigger_mode": trigger_mode,
                                               "description": f"Auto-activated skill: {skill_name}"})
                    else:
                        if show_skill_lifecycle:
                            yield skill_process_event(
                                skill_name,
                                lifecycle="failed",
                                trigger_mode=trigger_mode,
                                status="failed",
                                reason=f"Skill '{skill_name}' activation failed",
                                skill_manager=skill_manager,
                                loop_id="turn:skills",
                            )
            except asyncio.CancelledError:
                state.stopped_reason = "interrupted"
                raise
            except Exception as exc:
                logger.debug("Skills auto-detect failed: %s", exc)

        # Hook feedback/additional context is stored after start_turn renders the
        # effective user message, preserving model-visible history order.
        pending_turn_context: list[str] = []
        if session_hook_result:
            if session_hook_result.has_feedback:
                pending_turn_context.append(session_hook_result.feedback)
            if session_hook_result.has_additional_context:
                pending_turn_context.append(session_hook_result.additional_context)
        if prompt_hook_result:
            if prompt_hook_result.has_feedback:
                pending_turn_context.append(prompt_hook_result.feedback)
            if prompt_hook_result.has_additional_context:
                pending_turn_context.append(prompt_hook_result.additional_context)

        active_toolset_policy = _active_toolset_policy_for_context(
            coordinator_mode=coordinator_mode,
            permission_context=tool_ctx.permission,
        )
        base_tool_schemas = tool_registry.get_schemas(
            budget=budget.tool_schemas,
            permission_checker=permission_checker,
            permission_context=tool_ctx.permission,
            toolset_policy=active_toolset_policy,
            mcp_registry_version=_mcp_registry_version(),
        )
        if coordinator_mode:
            state.add_loop_guidance(COORDINATOR_GUIDANCE)
        bypass_full_access = tool_ctx.permission.mode == "bypass"
        if (
            bool((metadata or {}).get("requires_explicit_workspace"))
            and workspace_root is None
            and not bypass_full_access
        ):
            workspace_bound_tools = _workspace_bound_tool_names(base_tool_schemas)
            if workspace_bound_tools:
                state.disable_tools(workspace_bound_tools, _NO_WORKSPACE_GUIDANCE)
        mcp_instructions = _collect_mcp_instructions()
        turn_tool_schema_state = _derive_turn_tool_schema_state(
            base_tool_schemas=base_tool_schemas,
            disabled_tools=state.disabled_tools,
            mcp_instructions=mcp_instructions,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            permission_context=tool_ctx.permission,
            toolset_policy=active_toolset_policy,
        )
        state.prompt_context["tool_names"] = turn_tool_schema_state.tool_names
        state.tool_runtime_guidance = turn_tool_schema_state.runtime_guidance
        state.prompt_context["deferred_tools_prompt_block"] = turn_tool_schema_state.deferred_tools_prompt_block

        # Resume from checkpoint if available (long-running task recovery)
        async for ev in _resume_from_checkpoint_if_any(
            session_id=session_id,
            metadata=metadata,
            state=state,
            ctx=ctx,
            run_record=run_record,
            max_iterations_budget=initial_max_iterations_limit,
            skill_manager=skill_manager,
        ):
            yield ev

        # Baseline after checkpoint resume so loop metrics count only work done in
        # this user turn/resume attempt, not restored historical tool results.
        turn_start_tool_call_count = len(state.tool_calls)

        iteration_budget = _DynamicIterationBudget.from_settings(
            settings,
            state=state,
            initial_limit=initial_max_iterations_limit,
        )
        max_iterations_limit = iteration_budget.current_limit
        state.max_iterations = max(state.max_iterations, max_iterations_limit)

        llm_request_metadata = _build_llm_request_metadata(
            metadata=metadata,
            session_id=session_id,
            task_id=task_id,
            workspace_root=workspace_root,
            run_id=run_record.run_id,
            conversation_id=run_record.conversation_id,
        )
        prompt_cache_tracking_source = _prompt_cache_tracking_source(
            run_record=run_record,
            session_id=session_id,
            task_id=task_id,
        )
        prefer_stateful_history = _provider_stateful_history_preferred(llm)
        _set_context_stateful_history_preference(ctx, prefer_stateful_history)

        # Phase 2: Main loop (the kernel)
        guardrail_controller = ToolCallGuardrailController(
            is_idempotent=lambda name, args: _tool_is_idempotent(tool_registry, name, args)
        )
        error_controller = ErrorWithholdingController()
        deferred_cancel: asyncio.CancelledError | None = None
        try:
            while True:
                # Breathing room between iterations (prevents API flooding)
                if state.iterations > 0:
                    await _sleep_or_cancel(0.15, tool_ctx.cancel_event)
                if prompt_cache_settle_not_before > 0:
                    remaining = prompt_cache_settle_not_before - time.monotonic()
                    prompt_cache_settle_not_before = 0.0
                    if remaining > 0:
                        yield _agent_progress(
                            "等待 provider 缓存落盘",
                            stage="status",
                            status="running",
                            id=f"agent:prompt-cache-settle:{state.iterations}",
                            phase="model",
                            label="prompt cache",
                            summary=f"等待 {remaining:.1f}s 让大 prompt 前缀进入缓存",
                            visibility="debug",
                            display_scope="silent",
                            panel_hint="inspector",
                            iteration_id=_iteration_id(state),
                        )
                        await _sleep_or_cancel(remaining, tool_ctx.cancel_event)

                depth = chain.next_iteration()
                logger.info("%s Iteration %d", chain.to_log_context(), depth)

                # 进度事件：告知前端当前迭代数（仅调试模式可见）
                yield _agent_progress(
                    f"Iteration {state.iterations + 1}/{max_iterations_limit}",
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
                if state.iterations >= max_iterations_limit:
                    logger.warning("Max iterations reached: %d", max_iterations_limit)

                    reconcile = getattr(ctx, "reconcile_dangling_tool_calls", None)
                    if callable(reconcile):
                        try:
                            reconcile()
                        except Exception as reconcile_exc:
                            logger.debug("Max-iter tool trajectory reconcile failed: %s", reconcile_exc)

                    # 🔧 修复：强制生成最终总结
                    fallback_text = ""
                    if state.tool_calls:
                        # 有工具结果，生成总结
                        fallback_text = _tool_result_fallback_reply(
                            state,
                            reason=f"已达到最大迭代次数限制（{max_iterations_limit}次）。"
                        )
                        if fallback_text:
                            yield _fallback_recovery_progress_event(
                                state,
                                event_id=f"agent:max-iterations:fallback:{state.iterations}",
                                summary="Max iterations reached; using completed tool results",
                            )
                            for event in _fallback_recovery_text_events(fallback_text):
                                yield event
                            ctx.append_assistant(fallback_text)
                            state.reply = fallback_text
                            logger.info("Generated forced summary after max_iterations")

                    state.stopped_reason = "max_iterations"
                    if fallback_text:
                        completed_event = _complete_run_record(
                            "partial",
                            summary="Max iterations reached; retained completed tool results",
                        )
                    else:
                        yield AgentEvent.error(
                            message=f"已达到最大迭代次数限制（{max_iterations_limit}次）。",
                            recoverable=True,
                            error_type="budget",
                        )
                        await _run_stop_failure_hook(
                            "max_iterations",
                            error_details=f"Max iterations reached: {max_iterations_limit}",
                            last_assistant_message=state.reply,
                        )
                        completed_event = _complete_run_record(
                            "failed",
                            summary="Max iterations reached",
                            error="max_iterations",
                        )
                    if completed_event is not None:
                        yield completed_event
                    break

                # Termination: tool-call budget — hard backstop against runaway tool use
                # (e.g. the model creating files repeatedly without converging on a final reply).
                if settings.max_tool_calls > 0 and len(state.tool_calls) >= settings.max_tool_calls:
                    logger.warning("Tool-call budget reached: %d", settings.max_tool_calls)
                    if state.tool_calls:
                        fallback_text = _tool_result_fallback_reply(
                            state,
                            reason=f"已达到工具调用上限（{settings.max_tool_calls}次），停止执行。"
                        )
                        if fallback_text:
                            yield _fallback_recovery_progress_event(
                                state,
                                event_id=f"agent:max-tool-calls:fallback:{state.iterations}",
                                summary="Tool-call budget reached; using completed tool results",
                            )
                            for event in _fallback_recovery_text_events(fallback_text):
                                yield event
                            ctx.append_assistant(fallback_text)
                            state.reply = fallback_text
                    yield AgentEvent.error(
                        message=f"已达到工具调用上限（{settings.max_tool_calls}次）。已根据现有结果生成总结。",
                        recoverable=True, error_type="budget",
                    )
                    await _run_stop_failure_hook(
                        "max_tool_calls",
                        error_details=f"Tool-call budget reached: {settings.max_tool_calls}",
                        last_assistant_message=state.reply,
                    )
                    state.stopped_reason = "max_tool_calls"
                    completed_event = _complete_run_record(
                        "failed",
                        summary="Tool-call budget reached",
                        error="max_tool_calls",
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

                # Context pipeline: permission/tool visibility can change between
                # iterations (for example after enter_plan_mode), so derive schemas
                # from the current ToolExecutionContext each time.
                active_toolset_policy = _active_toolset_policy_for_context(
                    coordinator_mode=coordinator_mode,
                    permission_context=tool_ctx.permission,
                )
                base_tool_schemas = tool_registry.get_schemas(
                    budget=budget.tool_schemas,
                    permission_checker=permission_checker,
                    permission_context=tool_ctx.permission,
                    toolset_policy=active_toolset_policy,
                    mcp_registry_version=_mcp_registry_version(),
                )
                turn_tool_schema_state = _derive_turn_tool_schema_state(
                    base_tool_schemas=base_tool_schemas,
                    disabled_tools=state.disabled_tools,
                    mcp_instructions=mcp_instructions,
                    tool_registry=tool_registry,
                    permission_checker=permission_checker,
                    permission_context=tool_ctx.permission,
                    toolset_policy=active_toolset_policy,
                    previous=turn_tool_schema_state,
                )
                _populate_prompt_context(
                    state=state,
                    metadata=metadata,
                    workspace_root=workspace_root,
                    permission_context=tool_ctx.permission,
                )
                tool_schemas = turn_tool_schema_state.tool_schemas
                state.prompt_context["tool_names"] = turn_tool_schema_state.tool_names
                state.tool_runtime_guidance = turn_tool_schema_state.runtime_guidance
                state.prompt_context["deferred_tools_prompt_block"] = turn_tool_schema_state.deferred_tools_prompt_block

                active_user_message = next_user_message
                next_user_message = ""
                should_start_user_turn = initial_user_turn_pending or bool(active_user_message)
                initial_user_turn_pending = False
                if should_start_user_turn:
                    await ctx.start_turn(active_user_message, state)
                    for context_content in pending_turn_context:
                        ctx.append_user_context(context_content)
                    pending_turn_context.clear()

                capability_check = require_tool_calling(llm, tool_count=len(tool_schemas))
                if not capability_check.ok:
                    logger.error(
                        "Provider capability check failed run_id=%s capability=%s reason=%s",
                        run_record.run_id,
                        capability_check.capability,
                        capability_check.reason,
                    )
                    capabilities_payload = (
                        capability_check.capabilities.to_dict()
                        if capability_check.capabilities is not None
                        else {}
                    )
                    yield AgentEvent.error(
                        message=(
                            "当前模型或 provider 不支持工具调用，因此不能安全执行这轮 agent 任务。"
                            "请切换到支持 tool/function calling 的模型或 provider 后重试。"
                        ),
                        recoverable=True,
                        error_type="provider_capability",
                        provider_error_type="unsupported_capability",
                    )
                    yield AgentEvent.inspector_update(
                        "provider",
                        f"{run_record.run_id}:provider-capability:{state.iterations + 1}",
                        {
                            "kind": "provider_capability",
                            "capability": capability_check.capability,
                            "reason": capability_check.reason,
                            "capabilities": capabilities_payload,
                        },
                        display_scope="silent",
                        panel_hint="inspector",
                        requires_attention=True,
                    )
                    state.stopped_reason = "provider_capability"
                    completed_event = _complete_run_record(
                        "failed",
                        summary="Provider capability check failed",
                        error="provider_capability",
                    )
                    if completed_event is not None:
                        yield completed_event
                    break

                async for ev in _manage_context_budget(ctx, state, budget, tool_schemas):
                    yield ev
                if state.stopped_reason:
                    break

                await _inject_subagent_mailbox_updates(
                    ctx=ctx,
                    state=state,
                    metadata=metadata,
                    conversation_id=run_record.conversation_id,
                    emit_event=emit_event,
                )
                await _inject_parent_notifications(
                    ctx=ctx,
                    state=state,
                    metadata=metadata,
                    runtime=runtime,
                    parent_run_id=run_record.run_id,
                    conversation_id=run_record.conversation_id or str(metadata.get("conversation_id") or ""),
                    emit_event=emit_event,
                )

                # Reconcile dangling tool_call_ids before building the next request.
                reconcile = getattr(ctx, "reconcile_dangling_tool_calls", None)
                if callable(reconcile):
                    reconcile()

                # Build messages and call the LLM
                context_span_id = f"context:{run_record.run_id}:{state.iterations + 1}"
                context_started_at = _epoch_ms()
                await _emit_runtime_span(
                    "context.build.started",
                    span_id=context_span_id,
                    phase="context",
                    status="running",
                    label="context",
                    summary="Building model context",
                    started_at=context_started_at,
                    ui_visible=False,
                )
                messages = await ctx.build(state)
                context_completed_at = _epoch_ms()
                await _emit_runtime_span(
                    "context.build.completed",
                    span_id=context_span_id,
                    phase="context",
                    status="completed",
                    label="context",
                    summary="Model context ready",
                    started_at=context_started_at,
                    ended_at=context_completed_at,
                    duration_ms=context_completed_at - context_started_at,
                    ui_visible=False,
                    data={"message_count": len(messages)},
                )
                prompt_section_summary = state.prompt_context.get("prompt_section_summary")
                prompt_cache_safe_params = build_prompt_cache_safe_params(
                    messages=messages,
                    tool_schemas=tool_schemas,
                    request_metadata=llm_request_metadata,
                    prompt_section_summary=(
                        dict(prompt_section_summary)
                        if isinstance(prompt_section_summary, dict)
                        else {}
                    ),
                )
                parent_prompt_cache_safe_params = metadata.get("parent_prompt_cache_safe_params")
                prompt_cache_fork = prompt_cache_fork_diagnostic(
                    parent_prompt_cache_safe_params,
                    prompt_cache_safe_params,
                )
                if prompt_cache_fork:
                    _annotate_request_metadata_with_prompt_cache_fork(
                        llm_request_metadata,
                        prompt_cache_fork,
                    )
                    prompt_cache_safe_params = {
                        **prompt_cache_safe_params,
                        "fork_context": prompt_cache_fork,
                    }
                    metadata["prompt_cache_fork"] = prompt_cache_fork
                metadata["prompt_cache_safe_params"] = prompt_cache_safe_params
                tool_ctx.metadata["prompt_cache_safe_params"] = prompt_cache_safe_params
                if prompt_cache_fork:
                    tool_ctx.metadata["prompt_cache_fork"] = prompt_cache_fork
                if external_metadata is not None:
                    external_metadata["prompt_cache_safe_params"] = dict(prompt_cache_safe_params)
                    if prompt_cache_fork:
                        external_metadata["prompt_cache_fork"] = dict(prompt_cache_fork)

                state.iterations += 1
                iteration_id = _iteration_id(state)
                loop_started_at = _epoch_ms()
                transition_payload = state.transition_payload(
                    default_reason="turn_start" if state.iterations == 1 else "implicit_continue"
                )
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
                    transition_reason=str(transition_payload.get("reason") or ""),
                    transition_details=(
                        dict(transition_payload.get("details") or {})
                        if isinstance(transition_payload.get("details"), dict)
                        else None
                    ),
                )
                provider_progress_id = f"provider-request:{iteration_id}"
                yield AgentEvent.progress(
                    "Waiting for provider response",
                    id=provider_progress_id,
                    stage="planning",
                    phase="model",
                    status="running",
                    label="provider",
                    summary="Provider request started",
                    visibility="timeline",
                    iteration_id=iteration_id,
                    display_scope="activity",
                    panel_hint="inspector",
                )

                # Stream the LLM response (with retry ladder)
                full_text = ""
                final_candidate_text = ""
                finalizable_stream_text = ""
                pending_tool_calls: list[ToolCallEvent] = []
                saw_partial_tool_call = False
                final_tool_batch_received = False
                usage = UsageInfo()
                finish_reason = ""
                provider_raw_done: dict[str, Any] = {}
                stream_attempt = 0
                stream_recovery_attempted = False
                # Set when a recovery strategy rewrote history (emergency compaction):
                # the already-built `messages` are stale, so the retry must go back to
                # the outer loop and rebuild context (cc: tryReactiveCompact →
                # buildPostCompactMessages → continue).
                rebuild_context_and_retry = False
                process_text_emitted = False
                process_text_streamed = False
                process_text_source = "model_preamble"
                pending_process_text = ""
                pending_unphased_text = ""
                pending_unphased_visible_text = ""
                unphased_narration_stream_text = ""
                saw_final_answer_phase = False
                live_answer_streamed = False
                speculative_unphased_streamed = False
                provider_raw_final_text: dict[str, Any] = {}
                provider_response_items: list[dict[str, Any]] = []
                provider_response_phase = ""
                first_byte_at: int | None = None  # wall-clock of first stream event, for ttft
                provider_first_event_reported = False
                awaiting_trailing_tool_done = False
                streaming_tool_executor = StreamingToolExecutor(
                    state=state,
                    tool_registry=tool_registry,
                    permission_checker=permission_checker,
                    permission_context=effective_permission_context,
                    tool_ctx=tool_ctx,
                    stagnation_limit=settings.stagnation_limit,
                    guardrail_controller=guardrail_controller,
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

                def _pending_recovery_text() -> str:
                    if saw_final_answer_phase and final_candidate_text.strip():
                        return _scrub_thinking_tags(final_candidate_text)
                    if speculative_unphased_streamed and pending_unphased_text.strip():
                        return _scrub_thinking_tags(pending_unphased_text)
                    return ""

                def _accepted_answer_text() -> str:
                    if saw_final_answer_phase:
                        return _scrub_thinking_tags(final_candidate_text)
                    return _scrub_thinking_tags(pending_unphased_text or final_candidate_text)

                def _clear_pending_text_buffers() -> None:
                    nonlocal pending_process_text, pending_unphased_text, pending_unphased_visible_text
                    pending_process_text = ""
                    pending_unphased_text = ""
                    pending_unphased_visible_text = ""

                def _process_text_buffer() -> str:
                    return f"{pending_process_text}{pending_unphased_visible_text}".strip()

                def _final_answer_idle_timeout() -> float | None:
                    if _FINAL_ANSWER_IDLE_DONE_GRACE_SECONDS <= 0:
                        return None
                    if (
                        saw_final_answer_phase
                        and final_candidate_text.strip()
                        and not pending_tool_calls
                        and not saw_partial_tool_call
                        and not awaiting_trailing_tool_done
                    ):
                        return (
                            _FINAL_ANSWER_IDLE_DONE_GRACE_SECONDS
                            if _looks_like_complete_final_answer(final_candidate_text)
                            else _FINAL_ANSWER_OPEN_ENDED_IDLE_DONE_GRACE_SECONDS
                        )
                    return None

                def _maybe_stream_process_text(*, source: str) -> AgentEvent | None:
                    nonlocal process_text_streamed, process_text_source
                    if process_text_emitted:
                        return None
                    text = _process_text_buffer()
                    if not text:
                        return None
                    process_text_streamed = True
                    process_text_source = source
                    return _model_process_text_event(
                        text,
                        [],
                        iteration_id=iteration_id,
                        source=source,
                        status="running",
                    )

                def _promote_unphased_text_to_final_candidate() -> None:
                    nonlocal final_candidate_text, pending_unphased_text, pending_unphased_visible_text
                    if pending_unphased_text:
                        final_candidate_text += pending_unphased_text
                        pending_unphased_text = ""
                        pending_unphased_visible_text = ""

                def _clear_streamed_answer_draft(*, reroute_to_process: bool = False) -> list[AgentEvent]:
                    nonlocal live_answer_streamed, finalizable_stream_text
                    if not live_answer_streamed:
                        return []
                    events: list[AgentEvent] = []
                    # Only re-route explicit final_answer drafts. Unphased
                    # narration is sealed in its timeline segment and never
                    # cleared/replayed through process_text.
                    if reroute_to_process and not speculative_unphased_streamed:
                        # A provider-tagged final_answer draft is being cleared
                        # because a tool call followed — e.g. delegation
                        # narration. Re-route that text to the process area
                        # instead of discarding it, so the user sees it as
                        # process output rather than a retracted/stranded answer.
                        rerouted = (finalizable_stream_text or "").strip()
                        if rerouted:
                            process_event = _model_process_text_event(
                                rerouted,
                                [],
                                iteration_id=iteration_id,
                                source="commentary",
                                status="completed",
                            )
                            if process_event is not None:
                                events.append(process_event)
                    live_answer_streamed = False
                    finalizable_stream_text = ""
                    events.append(AgentEvent.text_replace(""))
                    return events

                def _seal_unphased_narration(reason: str) -> list[AgentEvent]:
                    nonlocal speculative_unphased_streamed
                    nonlocal pending_unphased_text, pending_unphased_visible_text
                    nonlocal unphased_narration_stream_text
                    if not speculative_unphased_streamed:
                        return []
                    event = AgentEvent.text_chunk(
                        "",
                        source="model_narration",
                        visibility="timeline",
                        phase="model",
                        finalize=True,
                        metadata={
                            "segmentId": current_segment_id,
                            "sealReason": reason,
                        },
                    )
                    speculative_unphased_streamed = False
                    pending_unphased_text = ""
                    pending_unphased_visible_text = ""
                    unphased_narration_stream_text = ""
                    return [event]

                def _flush_pending_process_text(
                    tool_calls: list[ToolCallEvent] | None = None,
                    *,
                    source: str | None = None,
                ) -> AgentEvent | None:
                    nonlocal pending_process_text, pending_unphased_text, pending_unphased_visible_text, process_text_emitted, process_text_source
                    text = _process_text_buffer()
                    pending_process_text = ""
                    pending_unphased_text = ""
                    pending_unphased_visible_text = ""
                    if not text or (process_text_emitted and not process_text_streamed):
                        return None
                    process_text_emitted = True
                    resolved_source = source or process_text_source
                    process_text_source = resolved_source
                    return _model_process_text_event(
                        text,
                        tool_calls or [],
                        iteration_id=iteration_id,
                        source=resolved_source,
                        status="completed",
                    )

                try:
                    while True:
                        should_retry = False
                        # Reset the thinking-char guard per stream attempt. It only fires
                        # when full_text/pending_tool_calls are still empty (see line ~632),
                        # which is exactly the retry precondition — so a transient error
                        # mid-thinking must not carry its char count into the fresh stream,
                        # or a healthy retry gets truncated early on a shrunken budget.
                        _thinking_chars = 0
                        visible_text_sanitizer = _ThinkingStreamSanitizer()
                        current_segment_id = f"{iteration_id}:narration:{state.iterations}:{stream_attempt}"
                        provider_span_id = f"provider:{iteration_id}:{stream_attempt + 1}"
                        provider_span_started_at = _epoch_ms()
                        await _emit_runtime_span(
                            "provider.request.started",
                            span_id=provider_span_id,
                            iteration_id=iteration_id,
                            phase="provider",
                            status="running",
                            label="provider",
                            summary="Provider request started",
                            started_at=provider_span_started_at,
                        )
                        stream_iter = stream_chat_with_request_metadata(
                            llm,
                            messages,
                            tools=tool_schemas,
                            metadata=llm_request_metadata,
                        ).__aiter__()
                        first_event = True
                        first_event_notice_emitted = False
                        while True:
                            final_answer_idle_timeout = _final_answer_idle_timeout()
                            timeout = (
                                settings.stream_timeout_seconds
                                if first_event
                                else _TOOL_CALL_TRAILING_DONE_GRACE_SECONDS
                                if awaiting_trailing_tool_done
                                else final_answer_idle_timeout
                                if final_answer_idle_timeout is not None
                            else max(0.1, float(settings.stream_timeout_seconds or LLM_STREAM_TIMEOUT_SECONDS))
                            )
                            try:
                                if first_event:
                                    first_byte_timeout_seconds = max(
                                        0.0,
                                        float(settings.first_byte_timeout_seconds or 0.0),
                                    )
                                    if first_byte_timeout_seconds > 0:
                                        timeout = min(timeout, max(0.02, first_byte_timeout_seconds))
                                    first_event_notice_seconds = min(
                                        4.0,
                                        max(0.02, min(0.25, timeout / 3)),
                                        max(0.02, timeout * 0.5),
                                    )
                                    first_event_task = asyncio.create_task(stream_iter.__anext__())
                                    try:
                                        async with asyncio.timeout(first_event_notice_seconds):
                                            event = await asyncio.shield(first_event_task)
                                    except asyncio.TimeoutError:
                                        if not first_event_notice_emitted:
                                            yield _agent_progress(
                                                "正在思考",
                                                stage="status",
                                                status="running",
                                                id=f"agent:first-byte:{state.iterations}:{stream_attempt}",
                                                phase="model",
                                                label="正在思考",
                                                summary="已发送请求，模型正在组织回复",
                                                visibility="timeline",
                                                count=stream_attempt + 1,
                                                step_id=f"first-byte:{state.iterations}",
                                                iteration_id=_iteration_id(state),
                                            )
                                            first_event_notice_emitted = True
                                        remaining_timeout = timeout - first_event_notice_seconds
                                        if remaining_timeout <= 0:
                                            first_event_task.cancel()
                                            raise
                                        first_byte_warning_seconds = max(
                                            0.0,
                                            float(settings.first_byte_warning_seconds or 0.0),
                                        )
                                        if (
                                            first_byte_warning_seconds > first_event_notice_seconds
                                            and first_byte_warning_seconds < timeout
                                        ):
                                            warning_wait = first_byte_warning_seconds - first_event_notice_seconds
                                            try:
                                                async with asyncio.timeout(warning_wait):
                                                    event = await asyncio.shield(first_event_task)
                                            except asyncio.TimeoutError:
                                                yield _agent_progress(
                                                    "模型响应较慢",
                                                    stage="status",
                                                    status="running",
                                                    id=f"agent:first-byte-slow:{state.iterations}:{stream_attempt}",
                                                    phase="model",
                                                    label="provider slow",
                                                    summary=(
                                                        f"已等待 {first_byte_warning_seconds:.1f}s "
                                                        "仍未收到首个响应事件，继续等待 provider"
                                                    ),
                                                    visibility="timeline",
                                                    count=stream_attempt + 1,
                                                    step_id=f"first-byte-slow:{state.iterations}",
                                                    iteration_id=_iteration_id(state),
                                                )
                                                remaining_timeout = timeout - first_byte_warning_seconds
                                                if remaining_timeout <= 0:
                                                    first_event_task.cancel()
                                                    raise
                                            else:
                                                remaining_timeout = 0
                                        # Wait for the first byte in bounded chunks, emitting a
                                        # heartbeat each chunk. Without this, a legitimately slow
                                        # provider stalls SILENTLY after the 12s warning and the
                                        # caller (e.g. a parent subagent's no-progress deadline,
                                        # typically 120-300s) kills a child that is correctly
                                        # waiting on first byte. The heartbeat keeps that deadline
                                        # fresh; the loop's own `timeout` still fires if the
                                        # provider truly dies.
                                        heartbeat_interval = max(
                                            5.0,
                                            min(30.0, float(settings.first_byte_warning_seconds or 20.0) or 20.0),
                                        )
                                        heartbeat_waited = 0.0
                                        while remaining_timeout - heartbeat_waited > 0:
                                            chunk = min(heartbeat_interval, remaining_timeout - heartbeat_waited)
                                            try:
                                                async with asyncio.timeout(chunk):
                                                    event = await asyncio.shield(first_event_task)
                                                break
                                            except asyncio.TimeoutError:
                                                heartbeat_waited += chunk
                                                if heartbeat_waited >= remaining_timeout:
                                                    first_event_task.cancel()
                                                    raise
                                                yield _agent_progress(
                                                    "模型仍在组织回复",
                                                    stage="status",
                                                    status="running",
                                                    id=f"agent:first-byte-heartbeat:{state.iterations}:{stream_attempt}:{int(heartbeat_waited)}",
                                                    phase="model",
                                                    label="provider waiting",
                                                    summary=(
                                                        f"已等待 provider 约 "
                                                        f"{int(heartbeat_waited + (settings.first_byte_warning_seconds or 0))}s，仍在等待首个响应事件"
                                                    ),
                                                    visibility="timeline",
                                                    count=stream_attempt + 1,
                                                    step_id=f"first-byte:{state.iterations}",
                                                    iteration_id=_iteration_id(state),
                                                )
                                else:
                                    event_task = asyncio.create_task(stream_iter.__anext__())
                                    cancel_task = (
                                        asyncio.create_task(cancel_event.wait())
                                        if cancel_event is not None
                                        else None
                                    )
                                    wait_set = {event_task}
                                    if cancel_task is not None:
                                        wait_set.add(cancel_task)
                                    done_tasks, _ = await asyncio.wait(
                                        wait_set,
                                        timeout=timeout,
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )
                                    if cancel_task is not None and cancel_task in done_tasks:
                                        event_task.cancel()
                                        with suppress(asyncio.CancelledError, Exception):
                                            await event_task
                                        raise asyncio.CancelledError
                                    if not done_tasks:
                                        event_task.cancel()
                                        with suppress(asyncio.CancelledError, Exception):
                                            await event_task
                                        raise asyncio.TimeoutError
                                    if cancel_task is not None:
                                        cancel_task.cancel()
                                    event = event_task.result()
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError:
                                if awaiting_trailing_tool_done and pending_tool_calls and final_tool_batch_received:
                                    break
                                if final_answer_idle_timeout is not None:
                                    finish_reason = finish_reason or "idle_final_answer"
                                    provider_raw_done.setdefault("finish_reason", finish_reason)
                                    # Observability: flag idle-sealed answers so the
                                    # false-seal rate can be measured downstream. Flows
                                    # into the done event's providerRaw.
                                    provider_raw_done["sealed_on_idle"] = True
                                    provider_raw_done.setdefault(
                                        "stream_idle_final_answer",
                                        {
                                            "timeout_seconds": final_answer_idle_timeout,
                                            "text_chars": len(final_candidate_text),
                                        },
                                    )
                                    logger.info(
                                        "Sealing final answer after %.2fs idle wait without provider DONE",
                                        final_answer_idle_timeout,
                                    )
                                    break
                                raise
                            first_event = False
                            if first_byte_at is None:
                                first_byte_at = _epoch_ms()
                            # SDK transparency: emit raw provider stream event for
                            # programmatic consumers. This is sdk_only=True so the
                            # UI will not render it, but external SDK consumers can
                            # access the underlying provider event.
                            raw_event = getattr(event, "raw", None)
                            if raw_event and isinstance(raw_event, dict) and raw_event:
                                provider_name = type(llm).__name__.replace("Adapter", "").lower()
                                yield AgentEvent.stream_event(
                                    provider=provider_name,
                                    event_type=event.type.value if hasattr(event.type, "value") else str(event.type),
                                    data=dict(raw_event),
                                    sdk_only=True,
                                )
                            if not provider_first_event_reported and first_byte_at is not None:
                                provider_first_event_reported = True
                                provider_wait_ms = max(0, first_byte_at - loop_started_at)
                                yield AgentEvent.progress(
                                    "Provider responded",
                                    id=provider_progress_id,
                                    stage="planning",
                                    phase="model",
                                    status="completed",
                                    label="provider",
                                    summary=f"First provider event after {provider_wait_ms}ms",
                                    detail=f"provider_wait_ms={provider_wait_ms}",
                                    visibility="timeline",
                                    iteration_id=iteration_id,
                                    display_scope="activity",
                                    panel_hint="inspector",
                                )
                                await _emit_runtime_span(
                                    "provider.first_event",
                                    span_id=provider_span_id,
                                    iteration_id=iteration_id,
                                    phase="provider",
                                    status="completed",
                                    label="provider",
                                    summary=f"First provider event after {provider_wait_ms}ms",
                                    started_at=provider_span_started_at,
                                    ended_at=first_byte_at,
                                    duration_ms=provider_wait_ms,
                                    data={"stream_attempt": stream_attempt + 1},
                                )
                            # Cooperative abort: if cancel was requested (user stop /
                            # task_stop / disconnect), bail between stream events
                            # instead of waiting for the next asyncio.timeout or
                            # task.cancel() propagation. Mirrors cc's signal.aborted
                            # check at each yield point.
                            if cancel_event is not None and cancel_event.is_set():
                                raise asyncio.CancelledError
                            if event.type == StreamEventType.TEXT_CHUNK:
                                if event.content:
                                    chunk_metadata = {
                                        "segmentId": current_segment_id,
                                        "iterationIndex": state.iterations,
                                        "streamAttempt": stream_attempt,
                                        "ttftMs": max(0, (first_byte_at or loop_started_at) - loop_started_at),
                                    }
                                    provider_raw_text = dict(getattr(event, "raw", {}) or {})
                                    provider_phase = (
                                        str(provider_raw_text.get("message_phase") or "").strip()
                                        or str(getattr(event, "phase", "") or "").strip()
                                    ).lower()
                                    visible_chunk = visible_text_sanitizer.feed(event.content)
                                    full_text += event.content
                                    if provider_phase in {"final_answer", "final"}:
                                        if not saw_final_answer_phase:
                                            if speculative_unphased_streamed:
                                                for _ev in _seal_unphased_narration("final_phase"):
                                                    yield _ev
                                            process_event = _flush_pending_process_text()
                                            if process_event is not None:
                                                yield process_event
                                        saw_final_answer_phase = True
                                        final_candidate_text += event.content
                                        if settings.live_text_streaming and visible_chunk:
                                            draft_chunk = event.content if "<" not in event.content else visible_chunk
                                            yield AgentEvent.text_chunk(
                                                draft_chunk,
                                                source="stream",
                                                visibility="draft",
                                                phase="final_answer",
                                                metadata=chunk_metadata,
                                            )
                                            live_answer_streamed = True
                                            finalizable_stream_text += visible_chunk
                                        if provider_raw_text:
                                            provider_raw_final_text.update(provider_raw_text)
                                    elif provider_phase == "commentary":
                                        pending_process_text += visible_chunk
                                        if settings.live_text_streaming:
                                            process_event = _maybe_stream_process_text(source="commentary")
                                            if process_event is not None:
                                                yield process_event
                                    else:
                                        # Legacy providers may stream plain text without
                                        # Codex-style message phases. Keep it pending
                                        # until the turn proves whether it was a tool
                                        # preamble or the final answer. When enabled,
                                        # expose it as timeline narration; a later tool
                                        # call seals that segment in place, while a
                                        # tool-free DONE promotes it once to final.
                                        pending_unphased_text += event.content
                                        pending_unphased_visible_text += visible_chunk
                                        if (
                                            settings.live_text_streaming
                                            and bool(getattr(settings, "speculative_unphased_streaming", False))
                                            and visible_chunk
                                            and not pending_tool_calls
                                            and not saw_partial_tool_call
                                        ):
                                            draft_chunk = event.content if "<" not in event.content else visible_chunk
                                            yield AgentEvent.text_chunk(
                                                draft_chunk,
                                                source="model_narration",
                                                visibility="timeline",
                                                phase="model",
                                                metadata={**chunk_metadata, "provisional": True},
                                            )
                                            speculative_unphased_streamed = True
                                            unphased_narration_stream_text += visible_chunk
                                        elif settings.live_text_streaming and not pending_tool_calls:
                                            process_event = _maybe_stream_process_text(source="model_preamble")
                                            if process_event is not None:
                                                yield process_event
                                    if provider_raw_text:
                                        chunk_metadata["providerRaw"] = provider_raw_text
                                        raw_finish_reason = str(provider_raw_text.get("finish_reason") or "")
                                        if raw_finish_reason:
                                            chunk_metadata["finishReason"] = raw_finish_reason
                                    # Phase-first routing: text only reaches the
                                    # answer channel after it is accepted as final.
                                    # Explicit commentary and unphased preambles are
                                    # held for process_text instead of being shown
                                    # as answer drafts and later retracted.
                            elif event.type == StreamEventType.FALLBACK_RESTART:
                                failed_segment_id = current_segment_id
                                if speculative_unphased_streamed:
                                    yield AgentEvent.text_replace(
                                        "",
                                        source="model_narration",
                                        visibility="timeline",
                                        phase="model",
                                        metadata={
                                            "segmentId": failed_segment_id,
                                            "sealReason": "provider_fallback",
                                        },
                                    )
                                if process_text_streamed:
                                    replacement_process = _model_process_text_event(
                                        "Previous provider response was discarded; switched to backup provider.",
                                        [],
                                        iteration_id=iteration_id,
                                        source=process_text_source,
                                        status="completed",
                                    )
                                    if replacement_process is not None:
                                        yield replacement_process
                                for _ev in _clear_streamed_answer_draft():
                                    yield _ev
                                full_text = ""
                                final_candidate_text = ""
                                finalizable_stream_text = ""
                                _clear_pending_text_buffers()
                                unphased_narration_stream_text = ""
                                saw_final_answer_phase = False
                                live_answer_streamed = False
                                speculative_unphased_streamed = False
                                process_text_emitted = False
                                process_text_streamed = False
                                process_text_source = "model_preamble"
                                usage = UsageInfo()
                                finish_reason = ""
                                provider_raw_done = {}
                                provider_raw_final_text = {}
                                provider_response_items = []
                                provider_response_phase = ""
                                awaiting_trailing_tool_done = False
                                visible_text_sanitizer = _ThinkingStreamSanitizer()
                                yield AgentEvent.progress(
                                    "Provider switched",
                                    id=f"agent:provider-fallback:{state.iterations}:{stream_attempt}",
                                    stage="planning",
                                    phase="model",
                                    status="completed",
                                    label="provider fallback",
                                    summary="Partial provider response was discarded; continuing with backup provider",
                                    detail=str(event.content or ""),
                                    visibility="timeline",
                                    iteration_id=iteration_id,
                                    display_scope="activity",
                                    panel_hint="inspector",
                                )
                            elif event.type == StreamEventType.THINKING_CHUNK:
                                _thinking_chars += len(event.content or "")
                                if event.content:
                                    yield AgentEvent.thinking_chunk(
                                        event.content,
                                        source="provider",
                                        visibility="debug",
                                        is_raw_provider_reasoning=True,
                                        provider_reasoning_type=str(
                                            (getattr(event, "raw", {}) or {}).get("provider_reasoning_type") or ""
                                        ),
                                        phase="model",
                                    )
                                # Long reasoning is valid output for R1-style models. Keep
                                # streaming it; the provider/token budget and outer timeout
                                # remain the safety boundaries.
                                if _thinking_chars > 8000 and _thinking_chars - len(event.content or "") <= 8000:
                                    logger.info("Provider reasoning exceeded 8000 chars; continuing stream")
                            elif event.type == StreamEventType.IMAGE_CHUNK:
                                yield AgentEvent.image_chunk(event.image_data, event.image_media_type)
                            elif event.type == StreamEventType.TOOL_CALL_START:
                                saw_partial_tool_call = True
                                for _ev in _seal_unphased_narration("tool_call"):
                                    yield _ev
                                for _ev in _clear_streamed_answer_draft(reroute_to_process=True):
                                    yield _ev
                            elif event.type == StreamEventType.TOOL_CALL_DELTA:
                                saw_partial_tool_call = True
                                for _ev in _seal_unphased_narration("tool_call"):
                                    yield _ev
                                for _ev in _clear_streamed_answer_draft(reroute_to_process=True):
                                    yield _ev
                            elif event.type == StreamEventType.TOOL_CALL:
                                for _ev in _seal_unphased_narration("tool_call"):
                                    yield _ev
                                for _ev in _clear_streamed_answer_draft(reroute_to_process=True):
                                    yield _ev
                                _merge_pending_tool_calls(event.tool_calls)
                                saw_partial_tool_call = saw_partial_tool_call or bool(pending_tool_calls)
                                if event.tool_calls_final:
                                    final_tool_batch_received = True
                                else:
                                    streaming_tool_executor.add_tools(event.tool_calls)
                                    # Give safe prefetched tools one scheduler turn before
                                    # requesting the provider's next stream frame. Without
                                    # this yield, synchronous/fast adapters can deliver the
                                    # final tool batch before the prefetched task even starts,
                                    # defeating the latency benefit of non-final tool blocks.
                                    await asyncio.sleep(0)
                                if (
                                    event.tool_calls_final
                                    and pending_tool_calls
                                    and not process_text_emitted
                                ):
                                    process_source = process_text_source
                                    if settings.live_text_streaming and process_source != "model_preamble_retracted":
                                        # Preserve the already-detected process-text
                                        # source. Provider commentary that leads into
                                        # a tool call must keep updating the same
                                        # commentary block instead of reopening as a
                                        # model_preamble block, which duplicates the
                                        # line in the UI.
                                        process_event = _maybe_stream_process_text(source=process_source)
                                        if process_event is not None:
                                            yield process_event
                                    process_event = _flush_pending_process_text(
                                        pending_tool_calls,
                                        source=process_source,
                                    )
                                    if process_event is not None:
                                        yield process_event
                                if pending_tool_calls and event.tool_calls_final:
                                    # Keep reading through the provider's trailing
                                    # DONE frame. Responses/Chat gateways often send
                                    # usage, prompt-cache, citations, and finish
                                    # metadata after the final tool-call block.
                                    awaiting_trailing_tool_done = True
                                    continue
                            elif event.type == StreamEventType.DONE:
                                usage = event.usage
                                finish_reason = event.finish_reason
                                provider_raw_done = dict(getattr(event, "raw", {}) or {})
                                provider_response_items = [
                                    dict(item)
                                    for item in (getattr(event, "provider_items", []) or [])
                                    if isinstance(item, dict)
                                ]
                                provider_response_phase = (
                                    str(getattr(event, "phase", "") or "").strip()
                                    or str(provider_raw_done.get("response_message_phase") or "").strip()
                                )
                                if prompt_cache_safe_params:
                                    provider_raw_done["prompt_cache_safe_params"] = dict(prompt_cache_safe_params)
                                provider_raw_done["request_summary"] = _merge_prompt_cache_safe_request_summary(
                                    provider_raw_done.get("request_summary"),
                                    prompt_cache_safe_params,
                                )
                                provider_raw_done["loop_metrics"] = _loop_metrics_payload(
                                    turn_started_at=turn_started_at,
                                    state=state,
                                    provider_call_count=provider_call_index + 1,
                                    iteration_limit=max_iterations_limit,
                                    iteration_hard_limit=iteration_budget.hard_limit,
                                    tool_batch_count=tool_batch_count,
                                    turn_start_tool_call_count=turn_start_tool_call_count,
                                    pending_tool_call_count=(
                                        len(pending_tool_calls)
                                        if pending_tool_calls and final_tool_batch_received
                                        else 0
                                    ),
                                    dynamic_iteration_budget_enabled=iteration_budget.enabled,
                                )
                                prompt_cache_diagnostic = observe_prompt_cache_break(
                                    request_summary=provider_raw_done.get("request_summary"),
                                    usage=usage,
                                    source=prompt_cache_tracking_source,
                                )
                                if prompt_cache_diagnostic:
                                    provider_raw_done["prompt_cache_diagnostic"] = prompt_cache_diagnostic
                                    logger.warning(
                                        "[PromptCacheBreak] %s",
                                        prompt_cache_diagnostic.get("reason", "unknown"),
                                    )
                                cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                                cache_creation_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                                if cache_read_tokens or cache_creation_tokens:
                                    yield cache_metric_event(
                                        cache_layer="provider.prompt",
                                        tool_name="provider",
                                        run_id=iteration_id,
                                        turn_id=iteration_id,
                                        args_signature_value=args_signature(provider_raw_done.get("request_summary") or {}),
                                        hit=cache_read_tokens > 0,
                                        estimated_saved_ms=300 if cache_read_tokens > 0 else 0,
                                        payload_size_bytes=(cache_read_tokens + cache_creation_tokens) * 4,
                                    )
                                settle_delay = _prompt_cache_settle_delay_seconds(
                                    settings=settings,
                                    provider_raw=provider_raw_done,
                                    usage=usage,
                                )
                                if settle_delay > 0:
                                    prompt_cache_settle_not_before = max(
                                        prompt_cache_settle_not_before,
                                        time.monotonic() + settle_delay,
                                    )
                                    provider_raw_done["prompt_cache_settle"] = {
                                        "delay_seconds": settle_delay,
                                        "target_hit_rate": float(
                                            getattr(settings, "prompt_cache_settle_target_hit_rate", 0.92)
                                            or 0.92
                                        ),
                                        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                                        "cache_read_input_tokens": int(
                                            getattr(usage, "cache_read_input_tokens", 0) or 0
                                        ),
                                    }
                                provider_call_index += 1
                                stateful_effective = _provider_stateful_history_effective(provider_raw_done)
                                if stateful_effective is not None and stateful_effective != prefer_stateful_history:
                                    prefer_stateful_history = stateful_effective
                                    _set_context_stateful_history_preference(ctx, prefer_stateful_history)
                                if provider_raw_done:
                                    trace_id = f"{iteration_id}:provider:{provider_call_index}"
                                    provider_raw_done.setdefault("trace_id", trace_id)
                                    yield AgentEvent.inspector_update(
                                        "provider",
                                        trace_id,
                                        _provider_trace_payload(
                                            provider_raw=provider_raw_done,
                                            usage=usage,
                                            finish_reason=finish_reason,
                                            iteration_id=iteration_id,
                                            call_index=provider_call_index,
                                            loop_metrics=provider_raw_done.get("loop_metrics"),
                                        ),
                                        display_scope="silent",
                                        panel_hint="inspector",
                                    )
                                provider_completed_at = _epoch_ms()
                                await _emit_runtime_span(
                                    "provider.request.completed",
                                    span_id=provider_span_id,
                                    iteration_id=iteration_id,
                                    phase="provider",
                                    status="completed",
                                    label="provider",
                                    summary="Provider request completed",
                                    started_at=provider_span_started_at,
                                    ended_at=provider_completed_at,
                                    duration_ms=provider_completed_at - provider_span_started_at,
                                    data={
                                        "finish_reason": finish_reason,
                                        "stream_attempt": stream_attempt + 1,
                                        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                                        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                                    },
                                )
                                awaiting_trailing_tool_done = False
                            elif event.type == StreamEventType.ERROR:
                                # Recovery ladder: retry transient errors
                                classification = classify_llm_error(event.content)
                                incomplete_tool_stream = saw_partial_tool_call and not pending_tool_calls
                                if (
                                    not full_text
                                    and not pending_tool_calls
                                    and not classification.fatal
                                    and classification.error_type not in {"prompt_too_long", "media_size"}
                                ):
                                    _new_attempt, _retry_progress, _retry_delay = _plan_stream_retry(
                                        stream_retry_policy, event.content, stream_attempt, state,
                                    )
                                    if _retry_progress is not None:
                                        stream_attempt = _new_attempt
                                        yield _retry_progress
                                        await _emit_runtime_span(
                                            "recovery.retry.started",
                                            span_id=f"recovery:{iteration_id}:{stream_attempt}",
                                            iteration_id=iteration_id,
                                            phase="recovery",
                                            status="running",
                                            label="recovery",
                                            summary="Model stream interrupted; retrying",
                                            data={
                                                "stream_attempt": stream_attempt,
                                                "provider_error_type": classification.provider_error_type,
                                                "error_type": classification.error_type,
                                            },
                                        )
                                        # Hermes: jittered backoff to prevent thundering herd
                                        _jittered_delay = _retry_delay * (1.0 + random.random() * 0.3)
                                        await _sleep_or_cancel(_jittered_delay, tool_ctx.cancel_event)
                                        should_retry = True
                                        break
                                # Error withholding: try recovery before surfacing
                                can_withhold_error = (
                                    not stream_recovery_attempted
                                    and not incomplete_tool_stream
                                )
                                if can_withhold_error and await _try_error_withholding_recovery(
                                    error_controller=error_controller,
                                    classification=classification,
                                    error_content=event.content,
                                    state=state,
                                    ctx=ctx,
                                ):
                                    stream_recovery_attempted = True
                                    rebuild_context_and_retry = True
                                    break
                                # Fall through to _degrade_and_finish

                                async for ev in _degrade_and_finish(
                                    state=state,
                                    ctx=ctx,
                                    llm=llm,
                                    user_message=user_message,
                                    usage=_add_usage(turn_usage, usage),
                                    full_text=_pending_recovery_text(),
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
                                            else _terminal_reason_from_error_type(classification.error_type)
                                            if classification.fatal
                                            else "api_error"
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
                                        live_text_streaming=live_answer_streamed,
                                        narration_streaming=speculative_unphased_streamed,
                                        narration_segment_id=current_segment_id,
                                    ),
                                ):
                                    yield ev
                                break

                        if rebuild_context_and_retry:
                            break
                        if should_retry:
                            continue
                        break

                except asyncio.TimeoutError:
                    incomplete_tool_stream = saw_partial_tool_call and not pending_tool_calls
                    logger.warning("LLM stream timeout: %ss", settings.stream_timeout_seconds)

                    async for ev in _degrade_and_finish(
                        state=state,
                        ctx=ctx,
                        llm=llm,
                        user_message=user_message,
                        usage=_add_usage(turn_usage, usage),
                        full_text=_pending_recovery_text(),
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
                            allow_last_resort=not incomplete_tool_stream,
                            allow_partial_text_commit=not saw_partial_tool_call,
                            live_text_streaming=live_answer_streamed,
                            narration_streaming=speculative_unphased_streamed,
                            narration_segment_id=current_segment_id,
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
                        usage=_add_usage(turn_usage, usage),
                        full_text=_pending_recovery_text(),
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
                            failed_stopped_reason=(
                                _terminal_reason_from_error_type(classification.error_type)
                                if classification.fatal
                                else "api_error"
                            ),
                            error_message=_format_llm_error(f"LLM API request failed: {exc}"),
                            error_type=classification.error_type,
                            recoverable=not classification.fatal,
                            provider_error_type=classification.provider_error_type,
                            allow_last_resort=not classification.fatal and not (saw_partial_tool_call and not pending_tool_calls),
                            allow_partial_text_commit=not saw_partial_tool_call,
                            live_text_streaming=live_answer_streamed,
                            narration_streaming=speculative_unphased_streamed,
                            narration_segment_id=current_segment_id,
                        ),
                    ):
                        yield ev
                    break

                if rebuild_context_and_retry:
                    # Emergency compaction rewrote history; restart the outer loop so
                    # ctx.build/reconcile run against the compacted history instead of
                    # resending the stale oversized messages. This is a retry of the
                    # same logical iteration, not another unit of the user's budget.
                    state.iterations = max(0, state.iterations - 1)
                    continue

                if state.stopped_reason:
                    break

                if pending_tool_calls and not provider_raw_done:
                    finish_reason = finish_reason or "tool_calls_no_done"
                    provider_raw_done = {
                        "provider": type(llm).__name__,
                        "finish_reason": finish_reason,
                        "event_type": "synthetic.tool_calls_no_done",
                        "request_summary": _merge_prompt_cache_safe_request_summary(
                            {},
                            prompt_cache_safe_params,
                        ),
                        "output_items": _synthetic_provider_output_items(
                            full_text=full_text,
                            tool_calls=pending_tool_calls,
                            saw_final_answer_phase=saw_final_answer_phase,
                            provider_response_phase=provider_response_phase,
                        ),
                        "provider_timeline": [
                            {
                                "event": "stream.tool_calls_final",
                                "status": "synthetic_no_done",
                                "output_items_len": len(pending_tool_calls),
                            }
                        ],
                        "safety": {"redacted_prompt": True, "synthetic_trace": True, "usage_unavailable": True},
                        "loop_metrics": _loop_metrics_payload(
                            turn_started_at=turn_started_at,
                            state=state,
                            provider_call_count=provider_call_index + 1,
                            iteration_limit=max_iterations_limit,
                            iteration_hard_limit=iteration_budget.hard_limit,
                            tool_batch_count=tool_batch_count,
                            turn_start_tool_call_count=turn_start_tool_call_count,
                            pending_tool_call_count=len(pending_tool_calls),
                            dynamic_iteration_budget_enabled=iteration_budget.enabled,
                        ),
                    }
                    provider_call_index += 1
                    trace_id = f"{iteration_id}:provider:{provider_call_index}"
                    provider_raw_done["trace_id"] = trace_id
                    yield AgentEvent.inspector_update(
                        "provider",
                        trace_id,
                        _provider_trace_payload(
                            provider_raw=provider_raw_done,
                            usage=usage,
                            finish_reason=finish_reason,
                            iteration_id=iteration_id,
                            call_index=provider_call_index,
                            loop_metrics=provider_raw_done.get("loop_metrics"),
                        ),
                        display_scope="silent",
                        panel_hint="inspector",
                    )

                record_actual_usage = getattr(ctx, "record_actual_usage", None)
                if callable(record_actual_usage):
                    record_actual_usage(usage, provider_raw=provider_raw_done)
                turn_usage = _add_usage(turn_usage, usage)
                chain.record_usage(
                    input_tokens=usage.input_tokens or 0,
                    output_tokens=usage.output_tokens or 0,
                )

                # Scrub internal reasoning tags from model text (hermes pattern).
                # Some models/proxies leak <thinking> blocks into the text stream.
                if full_text and "<" in full_text:
                    full_text = _scrub_thinking_tags(full_text)
                if final_candidate_text and "<" in final_candidate_text:
                    final_candidate_text = _scrub_thinking_tags(final_candidate_text)
                if pending_unphased_text and "<" in pending_unphased_text:
                    pending_unphased_text = _scrub_thinking_tags(pending_unphased_text)
                if pending_process_text and "<" in pending_process_text:
                    pending_process_text = _scrub_thinking_tags(pending_process_text)
                if pending_unphased_visible_text and "<" in pending_unphased_visible_text:
                    pending_unphased_visible_text = _scrub_thinking_tags(pending_unphased_visible_text)
                if finalizable_stream_text and "<" in finalizable_stream_text:
                    finalizable_stream_text = _scrub_thinking_tags(finalizable_stream_text)

                # Restore the adapter output cap after an escalated retry's stream
                # has finished (whether or not it truncated again), so the raised
                # cap never leaks into later turns or multi-turn continuation.
                if escalation_override_active:
                    if escalation_saved_max_tokens is not None:
                        _set_adapter_max_output(llm, escalation_saved_max_tokens)
                    escalation_override_active = False
                    escalation_saved_max_tokens = None

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
                    # First truncation while still on a small default cap: raise
                    # the cap and retry the SAME request once before falling back
                    # to multi-turn continuation (cc ESCALATED_MAX_TOKENS).
                    if not state.max_output_escalated:
                        current_cap = _get_adapter_max_output(llm)
                        if (
                            current_cap is not None
                            and current_cap <= _MAX_OUTPUT_ESCALATION_THRESHOLD
                        ):
                            escalation_saved_max_tokens = current_cap
                            _set_adapter_max_output(llm, _MAX_OUTPUT_ESCALATED_TOKENS)
                            escalation_override_active = True
                            state.max_output_escalated = True
                            state.mark_transition(
                                "max_output_tokens_escalate",
                                escalated_to=_MAX_OUTPUT_ESCALATED_TOKENS,
                                finish_reason=finish_reason,
                            )
                            yield _agent_progress(
                                "模型输出被截断，正在提高单次输出上限后重试",
                                stage="status",
                                status="running",
                                id=f"agent:max-output-escalate:{state.iterations}",
                                phase="recover",
                                label="提高上限重试",
                                summary="模型输出被截断，正在提高单次输出上限后重试",
                                visibility="timeline",
                                step_id=f"escalate:{state.iterations}",
                                iteration_id=_iteration_id(state),
                            )
                            continue
                    if state.max_output_recovery_count < _MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
                        accepted_recovery_text = _accepted_answer_text()
                        if accepted_recovery_text:
                            state.max_output_recovered_text += accepted_recovery_text
                            ctx.append_assistant(accepted_recovery_text)
                        else:
                            ctx.append_assistant("[output truncated before visible text]")
                        next_user_message = _MAX_OUTPUT_RECOVERY_PROMPT
                        state.max_output_recovery_count += 1
                        state.mark_transition(
                            "max_output_tokens_recovery",
                            attempt=state.max_output_recovery_count,
                            finish_reason=finish_reason,
                        )
                        yield _agent_progress(
                            "模型输出被截断，正在续写",
                            stage="status",
                            status="running",
                            id=f"agent:max-output:{state.iterations}:{state.max_output_recovery_count}",
                            phase="recover",
                            label="续写",
                            summary="模型输出被截断，正在续写",
                            visibility="timeline",
                            count=state.max_output_recovery_count,
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
                    if saw_final_answer_phase:
                        process_event = _flush_pending_process_text()
                        if process_event is not None:
                            yield process_event
                    else:
                        if pending_process_text:
                            process_event = _flush_pending_process_text()
                            if process_event is not None:
                                yield process_event
                        _promote_unphased_text_to_final_candidate()

                    # Empty response — escalating nudge ladder, then a forced fallback
                    # / explicit error to avoid both an infinite loop and a SILENT
                    # zero-output "completed". This fires whether or not prior tool
                    # calls exist: a model that returns nothing on the very first turn
                    # (e.g. a proxy returning an empty 200 body) must surface a signal,
                    # never a blank "done" event.
                    if not final_candidate_text.strip():
                        _had_tool_results = bool(state.tool_calls)
                        if state.empty_reply_retries == 0:
                            state.empty_reply_retries = 1
                            state.mark_transition(
                                "empty_reply_nudge_1",
                                had_tool_results=_had_tool_results,
                            )
                            ctx.append_assistant("(empty)")
                            ctx.append_user(
                                "你执行了工具调用但返回了空回复。请根据上面的工具结果提供你的回答。"
                                if _had_tool_results else
                                "你返回了空回复。请直接回答用户的问题。"
                            )
                            full_text = ""
                            final_candidate_text = ""
                            finalizable_stream_text = ""
                            _clear_pending_text_buffers()
                            continue

                        # Second empty response — force fallback and break.
                        # (Claude Code pattern: don't waste more than 1 retry)
                        state.mark_transition("empty_reply_fallback")
                        fallback_text = _tool_result_fallback_reply(state, reason="模型多次返回空回复。")
                        if fallback_text:
                            full_text = fallback_text
                            final_candidate_text = fallback_text
                            state.reply = fallback_text
                            yield _fallback_recovery_progress_event(
                                state,
                                event_id=f"agent:empty-reply:fallback:{state.iterations}",
                                summary="Empty model reply; using completed tool results",
                            )
                            for event in _fallback_recovery_text_events(fallback_text):
                                yield event
                            # Mechanical tool-result recovery is NOT a model answer:
                            # keep it out of the "completed" terminal state so the turn
                            # reads as degraded/incomplete rather than a normal reply.
                            state.stopped_reason = "empty_reply"
                            yield _usage_done_event(
                                turn_usage,
                                status="partial",
                                reason="empty_reply_recovered_from_tools",
                            )
                        else:
                            failed_tool_reply = _failed_tool_result_fallback_reply(state)
                            if failed_tool_reply:
                                full_text = failed_tool_reply
                                state.reply = full_text
                                yield _fallback_recovery_progress_event(
                                    state,
                                    event_id=f"agent:empty-reply:failed-tools:{state.iterations}",
                                    summary="Empty model reply; surfacing failed tool details",
                                )
                                for event in _fallback_recovery_text_events(full_text):
                                    yield event
                                yield AgentEvent.error(
                                    message="Tool calls failed and the model did not produce a final reply.",
                                    recoverable=True,
                                    error_type="tool_error",
                                )
                                state.stopped_reason = "tool_error"
                                yield _usage_done_event(turn_usage, status="failed")
                                break
                            # No tool results to summarize (e.g. empty reply on the very
                            # first turn): emit an explicit error instead of a silent
                            # zero-output "done", so the user knows the turn failed.
                            yield AgentEvent.error(
                                message="模型多次返回空回复，未能生成答案。请重试或换一种提问方式。",
                                recoverable=True, error_type="empty_reply",
                            )
                            state.stopped_reason = "empty_reply"
                            yield _usage_done_event(turn_usage, status="failed")
                        break

                    candidate_text = (
                        f"{state.max_output_recovered_text}{final_candidate_text}"
                        if state.max_output_recovered_text
                        else final_candidate_text
                    )

                    # A tool-using turn is not complete when the model only promises
                    # another action (for example, "让我再查一下"). Ask once more for
                    # the actual result instead of sealing that draft as completed.
                    if state.tool_calls and _looks_like_future_action_only_answer(candidate_text):
                        state.total_retries += 1
                        if state.total_retries > state.max_total_retries:
                            fallback_reason = "模型只描述了下一步，没有给出最终结果。"
                            fallback_text = _tool_result_fallback_reply(state, reason=fallback_reason)
                            if candidate_text.strip() and live_answer_streamed:
                                yield AgentEvent.text_replace("")
                            if fallback_text:
                                full_text = fallback_text
                                state.reply = fallback_text
                                for event in _fallback_recovery_text_events(fallback_text):
                                    yield event
                                # Retries exhausted while the model only promised a
                                # next step: the mechanical tool-result recovery is a
                                # degraded stand-in, never a completed final answer.
                                state.stopped_reason = "max_retries"
                                yield _usage_done_event(
                                    turn_usage,
                                    status="partial",
                                    reason="future_action_recovered_from_tools",
                                )
                            else:
                                failed_tool_reply = _failed_tool_result_fallback_reply(state, reason=fallback_reason)
                                if failed_tool_reply:
                                    full_text = failed_tool_reply
                                    state.reply = failed_tool_reply
                                    for event in _fallback_recovery_text_events(failed_tool_reply):
                                        yield event
                                yield AgentEvent.error(
                                    message="工具调用失败，且模型没有生成完整的最终回答。",
                                    recoverable=True,
                                    error_type="tool_error" if failed_tool_reply else "incomplete_reply",
                                )
                                state.stopped_reason = "tool_error" if failed_tool_reply else "incomplete_reply"
                                yield _usage_done_event(turn_usage, status="failed")
                            break
                        if candidate_text.strip() and live_answer_streamed:
                            yield AgentEvent.text_replace("")
                        _append_assistant_history(
                            ctx,
                            candidate_text,
                            phase=provider_response_phase or "final_answer",
                            provider_items=provider_response_items,
                        )
                        ctx.append_user(
                            "[系统完整性校验] 你刚才只描述了下一步行动，没有回答用户。"
                            "请立即综合已有工具结果给出完整结论；若工具失败或超时，请如实说明。"
                            "不要再次只说接下来要做什么。"
                        )
                        state.mark_transition(
                            "future_action_final_retry",
                            retry_count=state.total_retries,
                        )
                        full_text = ""
                        final_candidate_text = ""
                        finalizable_stream_text = ""
                        live_answer_streamed = False
                        speculative_unphased_streamed = False
                        _clear_pending_text_buffers()
                        continue

                    if (
                        not state.web_grounding_retry_used
                        and _needs_fetched_web_grounding(user_message, state.tool_calls)
                    ):
                        state.web_grounding_retry_used = True
                        state.total_retries += 1
                        if candidate_text.strip() and live_answer_streamed:
                            yield AgentEvent.text_replace("")
                        _append_assistant_history(
                            ctx,
                            candidate_text,
                            phase=provider_response_phase or "final_answer",
                            provider_items=provider_response_items,
                        )
                        ctx.append_user(
                            "[系统事实核验] 这是实时/当前事实问题，但目前只有搜索摘要，没有成功打开的来源页面。"
                            "请先打开至少一个可靠来源再给出确定结论。若页面均无法打开，可以使用搜索摘要，"
                            "但必须明确说明证据仅来自搜索摘要，并避免补写摘要中没有的细节。"
                        )
                        state.mark_transition("web_grounding_fetch_retry")
                        full_text = ""
                        final_candidate_text = ""
                        finalizable_stream_text = ""
                        live_answer_streamed = False
                        speculative_unphased_streamed = False
                        _clear_pending_text_buffers()
                        continue

                    # Stop hook (user-configured hooks only)
                    hook_mgr = get_hook_manager()
                    if hook_mgr and _hook_manager_has_hooks(hook_mgr, HookEvent.STOP):
                        hook_result = await hook_mgr.run_stop(
                            user_message, candidate_text, tool_results=state.tool_calls
                        )
                        if hook_result.has_feedback and state.stop_hook_feedback_count < _STOP_HOOK_FEEDBACK_LIMIT:
                            state.stop_hook_feedback_count += 1
                            state.mark_transition(
                                "stop_hook_feedback",
                                attempt=state.stop_hook_feedback_count,
                            )
                            if candidate_text.strip():
                                if live_answer_streamed:
                                    yield AgentEvent.text_replace("")
                            _append_assistant_history(
                                ctx,
                                candidate_text,
                                phase=provider_response_phase or "final_answer",
                                provider_items=provider_response_items,
                            )
                            ctx.append_user(hook_result.feedback)
                            full_text = ""
                            final_candidate_text = ""
                            finalizable_stream_text = ""
                            live_answer_streamed = False
                            speculative_unphased_streamed = False
                            _clear_pending_text_buffers()
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
                                yield _usage_done_event(turn_usage, status="failed")
                                break
                            if candidate_text.strip() and live_answer_streamed:
                                yield AgentEvent.text_replace("")
                            _append_assistant_history(
                                ctx,
                                candidate_text,
                                phase=provider_response_phase or "final_answer",
                                provider_items=provider_response_items,
                            )
                            ctx.append_user(
                                "你最近的工具调用全部失败了。请修复根本原因并重试，"
                                "或者明确告诉用户哪里出了问题以及你无法完成什么。"
                                "不要假装任务成功了。"
                            )
                            state.mark_transition(
                                "failed_tool_heal_retry",
                                attempt=state.heal_attempts,
                                recent_failures=len(recent_results),
                            )
                            full_text = ""
                            final_candidate_text = ""
                            finalizable_stream_text = ""
                            live_answer_streamed = False
                            speculative_unphased_streamed = False
                            _clear_pending_text_buffers()
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
                        verify_span_id = f"verification:{run_record.run_id}:{state.iterations}"
                        verify_started_at = _epoch_ms()
                        await _emit_runtime_span(
                            "verification.started",
                            span_id=verify_span_id,
                            iteration_id=_iteration_id(state),
                            phase="verification",
                            status="running",
                            label="verify",
                            summary=f"Running {settings.verify_command}",
                            started_at=verify_started_at,
                            data={"command": settings.verify_command},
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
                        verify_completed_at = _epoch_ms()
                        await _emit_runtime_span(
                            "verification.result",
                            span_id=verify_span_id,
                            iteration_id=_iteration_id(state),
                            phase="verification",
                            status="completed" if verify_ok else "failed",
                            label="verify",
                            summary="Verification passed" if verify_ok else "Verification failed",
                            started_at=verify_started_at,
                            ended_at=verify_completed_at,
                            duration_ms=verify_completed_at - verify_started_at,
                            requires_attention=not verify_ok,
                            data={
                                "command": settings.verify_command,
                                "passed": verify_ok,
                                "output_preview": verify_output[:2000],
                            },
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
                            state.mark_transition(
                                "verify_failed_retry",
                                attempt=state.verify_attempts,
                                command=settings.verify_command,
                            )
                            if candidate_text.strip() and live_answer_streamed:
                                yield AgentEvent.text_replace("")
                            _append_assistant_history(
                                ctx,
                                candidate_text,
                                phase=provider_response_phase or "final_answer",
                                provider_items=provider_response_items,
                            )
                            ctx.append_user(
                                f"你的修改未通过验证命令 `{settings.verify_command}`（非零退出码）。"
                                "请根据下面的输出修复问题后再给最终回答；如果无法修复，请如实说明原因，"
                                "不要假装任务成功了。\n\n"
                                f"验证输出：\n{verify_output}"
                            )
                            full_text = ""
                            final_candidate_text = ""
                            finalizable_stream_text = ""
                            live_answer_streamed = False
                            speculative_unphased_streamed = False
                            _clear_pending_text_buffers()
                            continue

                    coordinator_feedback = coordinator_finalization_feedback(
                        runtime=runtime,
                        parent_run_id=run_record.run_id,
                        conversation_id=run_record.conversation_id,
                        state=state,
                        candidate_text=candidate_text,
                    )
                    if coordinator_feedback:
                        if candidate_text.strip() and live_answer_streamed:
                            yield AgentEvent.text_replace("")
                        ctx.append_assistant(candidate_text)
                        _reset_history_after_draft_retry(ctx, candidate_text)
                        ctx.append_user(f"[系统完整性校验] {coordinator_feedback}")
                        state.mark_transition(
                            "coordinator_finalization_feedback",
                        )
                        full_text = ""
                        final_candidate_text = ""
                        finalizable_stream_text = ""
                        live_answer_streamed = False
                        speculative_unphased_streamed = False
                        _clear_pending_text_buffers()
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
                        final_metadata = {
                            "segmentId": current_segment_id,
                            "sealReason": "final_answer",
                            "finishReason": finish_reason,
                            "providerRaw": {**provider_raw_final_text, **provider_raw_done},
                        }
                        if (
                            speculative_unphased_streamed
                            and not saw_final_answer_phase
                            and final_text == _scrub_thinking_tags(unphased_narration_stream_text)
                        ):
                            yield AgentEvent.text_chunk(
                                "",
                                source="model_narration",
                                visibility="final",
                                phase="final",
                                finalize=True,
                                metadata={
                                    **final_metadata,
                                    "promoteAllUnsealedNarration": True,
                                },
                            )
                        elif live_answer_streamed and finalizable_stream_text and final_text == finalizable_stream_text:
                            # Compatibility path for legacy live-answer drafts:
                            # seal the already-visible answer without re-emitting
                            # the same markdown block.
                            yield AgentEvent.text_chunk(
                                "",
                                source="model_final",
                                visibility="final",
                                phase="final_answer",
                                finalize=True,
                                metadata=final_metadata,
                            )
                        else:
                            # Normal phase-first path: emit the accepted final
                            # answer once. If a legacy draft somehow exists, clear
                            # it first before sending the final text.
                            if speculative_unphased_streamed:
                                for _ev in _seal_unphased_narration("final_answer"):
                                    yield _ev
                            if live_answer_streamed:
                                yield AgentEvent.text_replace("")
                            yield AgentEvent.text_chunk(
                                final_text,
                                source="model_final",
                                visibility="final",
                                phase="final_answer",
                                metadata=final_metadata,
                            )
                        _append_assistant_history(
                            ctx,
                            # Truncation recovery already appended the earlier partial
                            # blocks to history (one per recovery iteration). Append
                            # only the unrecovered tail here so the recovered text
                            # isn't duplicated in context. state.reply keeps the full
                            # concatenation for display.
                            final_text[len(state.max_output_recovered_text):] if state.max_output_recovered_text else final_text,
                            phase=provider_response_phase or "final_answer",
                            provider_items=provider_response_items,
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
                    _set_terminal_reason(state, "completed")
                    completed_event = _complete_run_record(
                        _terminal_run_status(state.stopped_reason),
                        summary=_terminal_run_summary(state.stopped_reason),
                        error=_terminal_run_error(state.stopped_reason),
                    )
                    if completed_event is not None:
                        yield completed_event
                    yield AgentEvent.done(
                        input_tokens=turn_usage.input_tokens,
                        output_tokens=turn_usage.output_tokens,
                        cache_creation_input_tokens=turn_usage.cache_creation_input_tokens,
                        cache_read_input_tokens=turn_usage.cache_read_input_tokens,
                        reasoning_output_tokens=turn_usage.reasoning_output_tokens,
                        provider_raw=provider_raw_done,
                    )
                    break

                # A productive turn (the model emitted tool calls) breaks any run of
                # empty replies, so reset the consecutive-empty counter. Without this
                # the counter accumulates *total* empty replies across the whole turn
                # and the third non-consecutive empty reply trips the forced fallback
                # prematurely in long agentic sessions.
                process_event = _flush_pending_process_text(
                    pending_tool_calls,
                    source=process_text_source,
                )
                if process_event is not None:
                    yield process_event
                state.empty_reply_retries = 0

                # Execute tool calls. Only schema-safe calls are written into LLM
                # history; malformed calls still produce UI/state events and
                # runtime guidance, but are not sent back to strict gateways.
                prepared_tool_repairs = prepare_tool_call_sequence(
                    state,
                    pending_tool_calls,
                    tool_registry,
                    tool_ctx,
                )
                pending_tool_calls = [result.tool_call for result in prepared_tool_repairs]
                for _tc in pending_tool_calls:
                    chain.record_tool_call()
                tool_batch_count += 1
                history_tool_calls = [
                    tc for tc in pending_tool_calls
                    if tool_call_is_safe_for_model_history(tc, tool_registry)
                ]
                if history_tool_calls:
                    # Preserve model's reasoning text alongside tool_calls
                    # (Anthropic/OpenAI pattern: assistant message = text + tool_use)
                    _append_assistant_tool_calls_history(
                        ctx,
                        history_tool_calls,
                        content=full_text,
                        phase=provider_response_phase or "commentary",
                        provider_items=provider_response_items,
                    )
                elif full_text.strip():
                    # Unsafe tool calls still need an assistant message so the
                    # history is valid (no orphaned tool_result messages).
                    _append_assistant_history(
                        ctx,
                        full_text,
                        phase=provider_response_phase or "commentary",
                        provider_items=provider_response_items,
                    )

                _tool_batch_iter = _execute_tool_batch(
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
                    prepared_repair_results=prepared_tool_repairs,
                )
                try:
                    async for ev in _tool_batch_iter:
                        yield ev
                finally:
                    # Close the tool-batch generator on interrupt/exit so its `finally`
                    # runs and cancels in-flight tool tasks. Without this, an interrupt
                    # while processing a yielded event leaves the generator suspended at
                    # its yield — its pending tool tasks (e.g. file writes) never get
                    # cancelled and keep running as orphans.
                    await _tool_batch_iter.aclose()
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

                if iteration_budget.maybe_extend(state):
                    max_iterations_limit = iteration_budget.current_limit
                    state.max_iterations = max(state.max_iterations, max_iterations_limit)
                    logger.debug(
                        "Extended iteration budget to %d/%d after productive tool progress",
                        max_iterations_limit,
                        iteration_budget.hard_limit,
                    )
                    yield _agent_progress(
                        f"Extended iteration budget to {max_iterations_limit}",
                        stage="status",
                        status="running",
                        id=f"agent:iter-budget:{state.iterations}",
                        phase="iteration",
                        label="Iteration budget",
                        visibility="debug",
                        display_scope="silent",
                        panel_hint="inspector",
                        count=max_iterations_limit,
                        iteration_id=_iteration_id(state),
                    )

                # Post-tool-batch max-turn boundary (cc: after tools, before next model).
                if state.iterations >= max_iterations_limit:
                    logger.warning(
                        "Max iterations reached after tool batch: %d",
                        max_iterations_limit,
                    )
                    reconcile = getattr(ctx, "reconcile_dangling_tool_calls", None)
                    if callable(reconcile):
                        try:
                            reconcile()
                        except Exception as reconcile_exc:
                            logger.debug(
                                "Post-tool max-iter reconcile failed: %s",
                                reconcile_exc,
                            )
                    fallback_text = ""
                    if state.tool_calls:
                        fallback_text = _tool_result_fallback_reply(
                            state,
                            reason=f"已达到最大迭代次数限制（{max_iterations_limit}次）。",
                        )
                        if fallback_text:
                            yield _fallback_recovery_progress_event(
                                state,
                                event_id=f"agent:max-iterations:post-tools:{state.iterations}",
                                summary="Max iterations reached after tools; using completed tool results",
                            )
                            for event in _fallback_recovery_text_events(fallback_text):
                                yield event
                            ctx.append_assistant(fallback_text)
                            state.reply = fallback_text
                    state.stopped_reason = "max_iterations"
                    if fallback_text:
                        completed_event = _complete_run_record(
                            "partial",
                            summary="Max iterations reached after tools; retained completed tool results",
                        )
                    else:
                        yield AgentEvent.error(
                            message=f"已达到最大迭代次数限制（{max_iterations_limit}次）。",
                            recoverable=True,
                            error_type="budget",
                        )
                        await _run_stop_failure_hook(
                            "max_iterations",
                            error_details=f"Max iterations reached after tools: {max_iterations_limit}",
                            last_assistant_message=state.reply,
                        )
                        completed_event = _complete_run_record(
                            "failed",
                            summary="Max iterations reached after tools",
                            error="max_iterations",
                        )
                    if completed_event is not None:
                        yield completed_event
                    break

                state.mark_transition(
                    "next_turn",
                    tool_call_count=tool_call_count,
                    total_tool_calls=len(state.tool_calls),
                )

        except asyncio.CancelledError as exc:
            _set_terminal_reason(state, "interrupted")
            # Only persist provider-confirmed final text. Commentary and
            # unphased narration belong to the timeline and must not become a
            # normal assistant message when the user cancels mid-turn.
            cancelled_final_text = (
                _scrub_thinking_tags(final_candidate_text)
                if saw_final_answer_phase and final_candidate_text.strip()
                else ""
            )
            if cancelled_final_text:
                ctx.append_assistant(cancelled_final_text)
            # Close unresolved tool_use/tool_result pairs before terminal checkpoint
            # flush so resume history remains provider-legal (cc ensureToolResultPairing).
            reconcile = getattr(ctx, "reconcile_dangling_tool_calls", None)
            if callable(reconcile):
                try:
                    reconcile()
                except Exception as reconcile_exc:
                    logger.debug("Cancel-path tool trajectory reconcile failed: %s", reconcile_exc)
            # Inject an explicit interruption marker into history (cc
            # createUserInterruptionMessage) so a later resume carries a semantic
            # clue that the previous turn was cut off mid-flight, rather than
            # relying on stopped_reason + checkpoint alone.
            append_user = getattr(ctx, "append_user", None)
            if callable(append_user):
                try:
                    append_user("[Request interrupted by user]")
                except Exception as interrupt_exc:
                    logger.debug("Cancel-path interruption marker append failed: %s", interrupt_exc)
            completed_event = _complete_run_record(
                _terminal_run_status(state.stopped_reason),
                summary=_terminal_run_summary(state.stopped_reason),
                error=_terminal_run_error(state.stopped_reason),
            )
            if completed_event is not None:
                yield completed_event
            # Defer propagation until checkpoint persistence below has flushed the
            # current messages/tool evidence. This makes deadline resume real.
            deferred_cancel = exc

        # Ensure the run record is always marked complete. Several termination paths
        # (empty_reply, max_retries, budget_exceeded, incomplete_tool_stream,
        # tool_error, and the _degrade_and_finish ladder) break out of the loop
        # without calling _complete_run_record, which would leave the run stuck in
        # "running" and never emit agent.run.completed. _complete_run_record is
        # idempotent (guarded by run_completed_emitted), so this is a no-op when an
        # inner path already completed it (max_iterations, stagnation, final answer).
        if not run_completed_emitted:
            completed_event = _complete_run_record(
                _terminal_run_status(state.stopped_reason),
                summary=_terminal_run_summary(state.stopped_reason),
                error=_terminal_run_error(state.stopped_reason),
            )
            if completed_event is not None:
                yield completed_event

        # Phase 3: Post-session checkpoint and memory
        # Save checkpoint if the task didn't complete naturally (timeout/error/interrupt)
        # so it can be resumed later with /resume.
        if _terminal_should_save_checkpoint(state.stopped_reason) and session_id:
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
        elif _terminal_should_clear_checkpoints(state.stopped_reason) and session_id:
            # Clear checkpoints on successful completion
            try:
                clear_checkpoints(session_id)
            except Exception as exc:
                logger.debug("Checkpoint clear failed: %s", exc)
        if deferred_cancel is not None:
            raise deferred_cancel
    finally:
        LLMAdapter.unbind_turn_usage(_turn_usage_token)
        CostTracker.unbind_session(_cost_session_token)
