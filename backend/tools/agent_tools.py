"""Agent helper tools: user clarification, artifacts, and subagents."""

from __future__ import annotations

import asyncio
import inspect
import logging
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
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.state import AgentState
from backend.agents.loader import discover_agents, get_custom_agent
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import MAX_TOOL_RESULT_BYTES, BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.tools.agent_artifact_tools import ReadArtifactTool
from backend.tools.agent_user_tools import AskUserTool, BriefTool
from backend.tools.subagent_control_tools import TaskStatusTool, TaskStopTool
from backend.tools.subagent_catalog import (
    BUILTIN_AGENT_TYPES,
    agent_type_description,
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

_SUBAGENT_CAPACITY_MESSAGE = "Maximum concurrent subagents reached. Wait for a running task to finish."
# Pi's reference extension accepts eight parallel tasks and schedules four at
# a time. Keep the request limit separate from the runtime's global capacity so
# a single call can queue work instead of silently dropping tasks.
MAX_PARALLEL_TASKS = 8
MAX_PARALLEL_CONCURRENCY = 4
# Keep the subagent transport on the same inline contract as every other tool.
# The result pipeline owns persistence/truncation; this helper only provides a
# recoverable artifact for direct TaskTool callers that bypass that pipeline.
_SUBAGENT_RESULT_ARTIFACT_THRESHOLD_BYTES = MAX_TOOL_RESULT_BYTES

def _externalize_large_subagent_result(
    artifact_store: ArtifactStore,
    *,
    subagent_id: str,
    content: str,
) -> tuple[str, str]:
    """Keep large delegated reports out of the parent model context."""

    text = str(content or "")
    if len(text.encode("utf-8")) <= _SUBAGENT_RESULT_ARTIFACT_THRESHOLD_BYTES:
        return text, ""
    try:
        artifact_id = artifact_store.save(
            content=text,
            source=f"subagent:{subagent_id}",
            type="subagent_result",
        )
    except Exception as exc:
        logger.warning("large subagent result artifact save failed id=%s: %s", subagent_id, exc)
        return text, ""
    compact, _ = compact_subagent_result(text)
    return (
        "\n".join(
            (
                compact,
                "",
                "Full delegated result stored as artifact.",
                f"artifact_id: {artifact_id}",
                f"original_chars: {len(text)}",
                "Use read_artifact with this artifact_id only if the retained summary is insufficient.",
            )
        ).strip(),
        artifact_id,
    )


def _custom_agent_deny_rules(
    agent_type: str,
    tool_registry: Any | None,
    workspace_root: str | Path | None = None,
) -> list[str]:
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
        custom = get_custom_agent(agent_type, workspace_root)
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


def _user_visible_progress_text(value: Any) -> str:
    return str(value or "").strip()


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


def _prompt_scope_summary(prompt: str) -> str:
    """Compact user-facing scope label derived from the task prompt."""
    text = " ".join(str(prompt or "").split())
    return text if len(text) <= 120 else f"{text[:80]} … {text[-39:]}"


def _exclusive_parallel_task_scopes(tasks: list[dict[str, Any]]) -> list[str]:
    """Return explicit scope labels after checking structured write ownership.

    Natural-language similarity is deliberately irrelevant here: two workers
    may independently review the same problem.  Only overlapping write_scope
    paths are mechanically unsafe to schedule in parallel.
    """
    # Enforce write_scope exclusivity between sibling workers. Two parallel
    # workers whose write_scope shares a path would race on that file
    # (last-writer-wins) with no mutual exclusion, so reject the batch — the
    # caller surfaces this as "non-overlapping assignment" guidance.
    seen_write_paths: list[str] = []
    for task in tasks:
        raw_scope = task.get("write_scope")
        paths = raw_scope if isinstance(raw_scope, list) else []
        for path in paths:
            norm = str(path or "").strip().replace("\\", "/").strip("/").casefold()
            if not norm:
                continue
            if any(
                norm == existing
                or norm.startswith(f"{existing}/")
                or existing.startswith(f"{norm}/")
                for existing in seen_write_paths
            ):
                return []
            seen_write_paths.append(norm)

    scopes: list[str] = []
    for index, task in enumerate(tasks, 1):
        scope = str(
            task.get("description")
            or task.get("objective")
            or _prompt_scope_summary(str(task.get("prompt") or ""))
            or f"parallel task {index}"
        ).strip()
        scopes.append(scope)
    return scopes


def _parallel_undeclared_writers(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return write-capable parallel tasks that declare no write_scope.

    Two writers with no disjoint write_scope race on the same file
    (last-writer-wins) with no mutual exclusion, so they must not be scheduled
    concurrently. Read-only tasks (explore/plan or explicit read_only) may
    overlap freely — independent review of the same files is safe.
    """
    writers: list[dict[str, Any]] = []
    for task in tasks:
        if bool(task.get("read_only")):
            continue
        if str(task.get("agent_type") or "").lower() in {"explore", "plan"}:
            continue
        raw_scope = task.get("write_scope")
        paths = raw_scope if isinstance(raw_scope, list) else []
        declared = any(
            str(path or "").strip().replace("\\", "/").strip("/")
            for path in paths
        )
        if not declared:
            writers.append(task)
    return writers


def _available_agent_types() -> list[str]:
    """Return built-in plus discovered custom subagent types for model schema."""
    return available_agent_types(discover_agents)


def _resolve_subagent_llm(
    inherited_llm: Any,
    *,
    agent_type: str,
    model_override: str = "",
    effort_override: str = "",
    workspace_root: str | Path | None = None,
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
    custom = get_custom_agent(agent_type, workspace_root) if agent_type else None

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
        "cancel_with_parent": cancel_with_parent,
        "detach_from_parent": detach_from_parent,
        "read_only": _bool_field(raw.get("read_only"), False),
        "write_scope": _string_list(raw.get("write_scope")),
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
    # Delegation is a core execution capability. Hiding it behind tool_search
    # makes availability depend on discovery rather than the model's decision.
    should_defer = False
    result_kind = "subagent"
    activity_kind = "genericTool"
    display_label = "Start subagent"
    # Pi and Claude Code do not impose an invented per-delegation wall-clock
    # cutoff.  The enclosing turn/agent lifecycle owns any configured deadline.
    timeout_seconds = None
    # Pi caps each delegated result independently at 50 KiB. A parallel batch
    # may therefore legitimately exceed the generic single-result backstop.
    max_result_chars = None
    description = (
        "Delegate a sub-task to an independent agent. The sub-agent has its own context and tool access. "
        "Use only for broad, complex, independent work. Read or search a known file directly instead. "
        "Supports parallel sub-tasks via the parallel_tasks parameter (up to 8 tasks, with four running at once). "
        "The tool returns the sub-agent's final response and terminal status."
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
        )

    def model_description(self) -> str:
        return (
            "Delegate independent work to a subagent. When two or more independent tasks are known, "
            "use one parallel_tasks call so they start together. "
            "Prefer it for bounded work that benefits from independent context. "
            "Use explore for read-only code or web research and plan for planning work. "
            "Give implementation agents specific ownership: name the files or modules they own and the exact change. "
            "Do not delegate synthesis with vague instructions such as 'based on your findings, fix it'; "
            "understand the findings first, then delegate a concrete change. "
            "The call waits for its result by default. Set run_in_background=true only when the parent can "
            "continue without the result; background completion is delivered later. "
            "While a background agent is running, do not read its transcript/output file or predict its result. "
            "If asked before completion, report that it is still running; give status, not a guess. "
            "Use task_status to inspect, send_message to steer it, and task_stop to cancel it."
        )

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        """Allow separate same-turn read-only delegations to start together.

        The normal tool batcher serializes side-effecting tools. Read-only
        workers have isolated contexts and cannot mutate the workspace, so
        serializing them only delays the second spawn until the first worker
        finishes. Write-capable delegations keep
        the conservative serial behavior unless the caller uses parallel_tasks,
        whose scope validation is owned by this tool.
        """
        call_args = args if isinstance(args, dict) else {}
        if isinstance(call_args.get("parallel_tasks"), list):
            return False
        if bool(call_args.get("read_only")):
            return True
        return str(call_args.get("agent_type") or "").strip().lower() in {
            "explore",
            "plan",
        }

    def model_schema(self) -> ToolSchema:
        agent_types = _available_agent_types()
        agent_type_help = agent_type_description(
            agent_types,
            get_custom_agent=get_custom_agent,
        )
        parallel_task_schema = {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short label for this independent subtask.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete self-contained instructions for this subtask.",
                },
                "agent_type": {
                    "type": "string",
                    "enum": agent_types,
                    "description": agent_type_help,
                },
                "read_only": {
                    "type": "boolean",
                    "description": "Whether this subtask must avoid writes.",
                },
            },
            "required": ["description", "prompt"],
        }
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
                        "description": agent_type_help,
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model override for this subagent. 'inherit' or omitted "
                            "keeps the session model."
                        ),
                    },
                    "effort": {
                        "type": "string",
                        "description": "Optional reasoning effort override (low/medium/high).",
                    },
                    "parallel_tasks": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": MAX_PARALLEL_TASKS,
                        "items": parallel_task_schema,
                        "description": (
                            "Run 2-8 bounded subtasks in one call; at most four run concurrently and the call waits for their results. "
                            "Set run_in_background=true only when the parent can continue without them."
                        ),
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
                    "cancel_with_parent": {
                        "type": "boolean",
                        "description": "Whether parent cancel should also cancel this subagent.",
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
                },
                "anyOf": [
                    {"required": ["description", "prompt"]},
                    {"required": ["parallel_tasks"]},
                ],
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
        agent_type_help = agent_type_description(
            agent_types,
            get_custom_agent=get_custom_agent,
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
                        "description": agent_type_help,
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
                        "minItems": 2,
                        "maxItems": MAX_PARALLEL_TASKS,
                        "description": (
                            "Run 2-8 subtasks in one call. At most four run concurrently. Each item is an object with "
                            "'description', 'prompt', and optional 'agent_type'. Each subtask "
                            "must cover one concrete scope. Read-only scopes may overlap for independent review; "
                            "write-capable tasks need explicit, disjoint write_scope paths. "
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
                                    "description": agent_type_help,
                                },
                                "model": {
                                    "type": "string",
                                    "description": "Optional model override for this subtask ('inherit'/omit keeps session model).",
                                },
                                "effort": {
                                    "type": "string",
                                    "description": "Optional reasoning-effort override for this subtask (reasoning-capable providers).",
                                },
                                "cancel_with_parent": {
                                    "type": "boolean",
                                    "description": "Whether parent cancel should also cancel this subtask.",
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
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Return immediately with subagent ids instead of waiting for results. "
                            "Background completion is delivered later; use task_stop to cancel it."
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
                    "cancel_with_parent": {
                        "type": "boolean",
                        "description": "Whether parent cancel should also cancel this subagent.",
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
        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()
        if llm is None or tool_registry is None or permission_checker is None:
            return self._error_result("Subagent runtime is not configured")
        runtime = self._runtime_from_context(context) or default_runtime()
        parent_run_id = str(self._metadata_from_context(context).get("run_id") or "").strip()

        # ── Parallel execution path ──
        if isinstance(parallel_tasks, list) and len(parallel_tasks) > MAX_PARALLEL_TASKS:
            return self._error_result(
                f"Too many parallel tasks ({len(parallel_tasks)}). Max is {MAX_PARALLEL_TASKS}."
            )
        if isinstance(parallel_tasks, list) and len(parallel_tasks) >= 2:
            workspace_root = context.workspace_root if context is not None else None
            resolve_custom_agent = lambda name: get_custom_agent(name, workspace_root)
            tasks: list[dict[str, Any]] = []
            for item in parallel_tasks:
                if not isinstance(item, dict):
                    continue
                t_desc = str(item.get("description") or "").strip()
                t_prompt = str(item.get("prompt") or "").strip()
                t_type = str(item.get("agent_type") or "general-purpose").strip().lower()
                if t_desc and t_prompt:
                    tasks.append({
                        "description": t_desc,
                        "prompt": t_prompt,
                        "agent_type": normalize_agent_type(
                            t_type,
                            get_custom_agent=resolve_custom_agent,
                        ),
                        **_nonempty_subagent_metadata(item),
                    })
                    if tasks[-1]["agent_type"] in {"explore", "plan"}:
                        tasks[-1]["read_only"] = True
            if len(tasks) >= 2:
                scopes = _exclusive_parallel_task_scopes(tasks)
                if len(scopes) != len(tasks):
                    return self._error_result(
                        "Parallel write-capable tasks have overlapping explicit write_scope paths. "
                        "Give each worker a disjoint write_scope or run those mutations sequentially."
                    )
                undeclared_writers = _parallel_undeclared_writers(tasks)
                if undeclared_writers:
                    names = ", ".join(
                        f"'{task.get('description') or task.get('prompt', '')[:40]}'"
                        for task in undeclared_writers
                    )
                    return self._error_result(
                        "Parallel write-capable task(s) declare no write_scope: "
                        f"{names}. Two writers without disjoint write_scope would race on "
                        "the same file (last-writer-wins). Give each write-capable task an "
                        "explicit disjoint write_scope, or run the writes sequentially."
                    )
                if bool(args.get("run_in_background")):
                    return await self._start_background_subtasks(
                        tasks=tasks,
                        context=context,
                    )
                return await self._run_parallel_subtasks(tasks, context)

        # ── Single execution path ──
        prompt = str(args.get("prompt") or "").strip()
        isolation = str(args.get("isolation") or "").strip().lower()
        if isolation and isolation != "worktree":
            return self._error_result(
                f"Unsupported isolation mode: {isolation!r}. Only 'worktree' is supported."
            )
        workspace_root = context.workspace_root if context is not None else None
        agent_type = normalize_agent_type(
            str(args.get("agent_type") or "general-purpose"),
            get_custom_agent=lambda name: get_custom_agent(name, workspace_root),
        )
        if agent_type in {"explore", "plan"} and not bool(args.get("read_only")):
            args = {**args, "read_only": True}
        if not description:
            return self._error_result("Missing description argument")
        if not prompt:
            return self._error_result("Missing prompt argument")
        if bool(args.get("run_in_background")):
            return await self._start_background_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_metadata=_subagent_metadata(args),
            )

        return await self._run_foreground_subtask(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            context=context,
            subagent_metadata=_subagent_metadata(args),
        )

    # ------------------------------------------------------------------
    # Single subtask execution
    # ------------------------------------------------------------------

    async def _run_foreground_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        subagent_metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Wait for a Pi worker slot before starting one foreground task."""

        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_id = f"subagent-{uuid4().hex[:8]}"
        acquired = await runtime.acquire_subagent_slot(
            subagent_id,
            cancel_event=context.cancel_event if context is not None else None,
        )
        if not acquired:
            return ToolResult(
                content="Subagent was cancelled before it started.",
                is_error=False,
                status="cancelled",
                display_summary="Subagent cancelled before start",
                result_kind="subagent",
            )
        try:
            return await self._run_single_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_id=subagent_id,
                subagent_metadata=subagent_metadata,
            )
        finally:
            # start_subagent consumes the reservation. Releasing here is still
            # required for failures before start and wakes any capacity waiters
            # after this foreground worker becomes terminal.
            runtime.release_subagent_slot(subagent_id)

    async def _start_background_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        subagent_metadata: dict[str, Any] | None = None,
        subagent_id: str | None = None,
    ) -> ToolResult:
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        subagent_id = self._spawn_background_subtask(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            context=context,
            subagent_metadata=subagent_metadata,
            subagent_id=subagent_id,
            wait_for_slot=True,
        )
        await asyncio.sleep(0)

        return ToolResult(
            content=(
                f"Started background subagent {subagent_id} ({agent_type}). "
                "It will report progress through subagent events and its completion is automatically injected "
                "into the parent at the next safe loop boundary. "
                "The background agent is independent of this turn. Its completion will be reported through "
                "subagent events; use task_status to inspect it or task_stop to cancel it. "
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
    ) -> ToolResult:
        subagent_ids = [f"subagent-{uuid4().hex[:8]}" for _ in tasks]
        started: list[tuple[str, dict[str, str]]] = []
        try:
            for subagent_id, task in zip(subagent_ids, tasks, strict=True):
                self._spawn_background_subtask(
                    description=task["description"],
                    prompt=task["prompt"],
                    agent_type=task.get("agent_type", "general-purpose"),
                    context=context,
                    subagent_metadata=_subagent_metadata(task),
                    subagent_id=subagent_id,
                    wait_for_slot=True,
                )
                started.append((subagent_id, task))
        except Exception:
            raise
        await asyncio.sleep(0)
        lines = [
            f"Queued {len(started)} background subagents; up to four run concurrently.",
            "Their completions are reported through subagent events; use task_status to inspect one or task_stop to cancel it.",
        ]
        for index, (subagent_id, task) in enumerate(started, 1):
            lines.append(f"{index}. {subagent_id} ({task.get('agent_type', 'general-purpose')}): {task['description']}")
        return ToolResult(
            content="\n".join(lines),
            display_summary=f"{len(started)} subagents queued/running",
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
        subagent_metadata: dict[str, Any] | None = None,
        subagent_id: str | None = None,
        wait_for_slot: bool = False,
    ) -> str:
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_cancel_event = asyncio.Event()

        async def _run_background() -> ToolResult:
            if wait_for_slot:
                acquired = await runtime.acquire_subagent_slot(
                    subagent_id,
                    cancel_event=subagent_cancel_event,
                )
                if not acquired:
                    return ToolResult(
                        content=f"Background subagent {subagent_id} was cancelled before it started.",
                        is_error=False,
                        status="cancelled",
                        display_summary=f"Subagent cancelled: {description[:60]}",
                        result_kind="subagent",
                    )
                runtime.mark_subagent_task_running(subagent_id)
            return await self._run_single_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_id=subagent_id,
                cancel_event=subagent_cancel_event,
                subagent_metadata=subagent_metadata,
                background=True,
            )

        task = asyncio.create_task(
            _run_background()
        )
        parent_metadata = metadata_from_context(context)
        parent_run_id = str(parent_metadata.get("run_id", "")).strip()
        runtime.register_subagent_task(
            subagent_id,
            task,
            cancel_event=subagent_cancel_event,
            parent_run_id=parent_run_id,
            owner_task_id=str(getattr(context, "task_id", "") or ""),
            session_id=str(getattr(context, "session_id", "") or ""),
            agent_type=agent_type,
            prompt_summary=description,
            background=True,
            pending=wait_for_slot,
        )

        def _release_background_task(done_task: asyncio.Task[ToolResult]) -> None:
            runtime.release_subagent_task(subagent_id, expected_task=done_task)
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

    async def _run_single_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        subtask_index: int | None = None,
        total_subtasks: int | None = None,
        subagent_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        subagent_metadata: dict[str, Any] | None = None,
        background: bool = False,
    ) -> ToolResult:
        """Run one isolated subagent loop with progress reporting.

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
        subagent_config = _subagent_metadata(subagent_metadata)
        # Honor a per-subagent model/effort override (from the task-call args or
        # the custom agent definition). When present this replaces the inherited
        # session adapter with a fresh one; otherwise ``llm`` is unchanged.
        llm = _resolve_subagent_llm(
            llm,
            agent_type=agent_type,
            model_override=subagent_config.get("model", ""),
            effort_override=subagent_config.get("effort", ""),
            workspace_root=context.workspace_root if context is not None else None,
        )
        # A child owns its cancellation signal. Reusing the parent's event lets a
        # child deadline cancel the parent and every sibling sharing that context.
        subagent_cancel_event = cancel_event or asyncio.Event()
        parent_run_id = str(parent_metadata.get("run_id", ""))
        try:
            # Explicit background work is detached by default, matching CC's
            # unlinked async agent controller. Explicit flags always win.
            cancel_with_parent = subagent_config.get("cancel_with_parent")
            detach_from_parent = subagent_config.get("detach_from_parent")
            if (
                background
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
                cancel_with_parent=cancel_with_parent,
                detach_from_parent=detach_from_parent,
                read_only=subagent_config["read_only"],
                write_scope=subagent_config["write_scope"],
            ) if runtime is not None else None
        except RuntimeError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                status="blocked",
                display_summary="Subagent capacity reached",
                result_kind="subagent",
            )

        subagent_fence = {
            "agent_path": str(getattr(subagent_record, "agent_path", "") or ""),
            "mailbox_epoch": int(getattr(subagent_record, "mailbox_epoch", 0) or 0),
        }

        def _accepts_current_incarnation(*, require_running: bool = True) -> bool:
            if runtime is None:
                return True
            return runtime.accepts_subagent_incarnation(
                subagent_id,
                require_running=require_running,
                **subagent_fence,
            )

        async def _emit_incarnation_event(
            event_type: str,
            data: dict[str, Any],
            *,
            require_running: bool = True,
        ) -> bool:
            if emit_event is None or not _accepts_current_incarnation(
                require_running=require_running
            ):
                return False
            payload = {**data, **subagent_fence}
            await emit_event(event_type, payload)
            return True

        # ── Worktree isolation (cc AgentTool isolation: "worktree") ──
        # Created after the capacity check so a blocked delegation never leaves
        # a stray worktree behind. Isolation is fail-closed: a caller that asks
        # for a worktree must never silently run in the shared workspace.
        agent_worktree = None
        worktree_fallback_note = ""
        parent_workspace_root = (
            Path(context.workspace_root)
            if context is not None and context.workspace_root
            else Path.cwd()
        )
        if subagent_config.get("isolation") == "worktree":
            from backend.agent.worktree import (
                cleanup_stale_worktrees,
                create_agent_worktree,
            )

            # First delegation per git root sweeps orphaned worktrees left by a
            # killed process (clean ones removed, changed ones kept). Best-effort.
            await asyncio.to_thread(cleanup_stale_worktrees, parent_workspace_root)
            agent_worktree, worktree_reason = await asyncio.to_thread(
                create_agent_worktree, subagent_id, parent_workspace_root
            )
            if agent_worktree is None:
                logger.warning(
                    "Worktree isolation failed for %s: %s", subagent_id, worktree_reason
                )
                if runtime is not None:
                    runtime.complete_subagent(
                        subagent_id,
                        "failed",
                        summary=f"Worktree isolation failed: {worktree_reason}",
                        **subagent_fence,
                    )
                return ToolResult(
                    content=f"Worktree isolation failed: {worktree_reason}",
                    is_error=True,
                    status="failed",
                    display_summary="Worktree isolation failed",
                    result_kind="subagent",
                )

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
                current_activity=description,
                waiting_on="starting",
                last_progress_at=int(time.time() * 1000),
                **subagent_fence,
            )
            start_event.data.update(_nonempty_subagent_metadata(subagent_config))
            if subagent_record is not None:
                start_event.data["record"] = subagent_record.to_dict()
                start_event.data["parent_run_id"] = parent_run_id
            await _emit_incarnation_event("subagent.start", start_event.data)
        await _run_task_created_hook(
            task_id=subagent_id,
            subject=description,
            description=prompt,
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
                        "cancel_with_parent": bool(
                            getattr(subagent_record, "cancel_with_parent", True)
                            if subagent_record is not None
                            else subagent_config.get("cancel_with_parent", True)
                        ),
                        "detach_from_parent": bool(
                            getattr(subagent_record, "detach_from_parent", False)
                            if subagent_record is not None
                            else subagent_config.get("detach_from_parent", False)
                        ),
                    },
                )
            except Exception as journal_exc:
                logger.debug("Failed opening subagent journal for %s: %s", subagent_id, journal_exc)
                journal = None

        sub_settings = self._resolve_agent_settings()
        sub_budget = self._resolve_token_budget()
        # Apply a custom agent's tool restrictions (Agent editor). A custom
        # definition can declare a tools whitelist and/or disallowed_tools; those
        # must actually be enforced at runtime (deny rules block the call), not
        # just stored on the definition. model override still inherits the
        # session LLM (rebuilding an adapter per subagent is a separate change).
        custom_deny_rules = _custom_agent_deny_rules(
            agent_type,
            tool_registry,
            context.workspace_root if context is not None else None,
        )
        sub_context = self._build_permission_context(
            agent_type,
            context,
            read_only=subagent_config["read_only"],
            extra_deny_rules=custom_deny_rules,
        )
        delegated_prompt = self._build_subagent_prompt(
            agent_type,
            prompt,
            workspace_root=context.workspace_root if context is not None else None,
        )
        sub_state = AgentState(user_message=delegated_prompt, max_iterations=sub_settings.max_iterations)
        # Subagents cannot delegate; the prompt builder drops the delegation
        # section so the system prompt matches the denied toolset.
        sub_state.prompt_context["subagent"] = agent_type

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
            **subagent_fence,
            "cancel_event": subagent_cancel_event,
            **_nonempty_subagent_metadata(subagent_config),
        }
        if background:
            request_metadata = subagent_metadata_payload.get("llm_request_metadata")
            if not isinstance(request_metadata, dict):
                request_metadata = {}
            subagent_metadata_payload["llm_request_metadata"] = {
                **request_metadata,
                "prompt_cache_skip_write": True,
            }
        rollout_budget = parent_metadata.get("_rollout_budget")
        if rollout_budget is not None:
            subagent_metadata_payload["_rollout_budget"] = rollout_budget
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
        last_tool_name = ""
        terminal_status = "completed"
        terminal_reason = ""
        terminal_usage: dict[str, Any] = {}
        terminal_provider_raw: dict[str, Any] = {}
        last_error = ""
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

        def _terminal_result_payload(
            *,
            status: str,
            content: str,
            error: str = "",
            reason: str = "",
        ) -> dict[str, Any]:
            """Match the durable SubagentResultRecord shape for every transport."""
            return {
                "subagent_id": subagent_id,
                "status": status,
                "content": content,
                "error": error,
                "duration_ms": elapsed_ms,
                "iterations": sub_state.iterations,
                "tool_call_count": len(sub_state.tool_calls),
                "terminal_reason": reason or status,
                "input_tokens": int(terminal_usage.get("input_tokens") or 0),
                "output_tokens": int(terminal_usage.get("output_tokens") or 0),
                "total_tokens": (
                    int(terminal_usage.get("input_tokens") or 0)
                    + int(terminal_usage.get("output_tokens") or 0)
                    + int(terminal_usage.get("reasoning_output_tokens") or 0)
                ),
            }

        sub_context_builder = self._build_subagent_context_builder(
            context=context,
            token_budget=sub_budget,
            agent_settings=sub_settings,
            llm=llm,
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

            def _parent_turn_deadline() -> float | None:
                """Absolute monotonic deadline of the parent turn, if it has one."""
                raw = getattr(context, "deadline_monotonic", None)
                return None if raw is None else float(raw)

            def _parent_deadline_wait() -> asyncio.Task[None] | None:
                """Task that completes when the parent turn runs out of time."""
                deadline = _parent_turn_deadline()
                if deadline is None:
                    return None
                return asyncio.create_task(
                    asyncio.sleep(max(0.0, deadline - time.monotonic()))
                )

            def _parent_deadline_rejection() -> dict[str, str]:
                return {
                    "action": "reject",
                    "guidance": (
                        f"Subagent {subagent_id} could not obtain approval because the "
                        "parent turn's time budget expired."
                    ),
                }

            async def _await_parent_decision(parent_tool_call_id: str) -> dict[str, str] | None:
                """Forward one approval to the parent and wait within its turn budget.

                Returns the parent's decision, or ``None`` when the parent turn
                ran out of time first — the caller turns that into a rejection.
                The parent's own approval timeout can outlive the turn budget, so
                a decision that arrives after the deadline can no longer be acted
                on and must not pin the child.
                """
                response = parent_approval_handler(parent_tool_call_id)
                if not inspect.isawaitable(response):
                    return response if isinstance(response, dict) else None

                answer_task = asyncio.ensure_future(response)
                deadline_wait = _parent_deadline_wait()
                waiters: set[asyncio.Future[Any]] = {answer_task}
                if deadline_wait is not None:
                    waiters.add(deadline_wait)
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if deadline_wait is not None and not deadline_wait.done():
                        deadline_wait.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await deadline_wait
                if not answer_task.done():
                    # Abandon rather than await: a parent handler is free to
                    # swallow cancellation and keep waiting for its own timeout,
                    # and the child must not be pinned to that. Consume the
                    # eventual result so the loop does not log it as unretrieved.
                    answer_task.cancel()
                    answer_task.add_done_callback(
                        lambda finished: finished.cancelled() or finished.exception()
                    )
                    return None
                decision = answer_task.result()
                return decision if isinstance(decision, dict) else None

            async def subagent_approval_handler(tool_call_id: str) -> dict[str, str]:
                local_tool_call_id = str(tool_call_id or "").strip()
                if can_forward_approval and parent_approval_handler is not None:
                    # Child providers commonly reuse short ids such as call_1.
                    # Namespace the id before it enters the parent WebSocket
                    # approval map so parallel children cannot overwrite one
                    # another's pending Future.
                    parent_tool_call_id = f"{subagent_id}:{local_tool_call_id}"
                    ready = approval_ready.setdefault(local_tool_call_id, asyncio.Event())
                    # A child approval only reaches the user while this child owns
                    # the current incarnation, and only while the parent turn still
                    # has time to act on it. Waiting on `ready` alone would hang
                    # past both boundaries, so bound the wait by cancellation and
                    # by the parent's absolute deadline.
                    parent_deadline = _parent_turn_deadline()
                    deadline_expired = False
                    ready_wait = asyncio.create_task(ready.wait())
                    cancel_wait = asyncio.create_task(subagent_cancel_event.wait())
                    try:
                        wait_timeout: float | None = None
                        if parent_deadline is not None:
                            wait_timeout = max(0.0, float(parent_deadline) - time.monotonic())
                        done_waits, _ = await asyncio.wait(
                            {ready_wait, cancel_wait},
                            timeout=wait_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        deadline_expired = not done_waits and wait_timeout is not None
                    finally:
                        for waiter in (ready_wait, cancel_wait):
                            if not waiter.done():
                                waiter.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await asyncio.gather(
                                ready_wait, cancel_wait, return_exceptions=True
                            )
                    if deadline_expired:
                        return _parent_deadline_rejection()
                    if not ready.is_set():
                        return {
                            "action": "reject",
                            "guidance": (
                                f"Subagent {subagent_id} was cancelled before its approval "
                                "request reached the user."
                            ),
                        }
                    pending_approval_ids.add(local_tool_call_id)
                    try:
                        decision = await _await_parent_decision(parent_tool_call_id)
                        if decision is None:
                            return _parent_deadline_rejection()
                        return decision
                    finally:
                        pending_approval_ids.discard(local_tool_call_id)
                return {
                    "action": "reject",
                    "guidance": (
                        f"Subagent {subagent_id} cannot request user approvals directly. "
                        "Return a summary and let the main agent decide the next action."
                    ),
                }

            async def subagent_event_bridge(event_type: str, data: dict[str, Any]) -> None:
                if event_type not in {"tool_call", "agent.progress"}:
                    return
                if not _accepts_current_incarnation():
                    return
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
                    last_progress_at=int(time.time() * 1000),
                    activity_kind="tool" if event_type == "tool_call" else "status",
                    activity_summary=current_activity,
                    user_visible=bool(current_activity),
                    **subagent_fence,
                )
                progress_event.data["source_event_type"] = event_type
                if tool_call_id:
                    progress_event.data["tool_call_id"] = tool_call_id
                await _emit_incarnation_event("subagent.progress", progress_event.data)

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
                    item = await event_queue.get()
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    event = item
                    if event.type == "item.completed":
                        message_item = (
                            event.data.get("item")
                            if isinstance(event.data.get("item"), dict)
                            else {}
                        )
                        if message_item.get("type") == "agent_message":
                            content = str(message_item.get("text") or "")
                            if content:
                                summary_parts[:] = [content]
                    elif event.type == "approval_request":
                        if (
                            can_forward_approval
                            and emit_event is not None
                            and _accepts_current_incarnation()
                        ):
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
                        if journal is not None:
                            try:
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
                    elif event.type == "agent.item" and event.data.get("kind") == "process_text":
                        # Forward explicit model commentary to the Agents panel.
                        # Raw provider reasoning uses ``thinking_delta`` and is
                        # intentionally excluded. Previously child progress only
                        # exposed tool names, so a research/implementation agent
                        # could work for minutes with no intermediate explanation.
                        process_text = _user_visible_progress_text(
                            event.data.get("content")
                            or event.data.get("summary")
                            or event.data.get("title")
                        )
                        if process_text and emit_event is not None:
                            progress_event = AgentEvent.subagent_progress(
                                subagent_id=subagent_id,
                                iteration=sub_state.iterations,
                                max_iterations=sub_settings.max_iterations,
                                tool_name="",
                                detail=process_text,
                                current_activity=process_text,
                                waiting_on="model",
                                last_progress_at=int(time.time() * 1000),
                                activity_kind="narration",
                                activity_summary=process_text,
                                user_visible=True,
                                **subagent_fence,
                            )
                            progress_event.data["source_event_type"] = "agent.item"
                            item_id = str(event.data.get("item_id") or event.data.get("id") or "")
                            if item_id:
                                progress_event.data["item_id"] = item_id
                            await _emit_incarnation_event(
                                "subagent.progress", progress_event.data
                            )
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
                        await _emit_incarnation_event(
                            "subagent.progress",
                            AgentEvent.subagent_progress(
                                subagent_id=subagent_id,
                                iteration=sub_state.iterations,
                                max_iterations=sub_settings.max_iterations,
                                tool_name=last_tool_name,
                                detail="",
                                current_activity=description,
                                waiting_on="tool",
                                last_progress_at=int(time.time() * 1000),
                                activity_kind="tool_result",
                                activity_summary=description,
                                user_visible=bool(description),
                                **subagent_fence,
                            ).data,
                        )
            finally:
                if not pump_task.done():
                    pump_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await pump_task

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            streamed_summary = "".join(summary_parts).strip()
            summary = streamed_summary or sub_state.reply.strip()
            if not summary and terminal_status == "completed":
                terminal_status = "failed"
                terminal_reason = terminal_reason or "missing_final_summary"
                last_error = "Subagent ended without a final response."
            if terminal_usage:
                if rollout_budget is not None:
                    rollout_budget.record_usage_total(subagent_id, terminal_usage)
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
                    input_includes_cache_read=bool(
                        terminal_usage.get("input_includes_cache_read", True)
                    ),
                )
            display_summary = _subagent_display_summary(summary)
            tool_call_count = len(sub_state.tool_calls)

            if terminal_status == "cancelled":
                raise asyncio.CancelledError
            if terminal_status == "failed":
                raise RuntimeError(last_error or terminal_reason or "Subagent run failed")

            result_status = "partial" if terminal_status == "partial" else "completed"
            result_text = summary
            worktree_note = await _cleanup_worktree()
            for note in (worktree_note, worktree_fallback_note):
                if note:
                    result_text += f"\n\n{note}"
            full_result_text = result_text
            result_text, result_artifact_id = _externalize_large_subagent_result(
                self._artifact_store,
                subagent_id=subagent_id,
                content=result_text,
            )
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
                },
            )
            if runtime is not None:
                result_record = runtime.store_subagent_result(
                    subagent_id,
                    status=result_status,
                    content=full_result_text,
                    artifact_id=result_artifact_id,
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    terminal_reason=terminal_reason or result_status,
                    usage=terminal_usage,
                    **subagent_fence,
                )
                completed_record = runtime.complete_subagent(
                    subagent_id,
                    result_status,
                    summary=display_summary,
                    tool_count=tool_call_count,
                    **subagent_fence,
                )
                if result_record is None or completed_record is None:
                    return ToolResult(
                        content=f"Discarded stale completion for subagent {subagent_id}.",
                        is_error=True,
                        status="cancelled",
                        display_summary="Stale subagent completion discarded",
                        result_kind="subagent",
                    )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    summary=display_summary,
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    status=result_status,
                    termination_reason=terminal_reason or (
                        "success" if result_status == "completed" else "partial"
                    ),
                    initiator="runtime",
                    usage=terminal_usage,
                    **subagent_fence,
                )
                done_event.data["result"] = (
                    {**result_record.to_dict(), "content": result_text}
                    if result_record is not None
                    else _terminal_result_payload(
                        status=result_status,
                        content=result_text,
                        reason=terminal_reason,
                    )
                )
                if result_artifact_id:
                    done_event.data["artifact_id"] = result_artifact_id
                if completed_record is not None:
                    done_event.data["record"] = completed_record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await _emit_incarnation_event(
                    "subagent.done", done_event.data, require_running=False
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
                artifact_id=result_artifact_id or None,
            )
        except asyncio.CancelledError:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            record = None
            retained = "".join(summary_parts).strip()
            cancel_summary = retained
            with suppress(Exception):
                worktree_note = await _cleanup_worktree()
                if worktree_note:
                    logger.info("Subagent %s cleanup: %s", subagent_id, worktree_note)
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
                    content=retained,
                    error="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                    terminal_reason="cancelled",
                    usage=terminal_usage,
                    **subagent_fence,
                )
                record = runtime.complete_subagent(
                    subagent_id,
                    "cancelled",
                    summary="cancelled",
                    tool_count=len(sub_state.tool_calls),
                    **subagent_fence,
                )
                if record is None:
                    raise
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error="cancelled",
                    status="cancelled",
                    termination_reason="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    usage=terminal_usage,
                    **subagent_fence,
                )
                if record is not None:
                    done_event.data["record"] = record.to_dict()
                done_event.data["result"] = _terminal_result_payload(
                    status="cancelled",
                    content=retained,
                    error="cancelled",
                    reason="cancelled",
                )
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await _emit_incarnation_event(
                    "subagent.done", done_event.data, require_running=False
                )
            await _run_subagent_stop_hook(
                subagent_id,
                "cancelled",
                retained,
                agent_type=agent_type,
            )
            await _run_task_completed_hook(
                task_id=subagent_id,
                subject=description,
                description=retained,
                teammate_name=agent_type,
            )
            await _run_teammate_idle_hook(teammate_name=agent_type)
            raise
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            partial_text = "".join(summary_parts).strip()
            has_partial_output = bool(partial_text)
            failure_note = (
                f"Subagent {subagent_id} ({agent_type}) did not finish: "
                f"{type(exc).__name__}: {exc}"
            )
            if has_partial_output:
                error_content = failure_note
                if partial_text:
                    error_content += f"\n\nPartial output before the failure:\n{partial_text}"
            else:
                error_content = (
                    f"Subagent {subagent_id} ({agent_type}) failed after "
                    f"{elapsed_ms}ms and {sub_state.iterations} iteration(s).\n"
                    f"Error: {type(exc).__name__}: {exc}"
                )
            failure_status = "partial" if has_partial_output else "failed"
            with suppress(Exception):
                worktree_note = await _cleanup_worktree()
                if worktree_note:
                    error_content += f"\n{worktree_note}"
            record = None
            _close_journal(
                status=failure_status,
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
                    status=failure_status,
                    content=partial_text,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                    terminal_reason=terminal_reason or type(exc).__name__,
                    usage=terminal_usage,
                    **subagent_fence,
                )
                record = runtime.complete_subagent(
                    subagent_id,
                    failure_status,
                    summary=str(exc),
                    tool_count=len(sub_state.tool_calls),
                    **subagent_fence,
                )
                if record is None:
                    return ToolResult(
                        content=f"Discarded stale failure for subagent {subagent_id}.",
                        is_error=True,
                        status="cancelled",
                        display_summary="Stale subagent failure discarded",
                        result_kind="subagent",
                    )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    summary=_subagent_display_summary(partial_text),
                    error=str(exc),
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                    status=failure_status,
                    termination_reason=terminal_reason or type(exc).__name__,
                    usage=terminal_usage,
                    **subagent_fence,
                )
                if record is not None:
                    done_event.data["record"] = record.to_dict()
                done_event.data["result"] = _terminal_result_payload(
                    status=failure_status,
                    content=partial_text,
                    error=f"{type(exc).__name__}: {exc}",
                    reason=terminal_reason or type(exc).__name__,
                )
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await _emit_incarnation_event(
                    "subagent.done", done_event.data, require_running=False
                )
            await _run_subagent_stop_hook(subagent_id, failure_status, error_content, agent_type=agent_type)
            await _run_task_completed_hook(
                task_id=subagent_id,
                subject=description,
                description=error_content,
                teammate_name=agent_type,
            )
            await _run_teammate_idle_hook(teammate_name=agent_type)
            return ToolResult(
                content=error_content,
                is_error=not has_partial_output,
                status=failure_status,
                duration_ms=elapsed_ms,
                display_summary=(
                    f"Subagent unfinished: {description[:60]}"
                    if has_partial_output
                    else f"Subagent failed: {description[:60]}"
                ),
                result_kind="subagent",
            )

    # ------------------------------------------------------------------
    # Parallel subtask execution
    # ------------------------------------------------------------------

    async def _run_parallel_subtasks(
        self,
        tasks: list[dict[str, Any]],
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        """Run multiple independent subtasks concurrently."""
        total = len(tasks)
        start_time = time.perf_counter()
        runtime = self._runtime_from_context(context) or default_runtime()
        subagent_ids = [f"subagent-{uuid4().hex[:8]}" for _ in tasks]
        results: list[ToolResult | Exception | None] = [None] * total
        next_index = 0
        index_lock = asyncio.Lock()

        async def run_next() -> None:
            nonlocal next_index
            while True:
                async with index_lock:
                    if next_index >= total:
                        return
                    index = next_index
                    next_index += 1
                subagent_id = subagent_ids[index]
                if not await runtime.acquire_subagent_slot(subagent_id):
                    results[index] = ToolResult(
                        content=_SUBAGENT_CAPACITY_MESSAGE,
                        is_error=True,
                        status="blocked",
                        display_summary="Subagent capacity reached",
                        result_kind="subagent",
                    )
                    continue
                try:
                    results[index] = await self._run_single_subtask(
                        description=tasks[index]["description"],
                        prompt=tasks[index]["prompt"],
                        agent_type=tasks[index].get("agent_type", "general-purpose"),
                        context=context,
                        subtask_index=index,
                        total_subtasks=total,
                        subagent_id=subagent_id,
                        subagent_metadata=_subagent_metadata(tasks[index]),
                    )
                except Exception as exc:  # noqa: BLE001
                    results[index] = exc
                finally:
                    # start_subagent consumes a reservation; this is a no-op
                    # after a normal run and releases it on early failure.
                    runtime.release_subagent_slot(subagent_id)

        worker_count = min(MAX_PARALLEL_CONCURRENCY, total)
        await asyncio.gather(*(run_next() for _ in range(worker_count)))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        parts: list[str] = []
        has_error = False
        for i, (task, result) in enumerate(zip(tasks, results), 1):
            heading = f"[{i}/{total}] {task['description']}"
            if isinstance(result, Exception):
                has_error = True
                parts.append(f"{heading}\nError: {result}")
            elif isinstance(result, ToolResult):
                if result.is_error:
                    has_error = True
                parts.append(f"{heading}\n{result.content}")
            else:
                has_error = True
                parts.append(f"{heading}\n{result or 'No result returned.'}")
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
        return AgentSettings(agent_mode="react")

    def _resolve_token_budget(self) -> TokenBudget:
        if callable(self._token_budget_provider):
            budget = self._token_budget_provider()
            if isinstance(budget, TokenBudget):
                return budget
        if isinstance(self._token_budget_provider, TokenBudget):
            return self._token_budget_provider
        return TokenBudget()

    @staticmethod
    def _build_permission_context(
        agent_type: str,
        parent_context: ToolExecutionContext | None,
        *,
        read_only: bool = False,
        extra_deny_rules: list[str] | None = None,
    ) -> PermissionContext:
        return build_subagent_permission_context(
            agent_type,
            parent_context,
            read_only=read_only,
            extra_deny_rules=extra_deny_rules,
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
    def _build_subagent_prompt(
        agent_type: str,
        prompt: str,
        *,
        workspace_root: str | Path | None = None,
    ) -> str:
        return build_subagent_prompt(
            agent_type,
            prompt,
            get_custom_agent=lambda name: get_custom_agent(name, workspace_root),
        )

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
        llm: LLMAdapter | None = None,
    ) -> ContextBuilder:
        # A worker cannot see the parent conversation. Its delegated
        # prompt is self-contained, while ContextBuilder still loads workspace
        # instructions normally. Copying history leaks sibling objectives.
        return ContextBuilder(
            token_budget=token_budget,
            agent_settings=agent_settings,
            llm=llm,
        )
