"""Agent helper tools: user clarification, artifacts, and subagents."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.prompt_cache import prompt_cache_fork_diagnostic
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import default_runtime
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

_RESEARCH_TASK_RE = re.compile(
    r"(?:research|调查|调研|查询|搜索|检索|天气|latest|current|today|web|网页|来源|证据)",
    re.IGNORECASE,
)
_INTERNAL_TOOL_REFERENCE_RE = re.compile(r"\bcall_[a-z0-9_-]{8,}\b", re.IGNORECASE)
_ELAPSED_ONLY_RE = re.compile(r"^\d+(?:\.\d+)?s elapsed$", re.IGNORECASE)


def _subagent_iteration_budget(*, prompt: str, agent_type: str, configured: int) -> int:
    """Keep bounded workers, but give evidence-gathering tasks enough turns to finish."""
    configured = max(1, int(configured or 8))
    research_task = agent_type in {"explore", "plan"} or bool(_RESEARCH_TASK_RE.search(prompt or ""))
    ceiling = 12 if research_task else 8
    floor = 10 if research_task else 1
    return min(max(configured, floor), ceiling)


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


def _scope_parallel_task_prompt(task: dict[str, Any], *, scope: str) -> str:
    prompt = str(task.get("prompt") or "").strip()
    assigned_scope = str(scope).strip()
    if not assigned_scope:
        return prompt
    return (
        "[Parallel task scope]\n"
        f"Your assigned objective is exactly: {assigned_scope}\n"
        "Work only on this objective. Do not investigate, execute, or summarize targets "
        "assigned to sibling subagents, even if the original prompt mentions them.\n\n"
        f"{prompt}"
    )


def _exclusive_parallel_task_scopes(tasks: list[dict[str, Any]]) -> list[str]:
    """Select a unique user-facing scope for every parallel worker."""

    descriptions = [str(task.get("description") or "").strip() for task in tasks]
    objectives = [str(task.get("objective") or "").strip() for task in tasks]
    description_counts: dict[str, int] = {}
    objective_counts: dict[str, int] = {}
    for description in descriptions:
        if description:
            key = description.casefold()
            description_counts[key] = description_counts.get(key, 0) + 1
    for objective in objectives:
        if objective:
            key = objective.casefold()
            objective_counts[key] = objective_counts.get(key, 0) + 1

    scopes: list[str] = []
    for description, objective in zip(descriptions, objectives, strict=True):
        if description and description_counts.get(description.casefold()) == 1:
            scopes.append(description)
            continue
        if objective and objective_counts.get(objective.casefold()) == 1:
            scopes.append(objective)
            continue
        return []
    normalized_scopes = [scope.casefold() for scope in scopes]
    if len(set(normalized_scopes)) != len(normalized_scopes):
        return []
    generic_scope = re.compile(
        r"^(?:agent|subagent|worker|task|subtask|researcher|智能体|子智能体|子\s*agent|任务|调研员)"
        r"\s*[-_#：:]?\s*[一二三四五六七八九十\d]+$",
        re.IGNORECASE,
    )
    if any(generic_scope.fullmatch(scope) for scope in scopes):
        return []
    return scopes


def _available_agent_types() -> list[str]:
    """Return built-in plus discovered custom subagent types for model schema."""
    return available_agent_types(discover_agents)


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
        "read_only": _bool_field(raw.get("read_only"), False),
        "write_scope": _string_list(raw.get("write_scope")),
        "current_activity": str(raw.get("current_activity") or "").strip(),
        "waiting_on": waiting_on,
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


async def _auto_complete_workflow_task(
    *,
    context: ToolExecutionContext | None,
    workflow_metadata: dict[str, Any],
    subagent_id: str,
    result_text: str,
) -> None:
    task_id = str(workflow_metadata.get("task_id") or "").strip()
    if not task_id:
        return
    runtime = runtime_from_context(context) or default_runtime()
    task = runtime.get_swarm_task(task_id)
    if task is None or task.status in {"completed", "cancelled"}:
        return
    try:
        already_attached = any(
            str(getattr(output, "author_id", "") or "") == subagent_id
            for output in getattr(task, "outputs", []) or []
        )
        if already_attached:
            from backend.tools.swarm_tools import TaskUpdateTool

            await TaskUpdateTool().execute(
                {"task_id": task_id, "status": "completed"},
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
                    "status": "completed",
                },
                context=context,
            )
    except Exception as exc:
        logger.warning("workflow task auto-complete failed for %s: %s", task_id, exc)


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
        "Use for complex, independent work items that benefit from focused attention. "
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
                        "description": "Wall-clock deadline from 30 to 600 seconds. Prefer at least 90 seconds for web research.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Return immediately with a subagent id instead of waiting.",
                    },
                    "required_for_final": {
                        "type": "boolean",
                        "description": "Whether the parent must collect this result before finalizing.",
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
                    "parallel_tasks": {
                        "type": "array",
                        "description": (
                            "Run multiple subtasks concurrently. Each item is an object with "
                            "'description', 'prompt', and optional 'agent_type'. Each description "
                            "or objective must name one concrete, non-overlapping scope; generic "
                            "labels such as 'Agent 1' are rejected. "
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
                                "read_only": {
                                    "type": "boolean",
                                    "description": "Whether this subtask is intended to be read-only.",
                                },
                                "write_scope": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional file/path scopes this subtask may modify.",
                                },
                            },
                            "required": ["description", "prompt"],
                        },
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Wall-clock timeout in seconds per subtask (default 300, max 600). "
                            "If a subtask exceeds this, partial results are returned."
                        ),
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Start a single subtask asynchronously and return immediately with a subagent id. "
                            "Use task_stop to cancel it later. Ignored for parallel_tasks."
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
        timeout_seconds = float(args.get("timeout_seconds") or 300.0)
        timeout_seconds = min(max(timeout_seconds, 30.0), 600.0)

        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()
        if llm is None or tool_registry is None or permission_checker is None:
            return self._error_result("Subagent runtime is not configured")

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
                        "Parallel tasks require one exclusive description or objective per worker. "
                        "Split the shared request into non-overlapping scopes before delegating."
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
        agent_type = normalize_agent_type(
            str(args.get("agent_type") or "general-purpose"),
            get_custom_agent=get_custom_agent,
        )
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
                timeout_seconds=timeout_seconds,
                subagent_metadata=_subagent_metadata(args),
            )

        return await self._run_single_subtask(
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
        timeout_seconds: float,
        subagent_metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        subagent_id = self._spawn_background_subtask(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            context=context,
            timeout_seconds=timeout_seconds,
            subagent_metadata=subagent_metadata,
        )
        await asyncio.sleep(0)

        return ToolResult(
            content=(
                f"Started background subagent {subagent_id} ({agent_type}). "
                "It will report progress through subagent events. "
                f"Use task_status with subagent_id={subagent_id} to collect the result, "
                f"or task_stop with subagent_id={subagent_id} to cancel it."
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
        timeout_seconds: float,
    ) -> ToolResult:
        started: list[tuple[str, dict[str, str]]] = []
        for task in tasks:
            subagent_id = self._spawn_background_subtask(
                description=task["description"],
                prompt=task["prompt"],
                agent_type=task.get("agent_type", "general-purpose"),
                context=context,
                timeout_seconds=timeout_seconds,
                subagent_metadata=_subagent_metadata(task),
            )
            started.append((subagent_id, task))
        await asyncio.sleep(0)
        lines = [
            f"Started {len(started)} background subagents.",
            "Use task_status with each subagent_id to collect results, or task_stop to cancel one.",
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
        timeout_seconds: float,
        subagent_metadata: dict[str, Any] | None = None,
    ) -> str:
        subagent_id = f"subagent-{uuid4().hex[:8]}"
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
        timeout_seconds: float = 300.0,
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
        subagent_cancel_event = cancel_event or (context.cancel_event if context else None)
        parent_run_id = str(parent_metadata.get("run_id", ""))
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
            read_only=workflow_metadata["read_only"],
            write_scope=workflow_metadata["write_scope"],
            current_activity=workflow_metadata["current_activity"],
        ) if runtime is not None else None

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
            description=prompt,
            teammate_name=agent_type,
        )
        await _run_subagent_start_hook(subagent_id, agent_type)

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
        sub_context = self._build_permission_context(agent_type, context)
        delegated_prompt = self._build_subagent_prompt(agent_type, prompt)
        sub_state = AgentState(user_message=delegated_prompt, max_iterations=sub_settings.max_iterations)
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
        last_error = ""
        sub_context_builder = self._build_subagent_context_builder(
            context=context,
            token_budget=sub_budget,
            agent_settings=sub_settings,
        )

        try:
            from backend.agent.loop import run_agent_loop

            async def subagent_approval_handler(tool_call_id: str) -> dict[str, str]:
                return {
                    "action": "reject",
                    "guidance": (
                        f"Subagent {subagent_id} cannot request user approvals directly. "
                        "Return a summary and let the main agent decide the next action."
                    ),
                }

            async def subagent_event_bridge(event_type: str, data: dict[str, Any]) -> None:
                if emit_event is None:
                    return
                if event_type not in {"tool_call", "agent.progress"}:
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

            try:
                async with asyncio.timeout(timeout_seconds):
                    async for event in QueryEngine(runner=run_agent_loop).submit(QuerySubmission(
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
                            session_id=subagent_id,
                            task_id=subagent_id,
                            task_manager=context.task_manager if context else None,
                            emit_event=subagent_event_bridge if emit_event is not None else None,
                            metadata=subagent_metadata_payload,
                        ),
                    )):
                        if event.type == "text_chunk":
                            summary_parts.append(str(event.data.get("content", "")))
                        elif event.type == "error":
                            last_error = str(event.data.get("message", ""))
                            summary_parts.append(f"\nError: {last_error}")
                        elif event.type == "tool_result":
                            last_tool_name = str(
                                event.data.get("tool_name")
                                or event.data.get("name")
                                or event.data.get("id")
                                or ""
                            )
                        elif event.type == "done":
                            terminal_status = str(event.data.get("status") or "completed")
                            terminal_reason = str(event.data.get("reason") or "")
                        # Emit progress at iteration boundaries
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
            except asyncio.TimeoutError:
                timed_out = True

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            summary = "".join(summary_parts).strip() or sub_state.reply.strip()
            if timed_out:
                summary = _sanitize_timeout_partial_summary(summary)
            tool_call_count = len(sub_state.tool_calls)

            if timed_out:
                has_partial_result = bool(summary)
                timeout_status = "partial" if has_partial_result else "failed"
                timeout_content = (
                    f"Subagent {subagent_id} ({agent_type}) reached its runtime deadline after "
                    f"{timeout_seconds:.0f}s. It completed {sub_state.iterations} iteration(s) and "
                    f"{tool_call_count} tool call(s)."
                )
                if summary:
                    timeout_content += f"\nPartial result retained:\n{summary}"
                result_record = None
                completed_record = None
                if runtime is not None:
                    result_record = runtime.store_subagent_result(
                        subagent_id,
                        status=timeout_status,
                        content=timeout_content,
                        duration_ms=elapsed_ms,
                        iterations=sub_state.iterations,
                        tool_call_count=tool_call_count,
                        timed_out=True,
                    )
                    completed_record = runtime.complete_subagent(
                        subagent_id,
                        timeout_status,
                        summary=timeout_content[:500],
                        tool_count=tool_call_count,
                    )
                if emit_event is not None:
                    done_event = AgentEvent.subagent_done(
                        subagent_id=subagent_id,
                        summary=timeout_content[:500],
                        duration_ms=elapsed_ms,
                        iterations=sub_state.iterations,
                        tool_call_count=tool_call_count,
                        timed_out=True,
                        status=timeout_status,
                        termination_reason="deadline_exceeded",
                        initiator="runtime",
                    )
                    if result_record is not None:
                        done_event.data["result"] = result_record.to_dict()
                    if completed_record is not None:
                        done_event.data["record"] = completed_record.to_dict()
                    prompt_cache_fork = prompt_cache_fork_diagnostic()
                    if prompt_cache_fork:
                        done_event.data["prompt_cache_fork"] = prompt_cache_fork
                    await emit_event("subagent.done", done_event.data)
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
            if terminal_status == "failed":
                raise RuntimeError(last_error or terminal_reason or "Subagent run failed")

            result_text = self._build_subtask_result_summary(
                subagent_id=subagent_id,
                agent_type=agent_type,
                summary=summary,
                duration_ms=elapsed_ms,
                iterations=sub_state.iterations,
                tool_calls=sub_state.tool_calls,
                timed_out=timed_out,
                timeout_seconds=timeout_seconds,
            )
            result_record = None
            completed_record = None
            if runtime is not None:
                result_record = runtime.store_subagent_result(
                    subagent_id,
                    status="failed" if timed_out else "completed",
                    content=result_text,
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    timed_out=timed_out,
                )
                completed_record = runtime.complete_subagent(
                    subagent_id,
                    "failed" if timed_out else "completed",
                    summary=summary[:500] if summary else "",
                    tool_count=tool_call_count,
                )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    summary=summary[:500] if summary else "",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    timed_out=timed_out,
                )
                if result_record is not None:
                    done_event.data["result"] = result_record.to_dict()
                if completed_record is not None:
                    done_event.data["record"] = completed_record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await emit_event("subagent.done", done_event.data)
            if not timed_out:
                await _auto_complete_workflow_task(
                    context=context,
                    workflow_metadata=workflow_metadata,
                    subagent_id=subagent_id,
                    result_text=result_text,
                )
            await _run_subagent_stop_hook(
                subagent_id,
                "failed" if timed_out else "completed",
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
                duration_ms=elapsed_ms,
                display_summary=f"Subagent ({agent_type}): {description[:60]}",
                result_kind="subagent",
            )
        except asyncio.CancelledError:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if runtime is not None:
                runtime.store_subagent_result(
                    subagent_id,
                    status="cancelled",
                    content="Subagent was cancelled.",
                    error="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                )
                if runtime is not None:
                    record = runtime.complete_subagent(subagent_id, "cancelled", summary="cancelled", tool_count=len(sub_state.tool_calls))
                    if record is not None:
                        done_event.data["record"] = record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await emit_event("subagent.done", done_event.data)
            await _run_subagent_stop_hook(
                subagent_id,
                "cancelled",
                "Subagent was cancelled.",
                agent_type=agent_type,
            )
            await _run_task_completed_hook(
                task_id=subagent_id,
                subject=description,
                description="Subagent was cancelled.",
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
            if runtime is not None:
                runtime.store_subagent_result(
                    subagent_id,
                    status="failed",
                    content=error_content,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                )
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error=str(exc),
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                )
                if runtime is not None:
                    record = runtime.complete_subagent(subagent_id, "failed", summary=str(exc), tool_count=len(sub_state.tool_calls))
                    if record is not None:
                        done_event.data["record"] = record.to_dict()
                prompt_cache_fork = prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await emit_event("subagent.done", done_event.data)
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
        timeout_seconds: float,
    ) -> ToolResult:
        """Run multiple subtasks concurrently via ``asyncio.gather``.

        Each subtask gets its own wall-clock timeout.  An outer timeout
        ensures the entire parallel batch cannot run indefinitely.
        """
        emit_event = context.emit_event if context else None
        total = len(tasks)
        start_time = time.perf_counter()

        coros = [
            self._run_single_subtask(
                description=t["description"],
                prompt=t["prompt"],
                agent_type=t.get("agent_type", "general-purpose"),
                context=context,
                timeout_seconds=timeout_seconds,
                subtask_index=i,
                total_subtasks=total,
                subagent_metadata=_subagent_metadata(t),
            )
            for i, t in enumerate(tasks)
        ]

        # Outer timeout: per-task timeout + 30s overhead, capped at 10 minutes
        outer_timeout = min(timeout_seconds + 30.0, 600.0)
        task_ids = [
            str(_subagent_metadata(task).get("task_id") or "").strip()
            for task in tasks
        ]
        try:
            async with asyncio.timeout(outer_timeout):
                results = await asyncio.gather(*coros, return_exceptions=True)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if emit_event is not None:
                for task_id in task_ids:
                    if not task_id:
                        continue
                    await emit_event(
                        "subagent.done",
                        AgentEvent.subagent_done(
                            subagent_id=task_id,
                            error="outer timeout",
                            duration_ms=elapsed_ms,
                        ).data,
                    )
            return ToolResult(
                content=(
                    f"Parallel subtasks timed out after {outer_timeout:.0f}s. "
                    f"Some tasks may not have completed."
                ),
                is_error=True,
                duration_ms=elapsed_ms,
                display_summary="Parallel subtasks timed out",
                result_kind="subagent",
            )

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
        timeout_seconds: float,
    ) -> str:
        """Build a structured result summary for the parent agent.

        Includes the subagent's text output, timing metadata, and a compact
        list of tool calls so the parent knows what the subagent actually did.
        """
        header = f"Subagent {subagent_id} ({agent_type})"
        if timed_out:
            header += f" [TIMED OUT after {timeout_seconds:.0f}s]"
        header += f" completed in {duration_ms / 1000:.1f}s, {iterations} iteration(s)."

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
        return AgentSettings(max_iterations=8, agent_mode="react")

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
    ) -> PermissionContext:
        return build_subagent_permission_context(agent_type, parent_context)

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
        parent_builder = None
        if context is not None and isinstance(context.metadata, dict):
            parent_builder = context.metadata.get("_context_builder")
        if isinstance(parent_builder, ContextBuilder):
            cloned = clone_context_builder(parent_builder)
            TaskTool._drop_trailing_unresolved_tool_exchange(cloned)
            cloned._budget = token_budget
            cloned._agent_settings = agent_settings
            return cloned
        return ContextBuilder(
            token_budget=token_budget,
            agent_settings=agent_settings,
        )

    @staticmethod
    def _drop_trailing_unresolved_tool_exchange(builder: ContextBuilder) -> None:
        history = list(getattr(builder, "_history", []) or [])
        if not history:
            return

        pending_ids: set[str] = set()
        pending_assistant_index: int | None = None
        truncate_at: int | None = None

        for index, message in enumerate(history):
            if getattr(message, "role", "") == "assistant" and getattr(message, "tool_calls", None):
                if pending_ids and pending_assistant_index is not None:
                    truncate_at = pending_assistant_index
                    break
                pending_ids = {
                    str(tool_call.id)
                    for tool_call in (message.tool_calls or [])
                    if str(tool_call.id or "").strip()
                }
                pending_assistant_index = index if pending_ids else None
                continue

            if getattr(message, "role", "") == "tool":
                call_id = str(getattr(message, "tool_call_id", "") or "").strip()
                if pending_ids and call_id in pending_ids:
                    pending_ids.discard(call_id)
                    if not pending_ids:
                        pending_assistant_index = None
                    continue
                truncate_at = index
                break

            if pending_ids and pending_assistant_index is not None:
                truncate_at = pending_assistant_index
                break

        if truncate_at is None and pending_ids and pending_assistant_index is not None:
            truncate_at = pending_assistant_index
        if truncate_at is None:
            return

        builder._history = history[:truncate_at]
        builder._rebuild_history_token_cache()
