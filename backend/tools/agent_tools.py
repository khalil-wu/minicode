"""Agent helper tools: user clarification, artifacts, and subagents."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.prompt_cache import prompt_cache_fork_diagnostic
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import default_runtime
from backend.agent.coordinator import _delegation_text_similar
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.state import AgentState
from backend.agents.loader import discover_agents, get_custom_agent
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.tools.agent_artifact_tools import ReadArtifactTool
from backend.tools.agent_user_tools import AskUserTool, BriefTool
from backend.tools.subagent_control_tools import TaskStatusTool, TaskStopTool
from backend.tools.subagent_catalog import (
    BUILTIN_AGENT_TYPES,
    available_agent_types,
    normalize_agent_type,
)
from backend.tools.subagent_context import (
    build_subagent_permission_context,
    build_subagent_prompt,
    sanitize_subagent_runtime_metadata,
)
from backend.tools.subagent_runtime import metadata_from_context, runtime_from_context
from backend.tools.subagent_result import compact_subagent_result

logger = logging.getLogger(__name__)

_INTERNAL_TOOL_REFERENCE_RE = re.compile(r"\bcall_[a-z0-9_-]{8,}\b", re.IGNORECASE)
_ELAPSED_ONLY_RE = re.compile(r"^\d+(?:\.\d+)?s elapsed$", re.IGNORECASE)
_SUBAGENT_CAPACITY_MESSAGE = "Maximum concurrent subagents reached. Wait for a running task to finish."


def _auto_background_ms() -> int:
    """Wall-clock deadline after which a synchronous delegation hands off to the
    background instead of blocking the parent forever (cc AgentTool auto-background,
    default 120s). Set MINICODE_AUTO_BACKGROUND_TASKS_MS=0 to disable.
    """
    raw = os.environ.get("MINICODE_AUTO_BACKGROUND_TASKS_MS", "120000")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 120_000


def _subagent_iteration_budget(*, prompt: str, agent_type: str, configured: int) -> int:
    """Use the configured worker budget without a hidden task-type ceiling."""
    return max(1, int(configured or 30))


def _custom_agent_deny_rules(agent_type: str, tool_registry: Any | None) -> list[str]:
    """Enforce a custom agent's tool restrictions as deny rules.

    A custom AgentDefinition may declare a ``tools`` whitelist and/or
    ``disallowed_tools``. Without this they were loaded and saved but never
    applied — so a user who set ``disallowed_tools: [write_file]`` would still
    see the subagent write. Returns [] for built-in agent types or unrestricted
    custom agents.
    """
    if agent_type in BUILTIN_AGENT_TYPES:
        return []
    try:
        custom = get_custom_agent(agent_type)
    except Exception:
        return []
    if custom is None:
        return []
    deny: list[str] = []
    deny.extend(str(t).strip() for t in (custom.disallowed_tools or []) if str(t).strip())
    whitelist = [str(t).strip() for t in (custom.tools or []) if str(t).strip()]
    if whitelist:
        allowed = set(whitelist)
        try:
            all_names = tool_registry.list_tools() if tool_registry is not None else []
        except Exception:
            all_names = []
        deny.extend(t for t in all_names if t not in allowed)
    seen: set[str] = set()
    unique: list[str] = []
    for name in deny:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _sanitize_timeout_partial_summary(summary: str) -> str:
    """A runtime deadline must never be presented as a user interruption."""
    lines = [
        line
        for line in str(summary or "").splitlines()
        if "the user interrupted the current run" not in line.lower()
    ]
    return "\n".join(lines).strip()


def _user_visible_progress_text(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or _INTERNAL_TOOL_REFERENCE_RE.search(text)
        or _ELAPSED_ONLY_RE.fullmatch(text)
        or text.lower().startswith("tool started:")
    ):
        return ""
    return text


def _subagent_display_summary(value: Any) -> str:
    """Extract one plain, stable summary line from a child result."""
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or re.match(r"^#{1,6}\s+", line):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"(?:\*\*|__|`)", "", line).strip()
        if not line or re.match(r"^Subagent\s+subagent-[\w-]+.*completed", line, re.IGNORECASE):
            continue
        return line[:500]
    return ""


def _scope_parallel_task_prompt(task: dict[str, Any], *, scope: str) -> str:
    prompt = str(task.get("prompt") or "").strip()
    assigned_scope = str(scope).strip()
    if not assigned_scope:
        return prompt
    return (
        "[Assigned task scope]\n"
        f"Your assigned objective is exactly: {assigned_scope}\n"
        "Work only on this objective. Do not investigate, execute, or summarize targets "
        "assigned to sibling subagents, even if the original prompt mentions them.\n\n"
        f"{prompt}"
    )


def _hook_visible_task_prompt(prompt: str) -> str:
    """Keep internal assignment guards out of user/plugin-facing hook payloads."""
    text = str(prompt or "").strip()
    if text.startswith("[Assigned task scope]\n") and "\n\n" in text:
        return text.split("\n\n", 1)[1].strip()
    return text


_GENERIC_SCOPE_RE = re.compile(
    r"^(?:agent|subagent|worker|task|subtask|researcher|智能体|子智能体|子\s*agent|任务|调研员)"
    r"\s*[-_#：:]?\s*[一二三四五六七八九十\d]+$",
    re.IGNORECASE,
)


def _prompt_scope_summary(prompt: str) -> str:
    """Compact user-facing scope label derived from the task prompt."""
    text = " ".join(str(prompt or "").split())
    return text if len(text) <= 120 else f"{text[:80]} … {text[-39:]}"


def _exclusive_parallel_task_scopes(tasks: list[dict[str, Any]]) -> list[str]:
    """Select a unique user-facing scope for every parallel worker.

    Tasks are distinct when description+prompt differ; only true duplicate
    delegations (same description and same prompt) are rejected. A generic
    label such as "Agent 1" is fine as long as the prompt itself names a
    distinct scope — the prompt summary then serves as the scope label.
    """
    descriptions = [str(task.get("description") or "").strip() for task in tasks]
    objectives = [str(task.get("objective") or "").strip() for task in tasks]
    prompts = [str(task.get("prompt") or "").strip() for task in tasks]

    keys = [
        (description.casefold(), prompt.casefold())
        for description, prompt in zip(descriptions, prompts, strict=True)
    ]
    if len(set(keys)) != len(keys):
        return []
    # Enforce write_scope exclusivity between sibling workers. Two parallel
    # workers whose write_scope shares a path would race on that file
    # (last-writer-wins) with no mutual exclusion, so reject the batch — the
    # caller surfaces this as "non-overlapping assignment" guidance.
    seen_write_paths: set[str] = set()
    for task in tasks:
        raw_scope = task.get("write_scope")
        paths = raw_scope if isinstance(raw_scope, list) else []
        for path in paths:
            norm = str(path or "").strip().casefold()
            if not norm:
                continue
            if norm in seen_write_paths:
                return []
            seen_write_paths.add(norm)

    def _counts(values: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            if value:
                counts[value.casefold()] = counts.get(value.casefold(), 0) + 1
        return counts

    description_counts = _counts(descriptions)
    objective_counts = _counts(objectives)
    scopes: list[str] = []
    for description, objective, prompt in zip(descriptions, objectives, prompts, strict=True):
        scope = next(
            (
                label
                for label, counts in ((description, description_counts), (objective, objective_counts))
                if label and counts[label.casefold()] == 1 and not _GENERIC_SCOPE_RE.fullmatch(label)
            ),
            "",
        ) or _prompt_scope_summary(prompt)
        if not scope:
            return []
        scopes.append(scope)
    if len({scope.casefold() for scope in scopes}) != len(scopes):
        return []
    for index, scope in enumerate(scopes):
        if any(_delegation_text_similar(scope, other) for other in scopes[:index]):
            return []
    return scopes


_INCOMPLETE_SUBAGENT_RESULT_RE = re.compile(
    r"^(?:(?:now\s+)?(?:let me|i(?:'ll| will| need to| am going to))(?:\s|,|:)|"
    r"(?:接下来(?:我)?|现在(?:我)?|让我|我(?:将|会|要|需要|准备|打算)|下一步))",
    re.IGNORECASE,
)


def _is_incomplete_subagent_summary(summary: str) -> bool:
    text = str(summary or "").strip()
    if not text:
        return True
    substantive = [line.strip() for line in text.splitlines() if line.strip()]
    if not substantive:
        return True
    return all(_INCOMPLETE_SUBAGENT_RESULT_RE.match(line) for line in substantive[:3])


def _strip_incomplete_subagent_preamble(summary: str) -> str:
    """Drop a model's unfinished narration before its structured result.

    Some providers stream a sentence such as ``Now I have enough data...`` and
    then start the final ``## Result`` section in the same text message. Keep
    the structured result instead of exposing the unfinished sentence as part
    of the result heading (for example ``具## Result``).
    """
    text = str(summary or "").strip()
    if not text:
        return ""
    match = re.search(r"(?im)##\s*result\b", text)
    if match:
        prefix = text[:match.start()].strip()
        preamble_clause = re.search(
            r"(?i)(?:^|[，,。.!！?？；;])\s*(?:让我|接下来(?:我)?|现在(?:我)?|"
            r"now\s+let\s+me|i(?:'ll| will| need to| am going to))",
            prefix,
        )
        if prefix and (_is_incomplete_subagent_summary(prefix) or preamble_clause):
            return text[match.start():].lstrip()
    return text


def _existing_similar_subagent(runtime: Any, *, parent_run_id: str, label: str) -> str:
    if runtime is None or not parent_run_id or not label:
        return ""
    try:
        snapshot = runtime.list_runs(include_subagents=True)
    except Exception:
        return ""
    for item in snapshot.get("subagents", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("parent_run_id") or "") != parent_run_id:
            continue
        if str(item.get("status") or "") not in {"pending", "running", "blocked", "completed", "partial"}:
            continue
        existing = str(item.get("objective") or item.get("prompt_summary") or "").strip()
        if existing and _delegation_text_similar(label, existing):
            return existing
    return ""


def _available_agent_types() -> list[str]:
    """Return built-in plus discovered custom subagent types for model schema."""
    return available_agent_types(discover_agents)


def _resolve_subagent_llm(
    inherited_llm: Any,
    *,
    agent_type: str,
    model_override: str = "",
    effort_override: str = "",
) -> Any:
    """Build a per-subagent LLM adapter when a model/effort override is requested.

    Precedence: explicit task-call override > custom agent definition. A model of
    ``"inherit"`` (cc convention) or empty string keeps the session model. When a
    real override is present we construct a fresh adapter for the *current
    provider* via the existing adapter factory (``build_provider_adapter``), so
    the user-declared ``model:`` in an agent definition actually takes effect
    instead of being silently ignored. On any failure we fall back to the
    inherited adapter and log a warning (never silent).
    """
    custom = get_custom_agent(agent_type) if agent_type else None

    model = str(model_override or "").strip()
    if not model and custom is not None:
        model = str(getattr(custom, "model", "") or "").strip()
    if model.lower() == "inherit":
        model = ""

    effort = str(effort_override or "").strip()
    if not effort and custom is not None:
        effort = str(getattr(custom, "effort", "") or "").strip()

    if not model and not effort:
        return inherited_llm

    try:
        from backend.config import get_llm_provider
        from backend.services.llm_adapter_factory import build_provider_adapter

        adapter = build_provider_adapter(
            get_llm_provider(), model_override=model or None
        )
        if adapter is None:
            logger.warning(
                "Subagent %s requested model/effort override (model=%r effort=%r) "
                "but the provider adapter could not be built; inheriting session LLM.",
                agent_type, model, effort,
            )
            return inherited_llm

        # Reasoning effort only applies to OpenAI-compatible settings-based
        # adapters. Rebuild the fresh adapter's own settings (never mutate the
        # inherited/shared adapter) so the override is isolated to this subagent.
        if effort:
            settings = getattr(adapter, "_settings", None)
            if settings is not None and hasattr(settings, "reasoning_effort"):
                try:
                    from backend.llm.openai_adapter import OpenAIAdapter

                    adapter = OpenAIAdapter(settings=replace(settings, reasoning_effort=effort))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Subagent %s effort override %r failed to apply: %s",
                        agent_type, effort, exc,
                    )
            else:
                logger.warning(
                    "Subagent %s effort override %r ignored: current provider "
                    "adapter does not support reasoning effort.",
                    agent_type, effort,
                )
        return adapter
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Subagent %s LLM override (model=%r effort=%r) failed: %s; "
            "inheriting session LLM.",
            agent_type, model, effort, exc,
        )
        return inherited_llm


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _bool_field(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return bool(raw)


def _subagent_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    blocked_by = _string_list(raw.get("blocked_by"))
    required_for_final = _bool_field(raw.get("required_for_final"), True)
    waiting_on = str(raw.get("waiting_on") or "").strip()
    if not waiting_on and blocked_by:
        waiting_on = "dependencies"
    # cancel/detach are independent of required_for_final. Background + not
    # required_for_final detaches by default; explicit flags always win.
    has_detach = "detach_from_parent" in raw
    has_cancel = "cancel_with_parent" in raw
    if has_detach:
        detach_from_parent = _bool_field(raw.get("detach_from_parent"), False)
        cancel_with_parent = (
            _bool_field(raw.get("cancel_with_parent"), not detach_from_parent)
            if has_cancel
            else (not detach_from_parent)
        )
    elif has_cancel:
        cancel_with_parent = _bool_field(raw.get("cancel_with_parent"), True)
        detach_from_parent = not cancel_with_parent
    else:
        cancel_with_parent = True
        detach_from_parent = False
    if detach_from_parent:
        cancel_with_parent = False
    return {
        "workflow_id": str(raw.get("workflow_id") or "").strip(),
        "workflow_name": str(raw.get("workflow_name") or "").strip(),
        "workflow_mode": str(raw.get("workflow_mode") or "").strip(),
        "node_id": str(raw.get("node_id") or "").strip(),
        "task_id": str(raw.get("task_id") or "").strip(),
        "objective": str(raw.get("objective") or "").strip(),
        "depends_on": _string_list(raw.get("depends_on")),
        "blocked_by": blocked_by,
        "required_for_final": required_for_final,
        "blocks_final_reply": _bool_field(raw.get("blocks_final_reply"), required_for_final),
        "cancel_with_parent": cancel_with_parent,
        "detach_from_parent": detach_from_parent,
        "read_only": _bool_field(raw.get("read_only"), False),
        "write_scope": _string_list(raw.get("write_scope")),
        "current_activity": str(raw.get("current_activity") or "").strip(),
        "waiting_on": waiting_on,
        # LLM overrides (cc-compatible): a caller may pin a specific model /
        # reasoning effort for this subagent instead of inheriting the session.
        "model": str(raw.get("model") or "").strip(),
        "effort": str(raw.get("effort") or "").strip(),
        # cc AgentTool isolation: "worktree" runs the subagent inside a
        # temporary git worktree instead of the shared workspace.
        "isolation": str(raw.get("isolation") or "").strip().lower(),
    }


def _nonempty_subagent_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    metadata = _subagent_metadata(raw)
    return {
        key: value
        for key, value in metadata.items()
        if value not in ("", [], None)
    }


def _subagent_prompt_cache_fork_diagnostic(
    parent_summary: Any,
    child_summary: Any,
) -> dict[str, Any]:
    return prompt_cache_fork_diagnostic(parent_summary, child_summary)


async def _finalize_workflow_task(
    *,
    context: ToolExecutionContext | None,
    workflow_metadata: dict[str, Any],
    subagent_id: str,
    result_text: str,
    status: str,
) -> None:
    task_id = str(workflow_metadata.get("task_id") or "").strip()
    if not task_id:
        return
    runtime = runtime_from_context(context) or default_runtime()
    task = runtime.get_swarm_task(task_id)
    if task is None or task.status in {"completed", "cancelled"}:
        return
    # Map the subagent's terminal state to the shared workflow node status.
    # Partial output is retained for inspection but must not satisfy dependent
    # workflow nodes. The coordinator can explicitly reroute or approve it.
    if status == "completed":
        task_status = "completed"
    elif status == "cancelled":
        task_status = "cancelled"
    else:
        task_status = "blocked"
    try:
        already_attached = any(
            str(getattr(output, "author_id", "") or "") == subagent_id
            for output in getattr(task, "outputs", []) or []
        )
        if already_attached:
            from backend.tools.swarm_tools import TaskUpdateTool

            await TaskUpdateTool().execute(
                {"task_id": task_id, "status": task_status},
                context=context,
            )
        else:
            from backend.tools.swarm_tools import TaskOutputTool

            handoff_text, _ = compact_subagent_result(result_text, max_chars=6_000)
            await TaskOutputTool().execute(
                {
                    "task_id": task_id,
                    "content": handoff_text,
                    "author": subagent_id,
                    "status": task_status,
                },
                context=context,
            )
    except Exception as exc:
        logger.warning("workflow task finalization failed for %s: %s", task_id, exc)


async def _run_subagent_start_hook(subagent_id: str, agent_type: str) -> None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return
    try:
        await hook_mgr.run_subagent_start(
            subagent_id=subagent_id,
            agent_type=agent_type,
        )
    except Exception as exc:
        logger.warning("subagent_start hook failed: %s", exc)


async def _run_subagent_stop_hook(
    subagent_id: str,
    status: str,
    summary: str = "",
    *,
    agent_type: str = "",
) -> None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return
    try:
        await hook_mgr.run_subagent_stop(
            subagent_id=subagent_id,
            agent_type=agent_type,
            status=status,
            summary=summary,
        )
    except Exception as exc:
        logger.warning("subagent_stop hook failed: %s", exc)


async def _run_task_created_hook(
    *,
    task_id: str,
    subject: str,
    description: str,
    teammate_name: str = "",
    team_name: str = "",
) -> None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return
    try:
        await hook_mgr.run_task_created(
            task_id=task_id,
            subject=subject,
            description=description,
            teammate_name=teammate_name,
            team_name=team_name,
        )
    except Exception as exc:
        logger.warning("task_created hook failed: %s", exc)


async def _run_task_completed_hook(
    *,
    task_id: str,
    subject: str,
    description: str,
    teammate_name: str = "",
    team_name: str = "",
) -> None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return
    try:
        await hook_mgr.run_task_completed(
            task_id=task_id,
            subject=subject,
            description=description,
            teammate_name=teammate_name,
            team_name=team_name,
        )
    except Exception as exc:
        logger.warning("task_completed hook failed: %s", exc)


async def _run_teammate_idle_hook(
    *,
    teammate_name: str = "",
    team_name: str = "",
) -> None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return
    try:
        await hook_mgr.run_teammate_idle(
            teammate_name=teammate_name,
            team_name=team_name,
        )
    except Exception as exc:
        logger.warning("teammate_idle hook failed: %s", exc)


class TaskTool(BaseTool):
    """Delegate a bounded task to an isolated subagent."""

    name = "task"
    result_kind = "subagent"
    activity_kind = "genericTool"
    display_label = "Start subagent"
    display_scope = "agents"
    panel_hint = "subagents"
    description = (
        "Delegate a sub-task to an independent agent. The sub-agent has its own context and tool access. "
        "Use only for broad, complex, independent work. Read or search a known file directly instead. "
        "Never duplicate work already delegated, and do not repeat the same exploration in the parent thread. "
        "Supports parallel sub-tasks via the parallel_tasks parameter (up to 5 concurrent). "
        "Results include the sub-agent's findings and a summary of tools used."
    )
    permission = PermissionLevel.AUTO

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="agent.delegate",
            toolset="agent",
            exposure="core",
            required_args=("description", "prompt"),
            arg_roles={
                "description": "generated_content",
                "prompt": "generated_content",
                "agent_type": "control",
            },
            repair_policy={
                "description": "needs_model_generation",
                "prompt": "needs_model_generation",
                "agent_type": "runtime_control",
            },
            empty_args_policy="repair_or_block",
        )

    def model_description(self) -> str:
        return (
            "Delegate one bounded task to an independent subagent. "
            "Do not use it for a specific file or a small directed search. Do not launch overlapping work, "
            "and do not redo delegated exploration in the parent thread. "
            "Use task_status to list or wait, send_message to steer it, and task_stop to cancel it."
        )

    def model_schema(self) -> ToolSchema:
        agent_types = _available_agent_types()
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Short task label shown in the UI.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Complete self-contained instructions for the subagent.",
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": agent_types,
                        "description": "Optional specialized agent type.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Optional no-progress deadline from 300 to 1800 seconds. Omit for no subagent-level deadline.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Return immediately with a subagent id instead of waiting.",
                    },
                    "isolation": {
                        "type": "string",
                        "enum": ["worktree"],
                        "description": (
                            "Isolation mode. 'worktree' creates a temporary git worktree so the "
                            "subagent works on an isolated copy of the repo."
                        ),
                    },
                    "required_for_final": {
                        "type": "boolean",
                        "description": "Whether the parent must collect this result before finalizing.",
                    },
                    "cancel_with_parent": {
                        "type": "boolean",
                        "description": "Whether parent cancel should also cancel this subagent. Independent of required_for_final.",
                    },
                    "detach_from_parent": {
                        "type": "boolean",
                        "description": "If true, this subagent keeps running after parent cancel (cc async unlinked AbortController).",
                    },
                    "read_only": {
                        "type": "boolean",
                        "description": "Whether the delegated work must avoid writes.",
                    },
                    "write_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional file or directory scopes the subagent may modify.",
                    },
                    "resume_subagent_id": {
                        "type": "string",
                        "description": "Resume a partial/deadline-exceeded subagent from its retained checkpoint using the same subagent id.",
                    },
                },
                "required": ["description", "prompt"],
            },
        )

    def __init__(
        self,
        *,
        llm_provider: Any | None = None,
        tool_registry_provider: Any | None = None,
        artifact_store: ArtifactStore,
        permission_checker_provider: Any | None = None,
        agent_settings_provider: Any | None = None,
        token_budget_provider: Any | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._tool_registry_provider = tool_registry_provider
        self._artifact_store = artifact_store
        self._permission_checker_provider = permission_checker_provider
        self._agent_settings_provider = agent_settings_provider
        self._token_budget_provider = token_budget_provider

    def get_schema(self) -> ToolSchema:
        agent_types = _available_agent_types()
        agent_type_description = (
            "Optional subagent type. Use explore or plan for read-heavy investigation, "
            "implement for a focused code change, and verification for adversarial read-only checks. "
            f"Available types: {', '.join(agent_types)}."
        )
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Short description of the delegated task, shown in the UI.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The complete, self-contained prompt for the subagent.",
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": agent_types,
                        "description": agent_type_description,
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model override for this subagent. Overrides the agent "
                            "definition's model. Use 'inherit' or omit to keep the session model."
                        ),
                    },
                    "effort": {
                        "type": "string",
                        "description": (
                            "Optional reasoning-effort override (low/medium/high/…) for this "
                            "subagent. Applies to reasoning-capable providers; omit to inherit."
                        ),
                    },
                    "parallel_tasks": {
                        "type": "array",
                        "description": (
                            "Run multiple subtasks concurrently. Each item is an object with "
                            "'description', 'prompt', and optional 'agent_type'. Each subtask "
                            "must cover one concrete, semantically non-overlapping scope; duplicate "
                            "or substantially overlapping delegations are rejected. "
                            "When provided, the single-task 'prompt'/'description' fields are ignored."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "description": "Short description of this subtask.",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Complete prompt for this subtask.",
                                },
                                "agent_type": {
                                    "type": "string",
                                    "enum": agent_types,
                                    "description": agent_type_description,
                                },
                                "model": {
                                    "type": "string",
                                    "description": "Optional model override for this subtask ('inherit'/omit keeps session model).",
                                },
                                "effort": {
                                    "type": "string",
                                    "description": "Optional reasoning-effort override for this subtask (reasoning-capable providers).",
                                },
                                "workflow_id": {
                                    "type": "string",
                                    "description": "Optional workflow id used to group this subagent in the Agent workbench.",
                                },
                                "workflow_name": {
                                    "type": "string",
                                    "description": "Optional workflow display name.",
                                },
                                "workflow_mode": {
                                    "type": "string",
                                    "description": "Optional workflow orchestration mode, such as parallel or pipeline.",
                                },
                                "node_id": {
                                    "type": "string",
                                    "description": "Optional DAG node id for this subtask within a workflow.",
                                },
                                "task_id": {
                                    "type": "string",
                                    "description": "Optional shared swarm task id associated with this subtask.",
                                },
                                "objective": {
                                    "type": "string",
                                    "description": "Optional concise objective shown in the Agent workbench.",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional DAG node or task ids this subtask depends on.",
                                },
                                "blocked_by": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional concrete task ids currently blocking this subtask.",
                                },
                                "required_for_final": {
                                    "type": "boolean",
                                    "description": "Whether the parent should wait for this result before finalizing.",
                                },
                                "cancel_with_parent": {
                                    "type": "boolean",
                                    "description": "Whether parent cancel should also cancel this subtask. Independent of required_for_final.",
                                },
                                "detach_from_parent": {
                                    "type": "boolean",
                                    "description": "If true, this subtask keeps running after parent cancel.",
                                },
                                "read_only": {
                                    "type": "boolean",
                                    "description": "Whether this subtask is intended to be read-only.",
                                },
                                "write_scope": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional file/path scopes this subtask may modify.",
                                },
                                "isolation": {
                                    "type": "string",
                                    "enum": ["worktree"],
                                    "description": "Optional isolation mode: run this subtask in a temporary git worktree.",
                                },
                            },
                            "required": ["description", "prompt"],
                        },
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Optional no-progress deadline in seconds per subtask (min 300, max 1800). "
                            "Omit it for no subagent-level deadline; active progress renews the deadline."
                        ),
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Start a single subtask asynchronously and return immediately with a subagent id. "
                            "Use task_stop to cancel it later. Ignored for parallel_tasks."
                        ),
                    },
                    "isolation": {
                        "type": "string",
                        "enum": ["worktree"],
                        "description": (
                            "Isolation mode. 'worktree' runs the subagent in a temporary git worktree "
                            "(new branch under .minicode/worktrees/). Removed automatically when the "
                            "subagent made no changes; kept and reported otherwise. Falls back to "
                            "non-isolated execution outside a git repository."
                        ),
                    },
                    "workflow_id": {
                        "type": "string",
                        "description": "Optional workflow id used to group this subagent in the Agent workbench.",
                    },
                    "workflow_name": {
                        "type": "string",
                        "description": "Optional workflow display name.",
                    },
                    "workflow_mode": {
                        "type": "string",
                        "description": "Optional workflow orchestration mode, such as parallel or pipeline.",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional DAG node id for this subagent within a workflow.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional shared swarm task id associated with this subagent.",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Optional concise objective shown in the Agent workbench.",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional DAG node or task ids this subagent depends on.",
                    },
                    "blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional concrete task ids currently blocking this subagent.",
                    },
                    "required_for_final": {
                        "type": "boolean",
                        "description": "Whether the parent should wait for this result before finalizing.",
                    },
                    "cancel_with_parent": {
                        "type": "boolean",
                        "description": "Whether parent cancel should also cancel this subagent. Independent of required_for_final.",
                    },
                    "detach_from_parent": {
                        "type": "boolean",
                        "description": "If true, this subagent keeps running after parent cancel (cc async unlinked AbortController).",
                    },
                    "read_only": {
                        "type": "boolean",
                        "description": "Whether this subagent is intended to be read-only.",
                    },
                    "write_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional file/path scopes this subagent may modify.",
                    },
                },
                "anyOf": [
                    {"required": ["description", "prompt"]},
                    {"required": ["parallel_tasks"]},
                ],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if self._is_recursive_subagent_call(context):
            return ToolResult(
                content=(
                    "Blocked recursive subagent delegation. Subagents cannot call the task tool; "
                    "return a concise summary to the parent agent instead."
                ),
                is_error=True,
                status="blocked",
                display_summary="Blocked recursive subagent",
                result_kind="subagent",
            )

        description = str(args.get("description") or "").strip()
        parallel_tasks = args.get("parallel_tasks")
        timeout_raw = args.get("timeout_seconds")
        timeout_seconds = (
            min(max(float(timeout_raw), 300.0), 1800.0)
            if timeout_raw is not None
            else None
        )

        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()
        if llm is None or tool_registry is None or permission_checker is None:
            return self._error_result("Subagent runtime is not configured")
        runtime = self._runtime_from_context(context) or default_runtime()
        parent_run_id = str(self._metadata_from_context(context).get("run_id") or "").strip()

        # ── Parallel execution path ──
        if isinstance(parallel_tasks, list) and len(parallel_tasks) >= 2:
            tasks: list[dict[str, Any]] = []
            for item in parallel_tasks[:5]:  # cap at 5 parallel subtasks
                if not isinstance(item, dict):
                    continue
                t_desc = str(item.get("description") or "").strip()
                t_prompt = str(item.get("prompt") or "").strip()
                t_type = str(item.get("agent_type") or "general-purpose").strip().lower()
                if t_desc and t_prompt:
                    tasks.append({
                        "description": t_desc,
                        "prompt": t_prompt,
                        "agent_type": normalize_agent_type(t_type, get_custom_agent=get_custom_agent),
                        **_nonempty_subagent_metadata(item),
                    })
            if len(tasks) >= 2:
                scopes = _exclusive_parallel_task_scopes(tasks)
                if len(scopes) != len(tasks):
                    return self._error_result(
                        "Parallel tasks contain duplicate delegations (same description and prompt). "
                        "Give each worker a distinct, non-overlapping assignment before delegating."
                    )
                for scope in scopes:
                    existing = _existing_similar_subagent(
                        runtime,
                        parent_run_id=parent_run_id,
                        label=scope,
                    )
                    if existing:
                        return self._error_result(
                            f"Similar delegated work already exists: {existing}. "
                            "Collect or reuse that result instead of launching another subagent."
                        )
                for task, scope in zip(tasks, scopes, strict=True):
                    task["prompt"] = _scope_parallel_task_prompt(task, scope=scope)
                if bool(args.get("run_in_background")):
                    return await self._start_background_subtasks(
                        tasks=tasks,
                        context=context,
                        timeout_seconds=timeout_seconds,
                    )
                return await self._run_parallel_subtasks(
                    tasks, context, timeout_seconds,
                )

        # ── Single execution path ──
        prompt = str(args.get("prompt") or "").strip()
        resume_subagent_id = str(args.get("resume_subagent_id") or "").strip()
        isolation = str(args.get("isolation") or "").strip().lower()
        if isolation and isolation != "worktree":
            return self._error_result(
                f"Unsupported isolation mode: {isolation!r}. Only 'worktree' is supported."
            )
        agent_type = normalize_agent_type(
            str(args.get("agent_type") or "general-purpose"),
            get_custom_agent=get_custom_agent,
        )
        if not description:
            return self._error_result("Missing description argument")
        if not prompt:
            return self._error_result("Missing prompt argument")
        if resume_subagent_id:
            from backend.agent.checkpoint import load_latest_run_checkpoint

            if load_latest_run_checkpoint(resume_subagent_id) is None:
                return self._error_result(
                    f"Cannot resume subagent {resume_subagent_id}: checkpoint_not_found. "
                    "The retained partial result was preserved."
                )

        # Anchor the worker to its own assignment so it does not drift into
        # sibling objectives, and reject overlap with earlier sibling work.
        if not resume_subagent_id:
            assigned_scope = str(args.get("objective") or description).strip()
            existing = _existing_similar_subagent(
                runtime,
                parent_run_id=parent_run_id,
                label=assigned_scope,
            )
            if existing:
                return self._error_result(
                    f"Similar delegated work already exists: {existing}. "
                    "Use task_status to collect it or change the assignment scope."
                )
            prompt = _scope_parallel_task_prompt({"prompt": prompt}, scope=assigned_scope)

        if bool(args.get("run_in_background")):
            return await self._start_background_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                timeout_seconds=timeout_seconds,
                subagent_metadata=_subagent_metadata(args),
                subagent_id=resume_subagent_id or None,
                resume_from_checkpoint=bool(resume_subagent_id),
            )

        return await self._run_single_subtask_auto_background(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            context=context,
            timeout_seconds=timeout_seconds,
            subagent_metadata=_subagent_metadata(args),
            subagent_id=resume_subagent_id or None,
            resume_from_checkpoint=bool(resume_subagent_id),
        )

    # ------------------------------------------------------------------
    # Single subtask execution
    # ------------------------------------------------------------------

    async def _start_background_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        timeout_seconds: float | None,
        subagent_metadata: dict[str, Any] | None = None,
        subagent_id: str | None = None,
        resume_from_checkpoint: bool = False,
    ) -> ToolResult:
        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        if not runtime.try_reserve_subagent_slots([subagent_id]):
            return ToolResult(
                content=_SUBAGENT_CAPACITY_MESSAGE,
                is_error=True,
                status="blocked",
                display_summary="Subagent capacity reached",
                result_kind="subagent",
            )
        try:
            subagent_id = self._spawn_background_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                timeout_seconds=timeout_seconds,
                subagent_metadata=subagent_metadata,
                subagent_id=subagent_id,
                resume_from_checkpoint=resume_from_checkpoint,
            )
        except Exception:
            runtime.release_subagent_slot(subagent_id)
            raise
        await asyncio.sleep(0)

        return ToolResult(
            content=(
                f"{'Resumed' if resume_from_checkpoint else 'Started'} background subagent {subagent_id} ({agent_type}). "
                "It will report progress through subagent events. "
                f"Use task_status with subagent_id={subagent_id} and wait_seconds=30 to wait once and collect the result; "
                "do not poll repeatedly or use sleep. "
                f"Use task_stop with subagent_id={subagent_id} to cancel it."
            ),
            display_summary=f"Subagent running: {description[:60]}",
            result_kind="subagent",
            status="running",
        )

    async def _start_background_subtasks(
        self,
        *,
        tasks: list[dict[str, Any]],
        context: ToolExecutionContext | None,
        timeout_seconds: float | None,
    ) -> ToolResult:
        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_ids = [f"subagent-{uuid4().hex[:8]}" for _ in tasks]
        if not runtime.try_reserve_subagent_slots(subagent_ids):
            return ToolResult(
                content=_SUBAGENT_CAPACITY_MESSAGE,
                is_error=True,
                status="blocked",
                display_summary="Subagent capacity reached",
                result_kind="subagent",
            )
        started: list[tuple[str, dict[str, str]]] = []
        try:
            for subagent_id, task in zip(subagent_ids, tasks, strict=True):
                self._spawn_background_subtask(
                    description=task["description"],
                    prompt=task["prompt"],
                    agent_type=task.get("agent_type", "general-purpose"),
                    context=context,
                    timeout_seconds=timeout_seconds,
                    subagent_metadata=_subagent_metadata(task),
                    subagent_id=subagent_id,
                )
                started.append((subagent_id, task))
        except Exception:
            # Release only the slots for workers that were never spawned. The
            # spawned ones keep their slot (they are still running and self-release
            # via their done-callback); releasing them here would corrupt the
            # capacity count and allow later delegations to exceed the cap.
            spawned_ids = {subagent_id for subagent_id, _ in started}
            for subagent_id in subagent_ids:
                if subagent_id not in spawned_ids:
                    runtime.release_subagent_slot(subagent_id)
            raise
        await asyncio.sleep(0)
        lines = [
            f"Started {len(started)} background subagents.",
            "Collect the batch with one task_status(subagent_ids=[...], wait_seconds=30) call; do not poll each worker separately or use sleep. Use task_stop to cancel one.",
        ]
        for index, (subagent_id, task) in enumerate(started, 1):
            lines.append(f"{index}. {subagent_id} ({task.get('agent_type', 'general-purpose')}): {task['description']}")
        return ToolResult(
            content="\n".join(lines),
            display_summary=f"{len(started)} subagents running",
            result_kind="subagent",
            status="running",
        )

    def _spawn_background_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        timeout_seconds: float | None,
        subagent_metadata: dict[str, Any] | None = None,
        subagent_id: str | None = None,
        resume_from_checkpoint: bool = False,
    ) -> str:
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_cancel_event = asyncio.Event()

        task = asyncio.create_task(
            self._run_single_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                timeout_seconds=timeout_seconds,
                subagent_id=subagent_id,
                cancel_event=subagent_cancel_event,
                subagent_metadata=subagent_metadata,
                background=True,
                resume_from_checkpoint=resume_from_checkpoint,
            )
        )
        parent_metadata = metadata_from_context(context)
        parent_run_id = str(parent_metadata.get("run_id", "")).strip()
        runtime.register_subagent_task(
            subagent_id,
            task,
            cancel_event=subagent_cancel_event,
            parent_run_id=parent_run_id,
        )

        def _release_background_task(done_task: asyncio.Task[ToolResult]) -> None:
            runtime.release_subagent_task(subagent_id)
            runtime.release_subagent_slot(subagent_id)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # _run_single_subtask reports failures via subagent.done. The
                # callback only prevents an unhandled task exception warning.
                pass

        task.add_done_callback(_release_background_task)
        return subagent_id

    async def _run_single_subtask_auto_background(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        timeout_seconds: float | None,
        subagent_metadata: dict[str, Any] | None = None,
        subagent_id: str | None = None,
        resume_from_checkpoint: bool = False,
    ) -> ToolResult:
        """Run a delegation synchronously, but hand it off to the background if it
        exceeds the wall-clock auto-background deadline so the parent is never
        blocked indefinitely (cc AgentTool auto-background).

        This is distinct from ``timeout_seconds`` (a no-progress inactivity budget
        that cancels the subagent): here the subagent keeps running and the parent
        collects it later via task_status.
        """
        auto_ms = _auto_background_ms()
        if auto_ms <= 0:
            return await self._run_single_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                timeout_seconds=timeout_seconds,
                subagent_metadata=subagent_metadata,
                subagent_id=subagent_id,
                resume_from_checkpoint=resume_from_checkpoint,
            )

        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        cancel_event = asyncio.Event()
        parent_run_id = str(self._metadata_from_context(context).get("run_id") or "").strip()
        task = asyncio.create_task(
            self._run_single_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                timeout_seconds=timeout_seconds,
                subagent_id=subagent_id,
                cancel_event=cancel_event,
                subagent_metadata=subagent_metadata,
                resume_from_checkpoint=resume_from_checkpoint,
            )
        )
        runtime.register_subagent_task(
            subagent_id,
            task,
            cancel_event=cancel_event,
            parent_run_id=parent_run_id,
        )

        def _release(done_task: asyncio.Task[ToolResult]) -> None:
            runtime.release_subagent_task(subagent_id)
            with suppress(asyncio.CancelledError, Exception):
                done_task.result()

        task.add_done_callback(_release)
        try:
            # shield: a timeout must not cancel the child — it keeps running in
            # the background for later collection.
            return await asyncio.wait_for(asyncio.shield(task), timeout=auto_ms / 1000.0)
        except asyncio.TimeoutError:
            runtime.mark_subagent_background(subagent_id)
            return ToolResult(
                content=(
                    f"Subagent {subagent_id} ({agent_type}) has run for over "
                    f"{auto_ms // 1000}s and was moved to the background so it does not block you. "
                    f"Use task_status with subagent_id={subagent_id} and wait_seconds=30 to collect "
                    f"the result, or task_stop with subagent_id={subagent_id} to cancel it."
                ),
                display_summary=f"Subagent backgrounded: {description[:60]}",
                result_kind="subagent",
                status="running",
            )

    async def _run_single_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        timeout_seconds: float | None = None,
        subtask_index: int | None = None,
        total_subtasks: int | None = None,
        subagent_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        subagent_metadata: dict[str, Any] | None = None,
        background: bool = False,
        resume_from_checkpoint: bool = False,
    ) -> ToolResult:
        """Run one isolated subagent loop with timeout and progress reporting.

        Returns a structured ``ToolResult`` that includes the subagent summary,
        duration, iteration count, and tool-call statistics.
        """
        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()

        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        parent_id = context.task_id if context and context.task_id else context.session_id if context else ""
        emit_event = context.emit_event if context else None
        runtime = self._runtime_from_context(context) or default_runtime()
        parent_metadata = self._metadata_from_context(context)
        workflow_metadata = _subagent_metadata(subagent_metadata)
        # Honor a per-subagent model/effort override (from the task-call args or
        # the custom agent definition). When present this replaces the inherited
        # session adapter with a fresh one; otherwise ``llm`` is unchanged.
        llm = _resolve_subagent_llm(
            llm,
            agent_type=agent_type,
            model_override=workflow_metadata.get("model", ""),
            effort_override=workflow_metadata.get("effort", ""),
        )
        # A child owns its cancellation signal. Reusing the parent's event lets a
        # child deadline cancel the parent and every sibling sharing that context.
        subagent_cancel_event = cancel_event or asyncio.Event()
        parent_run_id = str(parent_metadata.get("run_id", ""))
        try:
            # Background workers that are not required for the parent final reply
            # default to detach; explicit metadata always wins.
            cancel_with_parent = workflow_metadata.get("cancel_with_parent")
            detach_from_parent = workflow_metadata.get("detach_from_parent")
            if (
                background
                and not workflow_metadata["required_for_final"]
                and "cancel_with_parent" not in (subagent_metadata or {})
                and "detach_from_parent" not in (subagent_metadata or {})
            ):
                detach_from_parent = True
                cancel_with_parent = False
            subagent_record = runtime.start_subagent(
                subagent_id=subagent_id,
                parent_run_id=parent_run_id,
                agent_type=agent_type,
                prompt_summary=description,
                background=background,
                workflow_id=workflow_metadata["workflow_id"],
                workflow_name=workflow_metadata["workflow_name"],
                workflow_mode=workflow_metadata["workflow_mode"],
                node_id=workflow_metadata["node_id"],
                task_id=workflow_metadata["task_id"],
                objective=workflow_metadata["objective"],
                depends_on=workflow_metadata["depends_on"],
                blocked_by=workflow_metadata["blocked_by"],
                required_for_final=workflow_metadata["required_for_final"],
                cancel_with_parent=cancel_with_parent,
                detach_from_parent=detach_from_parent,
                read_only=workflow_metadata["read_only"],
                write_scope=workflow_metadata["write_scope"],
                current_activity=workflow_metadata["current_activity"],
            ) if runtime is not None else None
        except RuntimeError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                status="blocked",
                display_summary="Subagent capacity reached",
                result_kind="subagent",
            )

        # ── Worktree isolation (cc AgentTool isolation: "worktree") ──
        # Created after the capacity check so a blocked delegation never leaves
        # a stray worktree behind. On any git failure we degrade gracefully to
        # non-isolated execution and tell the parent why.
        agent_worktree = None
        worktree_fallback_note = ""
        if workflow_metadata.get("isolation") == "worktree" and not resume_from_checkpoint:
            from backend.agent.worktree import (
                cleanup_stale_worktrees,
                create_agent_worktree,
            )

            parent_workspace_root = (
                Path(context.workspace_root)
                if context is not None and context.workspace_root
                else Path.cwd()
            )
            # First delegation per git root sweeps orphaned worktrees left by a
            # killed process (clean ones removed, changed ones kept). Best-effort.
            await asyncio.to_thread(cleanup_stale_worktrees, parent_workspace_root)
            agent_worktree, worktree_reason = await asyncio.to_thread(
                create_agent_worktree, subagent_id, parent_workspace_root
            )
            if agent_worktree is None:
                worktree_fallback_note = (
                    f"Worktree isolation was requested but unavailable: {worktree_reason}. "
                    "The subagent ran directly in the shared workspace."
                )
                logger.warning(
                    "Worktree isolation fallback for %s: %s", subagent_id, worktree_reason
                )
        elif resume_from_checkpoint:
            # A resumed subagent re-adopts the worktree recorded in its
            # checkpoint (resume_payload["worktree_path"]). Missing/invalid
            # worktree degrades to non-isolated execution with an explanation.
            try:
                from backend.agent.checkpoint import load_latest_run_checkpoint
                from backend.agent.worktree import resume_agent_worktree

                resume_checkpoint = await asyncio.to_thread(
                    load_latest_run_checkpoint, subagent_id
                )
                saved_worktree_path = str(
                    (resume_checkpoint.resume_payload or {}).get("worktree_path", "")
                    if resume_checkpoint is not None
                    else ""
                ).strip()
                if saved_worktree_path:
                    agent_worktree = await asyncio.to_thread(
                        resume_agent_worktree, saved_worktree_path
                    )
                    if agent_worktree is None:
                        worktree_fallback_note = (
                            f"The previous run used an isolated worktree at "
                            f"{saved_worktree_path}, which no longer exists. "
                            "The resumed subagent ran directly in the shared workspace."
                        )
                        logger.warning(
                            "Worktree resume fallback for %s: %s missing",
                            subagent_id,
                            saved_worktree_path,
                        )
            except Exception as exc:  # noqa: BLE001 — resume must not fail on worktree lookup
                logger.warning("Worktree resume lookup failed for %s: %s", subagent_id, exc)

        async def _cleanup_worktree() -> str:
            """Remove the worktree when unchanged; return a keep-note otherwise."""
            nonlocal agent_worktree
            if agent_worktree is None:
                return ""
            from backend.agent.worktree import cleanup_agent_worktree

            info, agent_worktree = agent_worktree, None
            try:
                kept, kept_path = await asyncio.to_thread(cleanup_agent_worktree, info)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Worktree cleanup failed for %s: %s", subagent_id, exc)
                return f"Worktree left at {info.worktree_path} (branch {info.branch}); cleanup failed."
            if kept:
                return (
                    f"The subagent worked in an isolated git worktree with changes kept at: "
                    f"{kept_path} (branch {info.branch})."
                )
            return ""

        if emit_event is not None:
            start_event = AgentEvent.subagent_start(
                subagent_id=subagent_id,
                parent_id=parent_id,
                role=agent_type,
                prompt=description,
                current_activity=workflow_metadata["current_activity"],
                waiting_on=workflow_metadata["waiting_on"],
                blocks_final_reply=workflow_metadata["blocks_final_reply"],
                last_progress_at=int(time.time() * 1000),
            )
            start_event.data.update(_nonempty_subagent_metadata(workflow_metadata))
            if subagent_record is not None:
                start_event.data["record"] = subagent_record.to_dict()
                start_event.data["parent_run_id"] = parent_run_id
            await emit_event("subagent.start", start_event.data)
        await _run_task_created_hook(
            task_id=subagent_id,
            subject=description,
            description=_hook_visible_task_prompt(prompt),
            teammate_name=agent_type,
        )
        await _run_subagent_start_hook(subagent_id, agent_type)

        journal: ExecutionJournal | None = None
        if runtime is not None:
            try:
                journal = runtime.execution_journal(subagent_id)
                journal.append(
                    "user_prompt",
                    {
                        "content": prompt,
                        "description": description,
                        "agent_type": agent_type,
                        "background": background,
                        "required_for_final": workflow_metadata["required_for_final"],
                        "cancel_with_parent": bool(
                            getattr(subagent_record, "cancel_with_parent", True)
                            if subagent_record is not None
                            else workflow_metadata.get("cancel_with_parent", True)
                        ),
                        "detach_from_parent": bool(
                            getattr(subagent_record, "detach_from_parent", False)
                            if subagent_record is not None
                            else workflow_metadata.get("detach_from_parent", False)
                        ),
                    },
                )
            except Exception as journal_exc:
                logger.debug("Failed opening subagent journal for %s: %s", subagent_id, journal_exc)
                journal = None

        sub_settings = self._resolve_agent_settings()
        sub_settings = replace(
            sub_settings,
            max_iterations=_subagent_iteration_budget(
                prompt=prompt,
                agent_type=agent_type,
                configured=sub_settings.max_iterations,
            ),
        )
        sub_budget = self._resolve_token_budget()
        # Apply a custom agent's tool restrictions (Agent editor). A custom
        # definition can declare a tools whitelist and/or disallowed_tools; those
        # must actually be enforced at runtime (deny rules block the call), not
        # just stored on the definition. model override still inherits the
        # session LLM (rebuilding an adapter per subagent is a separate change).
        custom_deny_rules = _custom_agent_deny_rules(agent_type, tool_registry)
        sub_context = self._build_permission_context(
            agent_type, context, extra_deny_rules=custom_deny_rules
        )
        delegated_prompt = self._build_subagent_prompt(agent_type, prompt)
        if agent_worktree is not None:
            delegated_prompt = (
                f"{delegated_prompt}\n\n"
                "[Worktree isolation]\n"
                f"You are working in an isolated git worktree at: {agent_worktree.worktree_path} "
                f"(branch {agent_worktree.branch}). All file paths must stay inside this worktree; "
                "do not modify the original repository checkout."
            )
        sub_state = AgentState(user_message=delegated_prompt, max_iterations=sub_settings.max_iterations)
        # Subagents cannot delegate; the prompt builder drops the delegation
        # section so the system prompt matches the denied toolset.
        sub_state.prompt_context["subagent"] = True
        sub_state.workspace_context = parent_metadata.get("workspace_context")
        if context is not None:
            sub_state.conversation_id = context.conversation_id
            sub_state.checkpoint_manager = context.checkpoint_manager
        parent_prompt_cache_safe_params = parent_metadata.get("prompt_cache_safe_params")
        subagent_metadata_payload = {
            **sanitize_subagent_runtime_metadata(parent_metadata),
            "agent_runtime": runtime,
            "parent_run_id": parent_run_id,
            "agent_role": f"subagent:{agent_type}",
            "agent_mode": "subagent",
            "run_id": subagent_id,
            "cancel_event": subagent_cancel_event,
            **_nonempty_subagent_metadata(workflow_metadata),
        }
        if resume_from_checkpoint:
            subagent_metadata_payload["resume_from_checkpoint"] = True
        if agent_worktree is not None:
            # The child's toolchain follows AgentLoopSessionContext.workspace_root
            # (loop.py builds tool_ctx.workspace_root and metadata["cwd"] from it),
            # so pointing both at the worktree moves every filesystem/shell tool
            # and the path-escape boundary into the isolated copy.
            subagent_metadata_payload["cwd"] = str(agent_worktree.worktree_path)
            subagent_metadata_payload.pop("workspace_context", None)
            sub_state.workspace_context = None
        if isinstance(parent_prompt_cache_safe_params, dict):
            subagent_metadata_payload["parent_prompt_cache_safe_params"] = dict(
                parent_prompt_cache_safe_params
            )

        def prompt_cache_fork_diagnostic() -> dict[str, Any]:
            existing = subagent_metadata_payload.get("prompt_cache_fork")
            if isinstance(existing, dict) and existing:
                return dict(existing)
            return _subagent_prompt_cache_fork_diagnostic(
                parent_prompt_cache_safe_params,
                subagent_metadata_payload.get("prompt_cache_safe_params"),
            )

        summary_parts: list[str] = []
        start_time = time.perf_counter()
        timed_out = False
        last_tool_name = ""
        terminal_status = "completed"
        terminal_reason = ""
        terminal_usage: dict[str, Any] = {}
        terminal_provider_raw: dict[str, Any] = {}
        last_error = ""
        tool_evidence: list[str] = []
        last_progress_at = time.monotonic()
        deadline_requested_at: float | None = None
        deadline_summary_parts: list[str] | None = None
        deadline_tool_evidence: list[str] | None = None
        journal_terminal_written = False

        def _close_journal(
            *,
            status: str,
            summary: str = "",
            reason: str = "",
            extra: dict[str, Any] | None = None,
        ) -> None:
            nonlocal journal_terminal_written
            if journal is None or journal_terminal_written:
                return
            try:
                if summary:
                    journal.append(
                        "assistant",
                        {
                            "content": summary[:8000],
                            "status": status,
                            "reason": reason,
                        },
                    )
                journal.close_unresolved_tool_uses(reason=reason or status)
                journal.append_terminal(
                    status=status,
                    summary=summary[:2000],
                    reason=reason or status,
                    extra=extra,
                )
                journal_terminal_written = True
            except Exception as journal_exc:
                logger.debug("Journal terminal close failed for %s: %s", subagent_id, journal_exc)
        sub_context_builder = self._build_subagent_context_builder(
            context=context,
            token_budget=sub_budget,
            agent_settings=sub_settings,
        )

        try:
            from backend.agent.loop import run_agent_loop

            parent_approval_handler = context.approval_handler if context is not None else None
            can_forward_approval = (
                callable(parent_approval_handler)
                and emit_event is not None
            )
            approval_ready: dict[str, asyncio.Event] = {}
            pending_approval_ids: set[str] = set()

            async def subagent_approval_handler(tool_call_id: str) -> dict[str, str]:
                nonlocal last_progress_at
                local_tool_call_id = str(tool_call_id or "").strip()
                if can_forward_approval and parent_approval_handler is not None:
                    # Child providers commonly reuse short ids such as call_1.
                    # Namespace the id before it enters the parent WebSocket
                    # approval map so parallel children cannot overwrite one
                    # another's pending Future.
                    parent_tool_call_id = f"{subagent_id}:{local_tool_call_id}"
                    ready = approval_ready.setdefault(local_tool_call_id, asyncio.Event())
                    await ready.wait()
                    pending_approval_ids.add(local_tool_call_id)
                    try:
                        response = parent_approval_handler(parent_tool_call_id)
                        if inspect.isawaitable(response):
                            response = await response
                        if isinstance(response, dict):
                            return response
                    finally:
                        pending_approval_ids.discard(local_tool_call_id)
                        last_progress_at = time.monotonic()
                return {
                    "action": "reject",
                    "guidance": (
                        f"Subagent {subagent_id} cannot request user approvals directly. "
                        "Return a summary and let the main agent decide the next action."
                    ),
                }

            async def subagent_event_bridge(event_type: str, data: dict[str, Any]) -> None:
                nonlocal last_progress_at
                if event_type not in {"tool_call", "agent.progress"}:
                    return
                last_progress_at = time.monotonic()
                if emit_event is None:
                    return
                tool_name = str(data.get("tool_name") or data.get("name") or "")
                tool_call_id = str(data.get("tool_call_id") or data.get("id") or "")
                message = _user_visible_progress_text(
                    data.get("message") or data.get("summary") or data.get("detail")
                )
                if event_type == "tool_call" and tool_name:
                    current_activity = description
                    waiting_on = "tool"
                    detail = ""
                else:
                    current_activity = message or description or "Working"
                    waiting_on = _user_visible_progress_text(data.get("stage")) or "tool"
                    detail = message
                progress_event = AgentEvent.subagent_progress(
                    subagent_id=subagent_id,
                    iteration=sub_state.iterations,
                    max_iterations=sub_settings.max_iterations,
                    tool_name=tool_name,
                    detail=detail,
                    current_activity=current_activity,
                    waiting_on=waiting_on,
                    blocks_final_reply=workflow_metadata["blocks_final_reply"],
                    last_progress_at=int(time.time() * 1000),
                )
                progress_event.data["source_event_type"] = event_type
                if tool_call_id:
                    progress_event.data["tool_call_id"] = tool_call_id
                await emit_event("subagent.progress", progress_event.data)

            # Keep the entire child query generator in ONE task.
            # Advancing an async generator across per-event create_task()
            # calls breaks ContextVar token continuity (bind in task A,
            # unbind in task B) and crashes the child loop.
            query_stream = QueryEngine(runner=run_agent_loop).submit(QuerySubmission(
                        user_message=delegated_prompt,
                        session=AgentSession(
                            llm=llm,
                            tool_registry=tool_registry,
                            artifact_store=self._artifact_store,
                            permission_checker=permission_checker,
                            agent_settings=sub_settings,
                            token_budget=sub_budget,
                            context_builder=sub_context_builder,
                            approval_handler=subagent_approval_handler,
                        ),
                        state=sub_state,
                        runtime=AgentLoopSessionContext(
                            permission_context=sub_context,
                            workspace_root=(
                                agent_worktree.worktree_path
                                if agent_worktree is not None
                                else None
                            ),
                            session_id=subagent_id,
                            task_id=subagent_id,
                            task_manager=context.task_manager if context else None,
                            emit_event=subagent_event_bridge,
                            metadata=subagent_metadata_payload,
                        ),
                    ))
            event_queue: asyncio.Queue[AgentEvent | BaseException | None] = asyncio.Queue()

            async def _pump_query_events() -> None:
                try:
                    async for event in query_stream:
                        await event_queue.put(event)
                    await event_queue.put(None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await event_queue.put(exc)

            pump_task = asyncio.create_task(_pump_query_events())
            try:
                while True:
                    wait_timeout = 5.0
                    if timeout_seconds is not None and deadline_requested_at is None:
                        remaining = timeout_seconds - (time.monotonic() - last_progress_at)
                        wait_timeout = max(0.05, min(wait_timeout, remaining))
                    try:
                        item = await asyncio.wait_for(event_queue.get(), timeout=wait_timeout)
                    except asyncio.TimeoutError:
                        # timeout_seconds is an inactivity budget. Provider
                        # heartbeats, tool activity and approval progress renew it.
                        if timeout_seconds is not None and time.monotonic() - last_progress_at >= timeout_seconds:
                            if deadline_requested_at is None:
                                timed_out = True
                                deadline_requested_at = time.monotonic()
                                deadline_summary_parts = list(summary_parts)
                                deadline_tool_evidence = list(tool_evidence)
                                subagent_cancel_event.set()
                                continue
                            if time.monotonic() - deadline_requested_at >= 30.0:
                                if not pump_task.done():
                                    pump_task.cancel()
                                    with suppress(asyncio.CancelledError, Exception):
                                        await pump_task
                                break
                        continue
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        if isinstance(item, asyncio.CancelledError):
                            if timed_out:
                                break
                            raise
                        raise item
                    event = item
                    if event.type in {"text_chunk", "tool_call", "tool_result", "approval_request", "agent.progress", "done", "error"}:
                        last_progress_at = time.monotonic()
                    if event.type == "text_chunk":
                        content = str(event.data.get("content", ""))
                        source = str(event.data.get("source") or "").strip().lower()
                        visibility = str(event.data.get("visibility") or "").strip().lower()
                        phase = str(event.data.get("phase") or "").strip().lower()
                        has_routing_metadata = bool(source or visibility or phase)
                        if (
                            content
                            and (
                                not has_routing_metadata
                                or visibility == "final"
                                or phase in {"final", "final_answer"}
                                or source in {"model_final", "fallback", "reply", "partial"}
                            )
                        ):
                            summary_parts.append(content)
                    elif event.type == "approval_request":
                        if can_forward_approval and emit_event is not None:
                            local_tool_call_id = str(
                                event.data.get("tool_call_id") or event.data.get("id") or ""
                            ).strip()
                            parent_tool_call_id = f"{subagent_id}:{local_tool_call_id}"
                            approval_ready.setdefault(local_tool_call_id, asyncio.Event())
                            bridged_data = {
                                **event.data,
                                "tool_call_id": parent_tool_call_id,
                                "subagent_id": subagent_id,
                                "source_agent": subagent_id,
                            }
                            await emit_event("approval_request", {
                                **bridged_data,
                            })
                            approval_ready[local_tool_call_id].set()
                    elif event.type == "tool_call":
                        if journal is not None:
                            try:
                                tool_name = str(
                                    event.data.get("tool_name")
                                    or event.data.get("name")
                                    or "tool"
                                )
                                call_id = str(
                                    event.data.get("tool_call_id")
                                    or event.data.get("id")
                                    or ""
                                ).strip()
                                arguments = event.data.get("arguments") or event.data.get("args") or {}
                                journal.append(
                                    "tool_use",
                                    {
                                        "tool_call": {
                                            "id": call_id,
                                            "name": tool_name,
                                            "arguments": arguments,
                                        },
                                        "content": str(event.data.get("content") or ""),
                                    },
                                )
                            except Exception as journal_exc:
                                logger.debug("Journal tool_use append failed: %s", journal_exc)
                    elif event.type == "error":
                        last_error = str(event.data.get("message", ""))
                    elif event.type == "tool_result":
                        last_tool_name = str(
                            event.data.get("tool_name")
                            or event.data.get("name")
                            or event.data.get("id")
                            or ""
                        )
                        evidence = str(
                            event.data.get("content")
                            or event.data.get("result")
                            or event.data.get("output")
                            or event.data.get("summary")
                            or ""
                        ).strip()
                        if evidence:
                            tool_evidence.append(f"{last_tool_name or 'tool'}: {evidence[:1200]}")
                        if journal is not None:
                            try:
                                call_id = str(
                                    event.data.get("tool_call_id")
                                    or event.data.get("id")
                                    or ""
                                ).strip()
                                journal.append(
                                    "tool_result",
                                    {
                                        "tool_call_id": call_id,
                                        "tool_name": last_tool_name or "tool",
                                        "content": evidence[:4000],
                                        "status": str(event.data.get("status") or "completed"),
                                    },
                                )
                            except Exception as journal_exc:
                                logger.debug("Journal tool_result append failed: %s", journal_exc)
                        try:
                            from backend.agent.checkpoint import save_run_checkpoint

                            save_run_checkpoint(
                                session_id=subagent_id,
                                user_message=delegated_prompt,
                                iterations=sub_state.iterations,
                                reply=sub_state.reply,
                                messages=sub_context_builder.export_snapshot().get("history", []),
                                tool_calls=sub_state.tool_calls,
                                active_skills=sub_state.active_skills,
                                disabled_tools=sub_state.disabled_tools,
                                stopped_reason="in_progress",
                                last_mutation_index=sub_state._last_mutation_index,
                                last_verified_mutation_index=sub_state.last_verified_mutation_index,
                                run_id=subagent_id,
                                conversation_id=str(sub_state.conversation_id or ""),
                                resume_payload={
                                    "run_id": subagent_id,
                                    "role": f"subagent:{agent_type}",
                                    **(
                                        {"worktree_path": str(agent_worktree.worktree_path)}
                                        if agent_worktree is not None
                                        else {}
                                    ),
                                },
                            )
                        except Exception as checkpoint_exc:
                            logger.debug("Incremental subagent checkpoint failed: %s", checkpoint_exc)
                    elif event.type == "done":
                        terminal_status = str(event.data.get("status") or "completed")
                        terminal_reason = str(event.data.get("reason") or "")
                        if isinstance(event.data.get("usage"), dict):
                            terminal_usage = dict(event.data["usage"])
                        if isinstance(event.data.get("providerRaw"), dict):
                            terminal_provider_raw = dict(event.data["providerRaw"])
                    if emit_event is not None and event.type == "tool_result":
                        await emit_event(
                            "subagent.progress",
                            AgentEvent.subagent_progress(
                                subagent_id=subagent_id,
                                iteration=sub_state.iterations,
                                max_iterations=sub_settings.max_iterations,
                                tool_name=last_tool_name,
                                detail="",
                                current_activity=description,
                                waiting_on="tool",
                                blocks_final_reply=workflow_metadata["blocks_final_reply"],
                                last_progress_at=int(time.time() * 1000),
                            ).data,
                        )
            finally:
                if not pump_task.done():
                    pump_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await pump_task

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if timed_out:
                summary_parts = deadline_summary_parts or []
                tool_evidence = deadline_tool_evidence or []
            streamed_summary = _strip_incomplete_subagent_preamble("".join(summary_parts))
            summary = streamed_summary or ("" if timed_out else sub_state.reply.strip())
            if not summary and tool_evidence:
                summary = "Completed tool evidence:\n" + "\n".join(tool_evidence[-8:])
            elif terminal_status == "completed" and _is_incomplete_subagent_summary(summary):
                terminal_status = "partial"
                terminal_reason = terminal_reason or "incomplete_final_summary"
                if tool_evidence:
                    summary = (
                        "The subagent stopped before producing a complete final summary. "
                        "Completed tool evidence was retained:\n"
                        + "\n".join(tool_evidence[-8:])
                    )
                else:
                    summary = "The subagent stopped before producing a complete final summary."
            if terminal_usage:
                from backend.llm.cost_tracker import CostTracker

                request_summary = terminal_provider_raw.get("request_summary")
                provider = str(
                    (request_summary.get("wire_api") if isinstance(request_summary, dict) else "")
                    or terminal_provider_raw.get("provider")
                    or type(llm).__name__
                )
                CostTracker.get_instance().record_usage(
                    input_tokens=int(terminal_usage.get("input_tokens") or 0),
                    output_tokens=int(terminal_usage.get("output_tokens") or 0),
                    cache_creation_input_tokens=int(terminal_usage.get("cache_creation_input_tokens") or 0),
                    cache_read_input_tokens=int(terminal_usage.get("cache_read_input_tokens") or 0),
                    reasoning_output_tokens=int(terminal_usage.get("reasoning_output_tokens") or 0),
                    model_id=getattr(llm, "_model", None),
                    provider=provider,
                    session_id=str(
                        (context.metadata.get("cost_session_id") if context and isinstance(context.metadata, dict) else "")
                        or (context.session_id if context else "")
                    ),
                )
            if timed_out:
                summary = _sanitize_timeout_partial_summary(summary)
            display_summary = _subagent_display_summary(summary)
            tool_call_count = len(sub_state.tool_calls)

            if timed_out:
                has_partial_result = bool(summary) and (
                    sub_state.iterations > 0 or tool_call_count > 0
                )
                if not has_partial_result:
                    summary = ""
                timeout_status = "partial" if has_partial_result else "failed"
                timeout_content = (
                    f"Subagent {subagent_id} ({agent_type}) made no progress for "
                    f"{timeout_seconds:.0f}s. It completed {sub_state.iterations} iteration(s) and "
                    f"{tool_call_count} tool call(s)."
                )
                if summary:
                    timeout_content += f"\nPartial result retained:\n{summary}"
                worktree_note = await _cleanup_worktree()
                for note in (worktree_note, worktree_fallback_note):
                    if note:
                        timeout_content += f"\n{note}"
                result_record = None
                completed_record = None
                _close_journal(
                    status=timeout_status,
                    summary=timeout_content,
                    reason="deadline_exceeded",
                    extra={
                        "timed_out": True,
                        "iterations": sub_state.iterations,
                        "tool_call_count": tool_call_count,
                        "duration_ms": elapsed_ms,
                    },
                )
                if runtime is not None:
                    result_record = runtime.store_subagent_result(
                        subagent_id,
                        status=timeout_status,
                        content=timeout_content,
                        duration_ms=elapsed_ms,
                        iterations=sub_state.iterations,
                        tool_call_count=tool_call_count,
                        timed_out=True,
                        usage=terminal_usage,
                    )
                    completed_record = runtime.complete_subagent(
                        subagent_id,
                        timeout_status,
                        summary=display_summary,
                        tool_count=tool_call_count,
                    )
                if emit_event is not None:
                    done_event = AgentEvent.subagent_done(
                        subagent_id=subagent_id,
                        summary=display_summary,
                        duration_ms=elapsed_ms,
                        iterations=sub_state.iterations,
                        tool_call_count=tool_call_count,
                        timed_out=True,
                        status=timeout_status,
                        termination_reason="deadline_exceeded",
                        initiator="runtime",
                        usage=terminal_usage,
                    )
                    if result_record is not None:
                        done_event.data["result"] = result_record.to_dict()
                    if completed_record is not None:
                        done_event.data["record"] = completed_record.to_dict()
                    prompt_cache_fork = prompt_cache_fork_diagnostic()
                    if prompt_cache_fork:
                        done_event.data["prompt_cache_fork"] = prompt_cache_fork
                    await emit_event("subagent.done", done_event.data)
                await _finalize_workflow_task(
                    context=context,
                    workflow_metadata=workflow_metadata,
                    subagent_id=subagent_id,
                    result_text=timeout_content,
                    status=timeout_status,
                )
                await _run_subagent_stop_hook(subagent_id, timeout_status, timeout_content, agent_type=agent_type)
                await _run_task_completed_hook(
                    task_id=subagent_id,
                    subject=description,
                    description=timeout_content,
                    teammate_name=agent_type,
                )
                await _run_teammate_idle_hook(teammate_name=agent_type)
                return ToolResult(
                    content=timeout_content,
                    is_error=not has_partial_result,
                    duration_ms=elapsed_ms,
                    display_summary=(
                        f"Subagent partially completed before deadline: {description[:60]}"
                        if has_partial_result
                        else f"Subagent deadline exceeded: {description[:60]}"
                    ),
                    result_kind="subagent",
                    status=timeout_status,
                )

            if terminal_status == "cancelled":
                raise asyncio.CancelledError
            if terminal_status == "failed" and terminal_reason == "max_iterations" and summary:
                terminal_status = "partial"
            if terminal_status == "failed":
                raise RuntimeError(last_error or terminal_reason or "Subagent run failed")

            result_status = "partial" if terminal_status == "partial" else "completed"
            result_text = self._build_subtask_result_summary(
                subagent_id=subagent_id,
                agent_type=agent_type,
                summary=summary,
                duration_ms=elapsed_ms,
                iterations=sub_state.iterations,
                tool_calls=sub_state.tool_calls,
                timed_out=timed_out,
                timeout_seconds=timeout_seconds,
                status=result_status,
            )
            worktree_note = await _cleanup_worktree()
            for note in (worktree_note, worktree_fallback_note):
                if note:
                    result_text += f"\n\n{note}"
            result_record = None
            completed_record = None
            _close_journal(
                status=result_status,
                summary=result_text,
                reason=terminal_reason or result_status,
                extra={
                    "iterations": sub_state.iterations,
                    "tool_call_count": tool_call_count,
                    "duration_ms": elapsed_ms,
                    "timed_out": timed_out,
                },
            )
            if runtime is not None:
                result_record = runtime.store_subagent_result(
                    subagent_id,
                    status=result_status,
                    content=result_text,
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    timed_out=timed_out,
                    usage=terminal_usage,
                )
                completed_record = runtime.complete_subagent(
                    subagent_id,
                    result_status,
                    summary=display_summary,
                    tool_count=tool_call_count,
                )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    summary=display_summary,
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    timed_out=timed_out,
                    status=result_status,
                    termination_reason=terminal_reason or (
                        "success" if result_status == "completed" else "partial"
                    ),
                    initiator="runtime",
                    usage=terminal_usage,
                )
                if result_record is not None:
                    done_event.data["result"] = result_record.to_dict()
                if completed_record is not None:
                    done_event.data["record"] = completed_record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await emit_event("subagent.done", done_event.data)
            await _finalize_workflow_task(
                context=context,
                workflow_metadata=workflow_metadata,
                subagent_id=subagent_id,
                result_text=result_text,
                status=result_status,
            )
            await _run_subagent_stop_hook(
                subagent_id,
                result_status,
                result_text,
                agent_type=agent_type,
            )
            await _run_task_completed_hook(
                task_id=subagent_id,
                subject=description,
                description=result_text,
                teammate_name=agent_type,
            )
            await _run_teammate_idle_hook(teammate_name=agent_type)
            return ToolResult(
                content=result_text,
                is_error=False,
                duration_ms=elapsed_ms,
                display_summary=f"Subagent ({agent_type}): {description[:60]}",
                result_kind="subagent",
                status=result_status,
            )
        except asyncio.CancelledError:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            record = None
            cancel_summary = "Subagent was cancelled."
            if summary_parts or tool_evidence:
                retained = "".join(summary_parts).strip()
                if not retained and tool_evidence:
                    retained = "Completed tool evidence:\n" + "\n".join(tool_evidence[-8:])
                if retained:
                    cancel_summary = f"Subagent was cancelled.\nPartial result retained:\n{retained}"
            with suppress(Exception):
                worktree_note = await _cleanup_worktree()
                if worktree_note:
                    cancel_summary += f"\n{worktree_note}"
            _close_journal(
                status="cancelled",
                summary=cancel_summary,
                reason="cancelled",
                extra={
                    "iterations": sub_state.iterations,
                    "tool_call_count": len(sub_state.tool_calls),
                    "duration_ms": elapsed_ms,
                },
            )
            if runtime is not None:
                runtime.store_subagent_result(
                    subagent_id,
                    status="cancelled",
                    content=cancel_summary,
                    error="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                    usage=terminal_usage,
                )
                record = runtime.complete_subagent(
                    subagent_id,
                    "cancelled",
                    summary="cancelled",
                    tool_count=len(sub_state.tool_calls),
                )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error="cancelled",
                    status="cancelled",
                    termination_reason="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    usage=terminal_usage,
                )
                if record is not None:
                    done_event.data["record"] = record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await emit_event("subagent.done", done_event.data)
            await _finalize_workflow_task(
                context=context,
                workflow_metadata=workflow_metadata,
                subagent_id=subagent_id,
                result_text=cancel_summary,
                status="cancelled",
            )
            await _run_subagent_stop_hook(
                subagent_id,
                "cancelled",
                cancel_summary,
                agent_type=agent_type,
            )
            await _run_task_completed_hook(
                task_id=subagent_id,
                subject=description,
                description=cancel_summary,
                teammate_name=agent_type,
            )
            await _run_teammate_idle_hook(teammate_name=agent_type)
            raise
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            error_content = (
                f"Subagent {subagent_id} ({agent_type}) failed after "
                f"{elapsed_ms}ms and {sub_state.iterations} iteration(s).\n"
                f"Error: {type(exc).__name__}: {exc}"
            )
            with suppress(Exception):
                worktree_note = await _cleanup_worktree()
                if worktree_note:
                    error_content += f"\n{worktree_note}"
            record = None
            _close_journal(
                status="failed",
                summary=error_content,
                reason=type(exc).__name__,
                extra={
                    "iterations": sub_state.iterations,
                    "tool_call_count": len(sub_state.tool_calls),
                    "duration_ms": elapsed_ms,
                },
            )
            if runtime is not None:
                runtime.store_subagent_result(
                    subagent_id,
                    status="failed",
                    content=error_content,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                    usage=terminal_usage,
                )
                record = runtime.complete_subagent(
                    subagent_id,
                    "failed",
                    summary=str(exc),
                    tool_count=len(sub_state.tool_calls),
                )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error=str(exc),
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    usage=terminal_usage,
                )
                if record is not None:
                    done_event.data["record"] = record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await emit_event("subagent.done", done_event.data)
            await _finalize_workflow_task(
                context=context,
                workflow_metadata=workflow_metadata,
                subagent_id=subagent_id,
                result_text=error_content,
                status="failed",
            )
            await _run_subagent_stop_hook(subagent_id, "failed", error_content, agent_type=agent_type)
            await _run_task_completed_hook(
                task_id=subagent_id,
                subject=description,
                description=error_content,
                teammate_name=agent_type,
            )
            await _run_teammate_idle_hook(teammate_name=agent_type)
            return ToolResult(
                content=error_content,
                is_error=True,
                duration_ms=elapsed_ms,
                display_summary=f"Subagent failed: {description[:60]}",
                result_kind="subagent",
            )

    # ------------------------------------------------------------------
    # Parallel subtask execution
    # ------------------------------------------------------------------

    async def _run_parallel_subtasks(
        self,
        tasks: list[dict[str, Any]],
        context: ToolExecutionContext | None,
        timeout_seconds: float | None,
    ) -> ToolResult:
        """Run multiple independently-deadlined subtasks concurrently."""
        total = len(tasks)
        start_time = time.perf_counter()
        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_ids = [f"subagent-{uuid4().hex[:8]}" for _ in tasks]
        if not runtime.try_reserve_subagent_slots(subagent_ids):
            return ToolResult(
                content=_SUBAGENT_CAPACITY_MESSAGE,
                is_error=True,
                status="blocked",
                display_summary="Subagent capacity reached",
                result_kind="subagent",
            )

        coros = [
            self._run_single_subtask(
                description=t["description"],
                prompt=t["prompt"],
                agent_type=t.get("agent_type", "general-purpose"),
                context=context,
                timeout_seconds=timeout_seconds,
                subtask_index=i,
                total_subtasks=total,
                subagent_id=subagent_ids[i],
                subagent_metadata=_subagent_metadata(t),
            )
            for i, t in enumerate(tasks)
        ]

        try:
            results = await asyncio.gather(*coros, return_exceptions=True)
        finally:
            for subagent_id in subagent_ids:
                runtime.release_subagent_slot(subagent_id)

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        # Merge results
        parts: list[str] = [f"Parallel subtasks completed ({total} tasks, {elapsed_ms / 1000:.1f}s total):\n"]
        has_error = False
        for i, (task, result) in enumerate(zip(tasks, results), 1):
            if isinstance(result, Exception):
                has_error = True
                parts.append(f"--- Task {i}/{total}: {task['description']} ---\nFAILED: {result}\n")
            elif isinstance(result, ToolResult):
                if result.is_error:
                    has_error = True
                parts.append(f"--- Task {i}/{total}: {task['description']} ---\n{result.content}\n")
            else:
                parts.append(f"--- Task {i}/{total}: {task['description']} ---\nNo result returned.\n")

        return ToolResult(
            content="\n".join(parts),
            is_error=has_error,
            duration_ms=elapsed_ms,
            display_summary=f"Parallel subtasks: {total} tasks",
            result_kind="subagent",
        )

    # ------------------------------------------------------------------
    # Result formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _build_subtask_result_summary(
        *,
        subagent_id: str,
        agent_type: str,
        summary: str,
        duration_ms: int,
        iterations: int,
        tool_calls: list,
        timed_out: bool,
        timeout_seconds: float | None,
        status: str = "completed",
    ) -> str:
        """Build a structured result summary for the parent agent.

        Includes the subagent's text output, timing metadata, and a compact
        list of tool calls so the parent knows what the subagent actually did.
        """
        header = f"Subagent {subagent_id} ({agent_type})"
        if timed_out:
            header += f" [TIMED OUT after {timeout_seconds:.0f}s]"
        completion_label = "partially completed" if status == "partial" else "completed"
        header += f" {completion_label} in {duration_ms / 1000:.1f}s, {iterations} iteration(s)."

        parts = [header]
        if summary:
            parts.append(f"\n{summary}")
        if tool_calls:
            parts.append(f"\nStats: {len(tool_calls)} tool call(s).")

        return "\n".join(parts)

    def _resolve_llm(self) -> LLMAdapter | None:
        if callable(self._llm_provider):
            return self._llm_provider()
        return self._llm_provider

    def _resolve_tool_registry(self) -> ToolRegistry | None:
        if callable(self._tool_registry_provider):
            return self._tool_registry_provider()
        return self._tool_registry_provider

    def _resolve_permission_checker(self) -> PermissionChecker | None:
        if callable(self._permission_checker_provider):
            return self._permission_checker_provider()
        return self._permission_checker_provider

    def _resolve_agent_settings(self) -> AgentSettings:
        if callable(self._agent_settings_provider):
            settings = self._agent_settings_provider()
            if isinstance(settings, AgentSettings):
                return settings
        if isinstance(self._agent_settings_provider, AgentSettings):
            return self._agent_settings_provider
        return AgentSettings(max_iterations=30, agent_mode="react")

    def _resolve_token_budget(self) -> TokenBudget:
        if callable(self._token_budget_provider):
            budget = self._token_budget_provider()
            if isinstance(budget, TokenBudget):
                return budget
        if isinstance(self._token_budget_provider, TokenBudget):
            return self._token_budget_provider
        return TokenBudget(total=64_000)

    @staticmethod
    def _build_permission_context(
        agent_type: str,
        parent_context: ToolExecutionContext | None,
        *,
        extra_deny_rules: list[str] | None = None,
    ) -> PermissionContext:
        return build_subagent_permission_context(
            agent_type, parent_context, extra_deny_rules=extra_deny_rules
        )

    @staticmethod
    def _is_recursive_subagent_call(context: ToolExecutionContext | None) -> bool:
        if context is None:
            return False
        permission = context.permission
        if permission.source.startswith("subagent:"):
            return True
        if "task" in permission.tool_deny_rules:
            return True
        return str(context.task_id or "").startswith("subagent-")

    @staticmethod
    def _build_subagent_prompt(agent_type: str, prompt: str) -> str:
        return build_subagent_prompt(agent_type, prompt, get_custom_agent=get_custom_agent)

    @staticmethod
    def _runtime_from_context(context: ToolExecutionContext | None):
        return runtime_from_context(context)

    @staticmethod
    def _metadata_from_context(context: ToolExecutionContext | None) -> dict[str, Any]:
        return metadata_from_context(context)

    @staticmethod
    def _build_subagent_context_builder(
        *,
        context: ToolExecutionContext | None,
        token_budget: TokenBudget,
        agent_settings: AgentSettings,
    ) -> ContextBuilder:
        # A worker cannot see the coordinator conversation. Its delegated
        # prompt is self-contained, while ContextBuilder still loads workspace
        # instructions normally. Copying history leaks sibling objectives.
        return ContextBuilder(
            token_budget=token_budget,
            agent_settings=agent_settings,
        )
