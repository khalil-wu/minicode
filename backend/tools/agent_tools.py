"""Agent helper tools: user clarification, artifacts, and subagents."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.public_projection import project_public_subagent_result
from backend.agent.provider_protocol import provider_raw_from_event_data
from backend.agent.prompt_cache import prompt_cache_fork_diagnostic
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.rollout_budget import RolloutBudget
from backend.agent.execution_journal import (
    ExecutionJournal,
)
from backend.agent.state import AgentState
from backend.agents.loader import discover_agents, get_custom_agent
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, TokenBudget, load_config
from backend.feature_flags import feature_enabled
from backend.llm.base import LLMAdapter
from backend.llm.model_selection import (
    REASONING_LEVEL_ORDER,
    apply_model_thinking_level,
    config_with_model_budget,
    default_model_thinking_level,
    model_thinking_levels,
)
from backend.permissions.checker import (
    PermissionChecker,
    normalize_permission_mode_token,
)
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_control_plane import (
    CHILD_AGENT_OPTIONS_METADATA_KEY,
    normalize_agent_fork_turns,
    normalize_agent_task_name,
)
from backend.tools.base import MAX_TOOL_RESULT_BYTES, BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.tools.agent_artifact_tools import ReadArtifactTool as ReadArtifactTool
from backend.tools.agent_user_tools import AskUserTool as AskUserTool
from backend.tools.agent_user_tools import BriefTool as BriefTool
from backend.tools.subagent_control_tools import TaskStatusTool as TaskStatusTool
from backend.tools.subagent_control_tools import TaskStopTool as TaskStopTool
from backend.tools.subagent_catalog import (
    BUILTIN_AGENT_TYPES,
    agent_type_description,
    available_agent_types,
    normalize_agent_type,
)
from backend.tools.subagent_context import (
    AgentExecutionProfile,
    build_agent_execution_profile,
    build_subagent_permission_context,
    build_subagent_prompt,
    execution_profile_for_background_resume,
    resolve_agent_execution_profile,
    sanitize_subagent_runtime_metadata,
    transition_subagent_permission_mode,
)
from backend.tools.subagent_runtime import (
    metadata_from_context,
    require_runtime_from_context,
)
from backend.tools.subagent_result import compact_subagent_result
from backend.tools.toolsets import (
    ACTIVE_TOOLSET_POLICY_METADATA_KEY,
    SESSION_TOOLSET_POLICY_METADATA_KEY,
    ToolsetPolicy,
)
from backend.tools.toolset_runtime import restore_toolset_policy


logger = logging.getLogger(__name__)

from backend.tools.subagent_support import (
    MAX_PARALLEL_CONCURRENCY,
    MAX_PARALLEL_TASKS,
    _MODEL_FAMILY_ALIASES,
    _PUBLIC_PERMISSION_MODE_TO_INTERNAL,
    _SUBAGENT_CAPACITY_MESSAGE,
    _SUBAGENT_RESULT_ARTIFACT_THRESHOLD_BYTES,
    _SubagentLifecycleOwner,
    _SubagentLLMResolution,
    _adapter_provider_model,
    _available_agent_types,
    _bool_field,
    _close_subagent_llm,
    _close_subagent_llm_resolution,
    _configured_subagent_overrides,
    _custom_agent_deny_rules,
    _exclusive_parallel_task_scopes,
    _externalize_large_subagent_result,
    _fork_snapshot_for_child,
    _format_teammate_message,
    _hook_veto,
    _is_team_subagent,
    _narrowed_subagent_scope_metadata,
    _nonempty_subagent_metadata,
    _normalize_child_task_name,
    _normalize_fork_turns,
    _parallel_undeclared_writers,
    _persisted_session_toolset_policy,
    _primary_llm_adapter,
    _prompt_scope_summary,
    _resolve_subagent_llm,
    _run_subagent_start_hook,
    _run_task_created_hook,
    _sanitize_teammate_name,
    _scope_is_within_any,
    _string_list,
    _subagent_display_summary,
    _subagent_metadata,
    _subagent_prompt_cache_fork_diagnostic,
    _user_visible_progress_text,
    _xml_attribute,
)


def _task_tool_parameters(
    agent_types: list[str],
    agent_type_help: str,
) -> dict[str, Any]:
    """Build the one canonical Task input contract used by every projection.

    The registry may expose a concise model description, but it must not hide
    runtime-required coordination fields. In particular, parallel writers are
    rejected unless each item declares a disjoint ``write_scope``.
    """

    def delegation_properties(*, parallel_item: bool) -> dict[str, Any]:
        subject = "subtask" if parallel_item else "subagent"
        properties = {
            "description": {
                "type": "string",
                "description": f"Short description of this {subject}, shown in the UI.",
            },
            "prompt": {
                "type": "string",
                "description": f"Complete self-contained instructions for this {subject}.",
            },
            "agent_type": {
                "type": "string",
                "enum": agent_types,
                "description": agent_type_help,
            },
            "model": {
                "type": "string",
                "description": "Optional model override; 'inherit' or omission keeps the session model.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider id for the delegated model; omitted inherits the parent provider.",
            },
            "reasoning_effort": {
                "type": "string",
                "description": (
                    "Optional reasoning-effort override. The value must be "
                    "declared by the resolved target model."
                ),
            },
            "cancel_with_parent": {
                "type": "boolean",
                "description": f"Whether parent cancellation also cancels this {subject}.",
            },
            "detach_from_parent": {
                "type": "boolean",
                "description": f"If true, this {subject} keeps running after parent cancellation.",
            },
            "read_only": {
                "type": "boolean",
                "description": f"Whether this {subject} must avoid workspace writes.",
            },
            "write_scope": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "File or directory scopes this subtask may modify. Required and disjoint for "
                    "write-capable parallel tasks."
                    if parallel_item
                    else "Optional file or directory scopes this subagent may modify."
                ),
            },
            "isolation": {
                "type": "string",
                "enum": ["worktree"],
                "description": "Optional isolation mode: run in a temporary git worktree.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional absolute working directory inside the active workspace.",
            },
        }
        return properties

    def teammate_properties() -> dict[str, Any]:
        # Teammate mode uses the public {name, team_name, mode} shape; internal
        # lifecycle flags are derived from that public contract.
        return {
            "name": {
                "type": "string",
                "description": "Addressable teammate name. Uses current team context when team_name is omitted.",
            },
            "team_name": {
                "type": "string",
                "description": "Existing team name used to spawn a teammate.",
            },
            "mode": {
                "type": "string",
                "enum": ["confirm", "auto", "bypass", "plan"],
                "description": "Permission mode for the spawned teammate; plan requires leader approval.",
            },
        }

    properties = delegation_properties(parallel_item=False)
    properties.update(teammate_properties())
    properties.update({
        "parallel_tasks": {
            "type": "array",
            "minItems": 2,
            "maxItems": MAX_PARALLEL_TASKS,
            "description": (
                "Run 2-8 bounded subtasks in one call; at most four run concurrently. "
                "Read-only scopes may overlap. Every write-capable item must declare an "
                "explicit write_scope disjoint from its siblings."
            ),
            "items": {
                "type": "object",
                "properties": delegation_properties(parallel_item=True),
                "required": ["description", "prompt"],
            },
        },
        "run_in_background": {
            "type": "boolean",
            "description": (
                "Return immediately with subagent ids instead of waiting. Background completion "
                "is delivered later; use task_status/send_message/task_stop to manage it."
            ),
        },
    })
    return {
        "type": "object",
        "properties": properties,
        "anyOf": [
            {"required": ["description", "prompt"]},
            {"required": ["parallel_tasks"]},
        ],
    }


class TaskTool(BaseTool):
    """Delegate a bounded task to an isolated subagent."""

    name = "task"
    # Delegation is a core execution capability. Hiding it behind tool_search
    # makes availability depend on discovery rather than the model's decision.
    should_defer = False
    result_kind = "subagent"
    activity_kind = "genericTool"
    display_label = "Start subagent"
    # The enclosing turn/agent lifecycle owns any configured deadline; the
    # delegation tool does not invent another wall-clock cutoff.
    timeout_seconds = None
    # Each delegated result has its own persistence/truncation boundary, so a
    # parallel batch may exceed the generic single-result backstop.
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
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters=_task_tool_parameters(agent_types, agent_type_help),
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
            parameters=_task_tool_parameters(agent_types, agent_type_help),
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        description = str(args.get("description") or "").strip()
        parallel_tasks = args.get("parallel_tasks")
        parent_metadata = self._metadata_from_context(context)
        run_context = context.run_context if context is not None else None
        parent_runtime = (
            run_context.subagent_parent_runtime
            if run_context is not None
            else {}
        )
        llm = (
            parent_runtime.get("llm")
            or (context.llm if context is not None else None)
            or self._resolve_llm()
        )
        tool_registry = self._resolve_tool_registry()
        permission_checker = (
            context.permission_checker
            if context is not None and context.permission_checker is not None
            else self._resolve_permission_checker()
        )
        if llm is None or tool_registry is None or permission_checker is None:
            return self._error_result("Subagent runtime is not configured")
        if isinstance(parallel_tasks, list) and any(
            isinstance(item, dict)
            and (
                str(item.get("name") or "").strip()
                or str(item.get("team_name") or "").strip()
                or str(item.get("mode") or "").strip()
            )
            for item in parallel_tasks
        ):
            return self._error_result(
                "Named teammates are spawned as individual task calls; "
                "parallel_tasks is reserved for ordinary independent subagents."
            )
        # ── Parallel execution path ──
        if isinstance(parallel_tasks, list) and len(parallel_tasks) > MAX_PARALLEL_TASKS:
            return self._error_result(
                f"Too many parallel tasks ({len(parallel_tasks)}). Max is {MAX_PARALLEL_TASKS}."
            )
        if isinstance(parallel_tasks, list) and len(parallel_tasks) == 1:
            return self._error_result(
                "parallel_tasks requires at least two tasks; use the single-task fields instead."
            )
        if isinstance(parallel_tasks, list) and len(parallel_tasks) >= 2:
            workspace_root = context.workspace_root if context is not None else None

            def resolve_custom_agent(name: str) -> dict[str, Any] | None:
                return get_custom_agent(name, workspace_root)

            tasks: list[dict[str, Any]] = []
            for item in parallel_tasks:
                if not isinstance(item, dict):
                    continue
                t_desc = str(item.get("description") or "").strip()
                t_prompt = str(item.get("prompt") or "").strip()
                t_type = str(item.get("agent_type") or "general-purpose").strip()
                if t_desc and t_prompt:
                    try:
                        resolved_type = normalize_agent_type(
                            t_type,
                            get_custom_agent=resolve_custom_agent,
                        )
                    except ValueError as exc:
                        return self._error_result(str(exc))
                    task_payload = {
                        "description": t_desc,
                        "prompt": t_prompt,
                        "agent_type": resolved_type,
                        **_nonempty_subagent_metadata(item),
                    }
                    tasks.append(task_payload)
                    if tasks[-1]["agent_type"] in {"explore", "plan"}:
                        tasks[-1]["read_only"] = True
            # A single-item batch follows the same batch path; a len>=2 gate
            # used to fall through to the single-execution
            # path, which reads a top-level prompt that parallel calls lack.
            if tasks:
                try:
                    for task in tasks:
                        task_runtime_config = _subagent_metadata(task)
                        await _resolve_subagent_llm(
                            llm,
                            parent_metadata=parent_metadata,
                            run_context=run_context,
                            agent_type=str(task.get("agent_type") or "general-purpose"),
                            model_override=str(task_runtime_config.get("model") or ""),
                            effort_override=str(task_runtime_config.get("effort") or ""),
                            workspace_root=workspace_root,
                            build_adapter=False,
                        )
                except (RuntimeError, ValueError) as exc:
                    return self._error_result(str(exc))
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
        teammate_name = str(args.get("name") or "").strip()
        team_name = str(args.get("team_name") or "").strip()
        teammate_mode = str(args.get("mode") or "").strip()
        if self._is_recursive_subagent_call(context):
            permission = getattr(context, "permission", None)
            profile = resolve_agent_execution_profile(permission, parent_metadata)
            if profile is None:
                # Legacy transcripts may have the child identity but no
                # persisted profile.  Infer the narrowest safe surface only as
                # a compatibility fallback; all new routing is capability
                # based and provider-neutral.
                source = str(getattr(permission, "source", "") or "")
                profile = build_agent_execution_profile(
                    team_mode=source.startswith("teammate:"),
                    background=not source.startswith("teammate:"),
                )

            # Named teammates are a leader-facing roster operation, never a
            # child delegation primitive.  A teammate can launch an ordinary
            # foreground child; a hierarchical profile may opt in to either
            # foreground or background ordinary children through its
            # explicit delegation capability.
            nested_teammate = bool(teammate_name)
            nested_background = bool(args.get("run_in_background"))
            if nested_teammate:
                message = (
                    "Nested agents cannot spawn other teammates — the team roster is flat. "
                    "To spawn an ordinary child, omit the name parameter."
                )
                summary = "Blocked nested teammate"
            elif nested_background and not profile.can_delegate_background:
                if profile.team_mode:
                    message = (
                        "In-process teammates cannot spawn background agents. "
                        "Use run_in_background=false for a synchronous ordinary child."
                    )
                    summary = "Blocked teammate background subagent"
                else:
                    message = (
                        "This child execution profile cannot spawn background agents. "
                        "Complete the assigned task with the available tools."
                    )
                    summary = "Blocked background delegation"
            elif not nested_background and not profile.can_delegate_foreground:
                message = (
                    "Recursive subagent delegation is blocked. This execution profile "
                    "does not allow another child. "
                    "Complete the assigned task with the available tools."
                )
                summary = "Blocked recursive subagent"
            else:
                message = ""
                summary = ""
            if message:
                return ToolResult(
                    content=message,
                    is_error=True,
                    status="blocked",
                    display_summary=summary,
                    result_kind="subagent",
                )
        isolation = str(args.get("isolation") or "").strip().lower()
        if isolation and isolation != "worktree":
            return self._error_result(
                f"Unsupported isolation mode: {isolation!r}. Only 'worktree' is supported."
            )
        workspace_root = context.workspace_root if context is not None else None
        requested_cwd = str(args.get("cwd") or "").strip()
        if requested_cwd and isolation:
            return self._error_result(
                'cwd is mutually exclusive with isolation: "worktree"'
            )
        if requested_cwd:
            candidate = Path(requested_cwd).expanduser()
            if not candidate.is_absolute():
                return self._error_result("cwd must be an absolute path")
            candidate = candidate.resolve()
            owning_root = Path(workspace_root).resolve() if workspace_root else None
            if owning_root is None:
                return self._error_result("cwd requires an active workspace")
            try:
                candidate.relative_to(owning_root)
            except ValueError:
                return self._error_result("cwd must be inside the active workspace")
            if not candidate.is_dir():
                return self._error_result("cwd must reference an existing directory")
            args = {**args, "cwd": str(candidate)}
        try:
            agent_type = normalize_agent_type(
                str(args.get("agent_type") or "general-purpose"),
                get_custom_agent=lambda name: get_custom_agent(name, workspace_root),
            )
        except ValueError as exc:
            return self._error_result(str(exc))
        if agent_type in {"explore", "plan"} and not bool(args.get("read_only")):
            args = {**args, "read_only": True}
        if not description:
            return self._error_result("Missing description argument")
        if not prompt:
            return self._error_result("Missing prompt argument")
        runtime = require_runtime_from_context(context)
        conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
        if teammate_name and not team_name:
            leader_id = str(parent_metadata.get("run_id") or "").strip()
            led_teams = runtime.list_swarm_teams(
                conversation_id=conversation_id,
                limit=100,
            )
            current_team = next(
                (
                    team for team in led_teams
                    if str(team.created_by or "") == leader_id
                ),
                None,
            )
            if current_team is not None:
                team_name = current_team.team_name
                args = {**args, "team_name": team_name}
        if team_name and not teammate_name:
            return self._error_result(
                "team_name only selects a team; provide name to spawn a teammate."
            )
        if teammate_mode and not (teammate_name and team_name):
            return self._error_result(
                "mode is only valid when spawning a named teammate."
            )
        if teammate_mode and teammate_mode not in _PUBLIC_PERMISSION_MODE_TO_INTERNAL:
            return self._error_result(
                f"Unsupported teammate mode: {teammate_mode!r}."
            )
        try:
            single_runtime_config = _subagent_metadata(args)
            await _resolve_subagent_llm(
                llm,
                parent_metadata=parent_metadata,
                run_context=run_context,
                agent_type=agent_type,
                model_override=str(single_runtime_config.get("model") or ""),
                effort_override=str(single_runtime_config.get("effort") or ""),
                workspace_root=workspace_root,
                build_adapter=False,
            )
        except (RuntimeError, ValueError) as exc:
            return self._error_result(str(exc))
        if teammate_name:
            team = runtime.list_swarm_teams(
                conversation_id=conversation_id,
                team_name=team_name,
                limit=1,
            )
            if not team:
                return self._error_result(
                    f'Team "{team_name}" does not exist. Call TeamCreate first to create the team.'
                )
            existing_names = {
                str(member.id or "").split("@", 1)[0].casefold()
                for member in team[0].members
            }
            unique_name = teammate_name
            suffix = 2
            while unique_name.casefold() in existing_names:
                unique_name = f"{teammate_name}-{suffix}"
                suffix += 1
            sanitized_name = _sanitize_teammate_name(unique_name)
            teammate_id = f"{sanitized_name}@{team_name}"
            teammate_metadata = _subagent_metadata({
                **args,
                "name": sanitized_name,
                "team_name": team_name,
                "teammate_id": teammate_id,
            })
            return await self._start_background_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_metadata=teammate_metadata,
                subagent_id=teammate_id,
            )
        child_metadata = _subagent_metadata(args)
        internal_child_options = parent_metadata.get(
            CHILD_AGENT_OPTIONS_METADATA_KEY
        )
        if isinstance(internal_child_options, dict):
            raw_profile = internal_child_options.get("execution_profile")
            if isinstance(raw_profile, AgentExecutionProfile):
                child_metadata["_execution_profile"] = raw_profile
            elif isinstance(raw_profile, dict):
                child_metadata["_execution_profile"] = dict(raw_profile)
            for public_key, internal_key in (
                ("task_name", "_task_name"),
                ("fork_turns", "_fork_turns"),
            ):
                value = internal_child_options.get(public_key)
                if value not in (None, ""):
                    child_metadata[internal_key] = value
        if bool(args.get("run_in_background")):
            return await self._start_background_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_metadata=child_metadata,
            )

        return await self._run_foreground_subtask(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            context=context,
            subagent_metadata=child_metadata,
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
        """Wait for a MiniCode worker slot before starting one foreground task."""

        runtime = require_runtime_from_context(context)
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
            result = await self._run_single_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_id=subagent_id,
                subagent_metadata=subagent_metadata,
            )
            result.runtime_metadata.setdefault("subagent_id", subagent_id)
            return result
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
        # Resolve the shared runtime before publishing the child.  The
        # background path used to rely on an accidental free variable here;
        # the resulting NameError was swallowed by the advisory metadata
        # handler, so the task started but reported an empty output journal.
        runtime = require_runtime_from_context(context)
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        teammate_config = _subagent_metadata(subagent_metadata)
        is_teammate = _is_team_subagent(teammate_config)
        hook_manager = (
            context.run_context.hook_manager
            if context is not None and context.run_context is not None
            else None
        )
        # Background calls return to the parent immediately, so the lifecycle
        # gate must run in this synchronous caller before a queued child task
        # is exposed as running.  The child path receives an explicit marker to
        # avoid firing TaskCreated a second time when it eventually starts.
        task_created_result = await _run_task_created_hook(
            hook_manager,
            task_id=subagent_id,
            subject=description,
            description=prompt,
            teammate_name=str(teammate_config.get("teammate_name") or agent_type),
            team_name=str(teammate_config.get("team_name") or ""),
        )
        task_created_blocked, task_created_message = _hook_veto(task_created_result)
        if task_created_blocked:
            return ToolResult(
                content=task_created_message,
                is_error=True,
                status="blocked",
                display_summary="Task creation blocked by hook",
                result_kind="subagent",
            )
        try:
            llm_resolution = await self._prepare_subagent_llm_resolution(
                context=context,
                agent_type=agent_type,
                subagent_metadata=subagent_metadata,
            )
        except ValueError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                status="blocked",
                display_summary="Invalid subagent model configuration",
                result_kind="subagent",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                content=f"Subagent LLM initialization failed: {exc}",
                is_error=True,
                status="failed",
                display_summary="Subagent LLM initialization failed",
                result_kind="subagent",
            )
        try:
            subagent_id = self._spawn_background_subtask(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_metadata=subagent_metadata,
                subagent_id=subagent_id,
                wait_for_slot=True,
                task_created_checked=True,
                prepared_llm_resolution=llm_resolution,
            )
        except RuntimeError as exc:
            with suppress(Exception):
                await _close_subagent_llm_resolution(llm_resolution)
            path_conflict = "Agent path" in str(exc) and "already owned" in str(exc)
            return ToolResult(
                content=str(exc),
                is_error=True,
                status="blocked" if path_conflict else "failed",
                display_summary=(
                    "Duplicate agent task name"
                    if path_conflict
                    else "Subagent spawn failed"
                ),
                result_kind="subagent",
            )
        except BaseException:
            with suppress(Exception):
                await _close_subagent_llm_resolution(llm_resolution)
            raise
        await asyncio.sleep(0)
        result_metadata = {
            "subagent_id": subagent_id,
            "provider": str(llm_resolution.provider or ""),
            "model": str(llm_resolution.model or ""),
            "effort": str(llm_resolution.effort or ""),
        }
        try:
            result_metadata["output_file"] = str(
                runtime.execution_journal(subagent_id).path
            )
        except Exception:
            # Advisory model-facing metadata must not roll back a child that
            # has already been durably registered with the shared runtime.
            result_metadata["output_file"] = ""

        if is_teammate:
            teammate_name = str(teammate_config.get("teammate_name") or "")
            team_name = str(teammate_config.get("team_name") or "")
            return ToolResult(
                content=(
                    "Spawned successfully.\n"
                    f"agent_id: {subagent_id}\n"
                    f"name: {teammate_name}\n"
                    f"team_name: {team_name}\n"
                    "The agent is now running and will receive instructions via mailbox."
                ),
                display_summary=f"Teammate running: {teammate_name}",
                result_kind="subagent",
                status="teammate_spawned",
                runtime_metadata=result_metadata,
            )
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
            runtime_metadata=result_metadata,
        )

    async def _start_background_subtasks(
        self,
        *,
        tasks: list[dict[str, Any]],
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        subagent_ids = [f"subagent-{uuid4().hex[:8]}" for _ in tasks]
        started: list[tuple[str, dict[str, str]]] = []
        prepared_resolutions: list[_SubagentLLMResolution] = []
        runtime = require_runtime_from_context(context)
        hook_manager = (
            context.run_context.hook_manager
            if context is not None and context.run_context is not None
            else None
        )
        parent_run_id = str(metadata_from_context(context).get("run_id") or "").strip()
        try:
            # Validate every TaskCreated hook before queueing any worker.  A
            # parallel TaskTool call is one user operation; allowing the
            # first child to start and then returning a veto for the second
            # would leave a partially-created batch with no stable parent
            # result to reconcile.
            for subagent_id, task in zip(subagent_ids, tasks, strict=True):
                task_config = _subagent_metadata(task)
                task_created_result = await _run_task_created_hook(
                    hook_manager,
                    task_id=subagent_id,
                    subject=task["description"],
                    description=task["prompt"],
                    teammate_name=str(
                        task_config.get("teammate_name")
                        or task.get("agent_type", "general-purpose")
                    ),
                    team_name=str(task_config.get("team_name") or ""),
                )
                task_created_blocked, task_created_message = _hook_veto(task_created_result)
                if task_created_blocked:
                    return ToolResult(
                        content=(
                            f"Task creation blocked for {task['description']}: "
                            f"{task_created_message}"
                        ),
                        is_error=True,
                        status="blocked",
                        display_summary="Task creation blocked by hook",
                        result_kind="subagent",
                    )
            # Validate every canonical child path before creating a worker or
            # handing off an adapter. The same runtime method is called again
            # by registration as the final ownership fence.
            batch_paths: set[str] = set()
            for subagent_id, task in zip(subagent_ids, tasks, strict=True):
                canonical_task_name = _normalize_child_task_name(
                    task.get("_task_name")
                )
                candidate_path = runtime.validate_subagent_task_registration(
                    subagent_id,
                    parent_run_id=parent_run_id,
                    canonical_task_name=canonical_task_name,
                    agent_path_segment=canonical_task_name,
                )
                if candidate_path in batch_paths:
                    return ToolResult(
                        content=f"Agent path {candidate_path!r} is duplicated in this batch.",
                        is_error=True,
                        status="blocked",
                        display_summary="Duplicate agent task name",
                        result_kind="subagent",
                    )
                batch_paths.add(candidate_path)
            # Resolve every child config before publishing any member of a
            # multi-agent spawn so an auth or
            # adapter-factory failure cannot leave a partially-created queue.
            try:
                for task in tasks:
                    prepared_resolutions.append(
                        await self._prepare_subagent_llm_resolution(
                            context=context,
                            agent_type=task.get("agent_type", "general-purpose"),
                            subagent_metadata=_subagent_metadata(task),
                        )
                    )
            except ValueError as exc:
                for resolution in prepared_resolutions:
                    with suppress(Exception):
                        await _close_subagent_llm_resolution(resolution)
                return ToolResult(
                    content=str(exc),
                    is_error=True,
                    status="blocked",
                    display_summary="Invalid subagent model configuration",
                    result_kind="subagent",
                )
            except Exception as exc:  # noqa: BLE001
                for resolution in prepared_resolutions:
                    with suppress(Exception):
                        await _close_subagent_llm_resolution(resolution)
                return ToolResult(
                    content=f"Subagent LLM initialization failed: {exc}",
                    is_error=True,
                    status="failed",
                    display_summary="Subagent LLM initialization failed",
                    result_kind="subagent",
                )
            for subagent_id, task, resolution in zip(
                subagent_ids,
                tasks,
                prepared_resolutions,
                strict=True,
            ):
                self._spawn_background_subtask(
                    description=task["description"],
                    prompt=task["prompt"],
                    agent_type=task.get("agent_type", "general-purpose"),
                    context=context,
                    subagent_metadata=_subagent_metadata(task),
                    subagent_id=subagent_id,
                    wait_for_slot=True,
                    task_created_checked=True,
                    prepared_llm_resolution=resolution,
                )
                started.append((subagent_id, task))
        except Exception as exc:  # noqa: BLE001
            # No await occurs during the publication loop, so none of the
            # created workers can start before this rollback runs. Cancel every
            # published task and close every prepared adapter as one failed
            # batch instead of leaving an invisible partial spawn behind.
            for subagent_id, _task in started:
                with suppress(Exception):
                    runtime.cancel_subagent_task(
                        subagent_id,
                        reason="batch_spawn_failed",
                    )
            for resolution in prepared_resolutions:
                try:
                    await _close_subagent_llm_resolution(resolution)
                except Exception:
                    logger.warning(
                        "Failed closing a rolled-back queued child LLM adapter",
                        exc_info=True,
                    )
            return ToolResult(
                content=f"Subagent batch could not be published: {exc}",
                is_error=True,
                status="failed",
                display_summary="Subagent batch spawn failed",
                result_kind="subagent",
            )
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
        resume_snapshot: dict[str, Any] | None = None,
        resume_workspace_root: Path | None = None,
        task_created_checked: bool = False,
        prepared_llm_resolution: _SubagentLLMResolution | None = None,
        start_acknowledged: asyncio.Future[str] | None = None,
    ) -> str:
        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        runtime = require_runtime_from_context(context)
        subagent_cancel_event = asyncio.Event()

        async def _run_background() -> ToolResult:
            resolution_handed_off = False
            try:
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
                resolution_handed_off = prepared_llm_resolution is not None
                return await self._run_single_subtask(
                    description=description,
                    prompt=prompt,
                    agent_type=agent_type,
                    context=context,
                    subagent_id=subagent_id,
                    cancel_event=subagent_cancel_event,
                    subagent_metadata=subagent_metadata,
                    background=True,
                    resume_snapshot=resume_snapshot,
                    resume_workspace_root=resume_workspace_root,
                    task_created_checked=task_created_checked,
                    prepared_llm_resolution=prepared_llm_resolution,
                    start_acknowledged=start_acknowledged,
                )
            finally:
                if prepared_llm_resolution is not None and not resolution_handed_off:
                    try:
                        await _close_subagent_llm_resolution(prepared_llm_resolution)
                    except Exception:
                        logger.warning(
                            "Failed closing queued child LLM adapter for %s",
                            prepared_llm_resolution.model,
                            exc_info=True,
                        )

        task = asyncio.create_task(_run_background())
        parent_metadata = metadata_from_context(context)
        parent_run_id = str(parent_metadata.get("run_id", "")).strip()
        canonical_task_name = _normalize_child_task_name(
            (subagent_metadata or {}).get("_task_name")
            if isinstance(subagent_metadata, dict)
            else ""
        )
        try:
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
                canonical_task_name=canonical_task_name,
                agent_path_segment=canonical_task_name,
            )
            task_metadata = getattr(runtime, "_subagent_task_metadata", None)
            if isinstance(task_metadata, dict):
                task_metadata_entry = task_metadata.setdefault(subagent_id, {})
                task_metadata_entry.update(
                    {
                        key: value
                        for key, value in _subagent_metadata(subagent_metadata).items()
                        if key in {
                            "team_mode",
                            "plan_mode_required",
                            "plan_slug",
                            "team_name",
                            "teammate_name",
                            "teammate_id",
                            "mode",
                        }
                        and value not in ("", None)
                    }
                )
                raw_profile = (
                    (subagent_metadata or {}).get("_execution_profile")
                    if isinstance(subagent_metadata, dict)
                    else None
                )
                if isinstance(raw_profile, AgentExecutionProfile):
                    task_metadata_entry["execution_profile"] = raw_profile.to_dict()
                elif isinstance(raw_profile, dict):
                    task_metadata_entry["execution_profile"] = (
                        AgentExecutionProfile.from_mapping(raw_profile).to_dict()
                    )
                if canonical_task_name:
                    task_metadata_entry["task_name"] = canonical_task_name
        except BaseException:
            # create_task() does not run the coroutine until control returns to
            # the event loop. If publication fails, cancel the unpublished
            # worker immediately and remove any partial runtime registration;
            # the async caller still owns and closes the prepared adapter.
            task.cancel()
            try:
                runtime.release_subagent_task(subagent_id, expected_task=task)
            except Exception:
                logger.exception(
                    "Failed to release unpublished background subtask %s",
                    subagent_id,
                )
            try:
                runtime.release_subagent_slot(subagent_id)
            except Exception:
                logger.exception(
                    "Failed to release capacity slot for unpublished background subtask %s",
                    subagent_id,
                )
            raise

        def _release_background_task(done_task: asyncio.Task[ToolResult]) -> None:
            completed_result: ToolResult | None = None
            completed_error = ""
            try:
                completed_result = done_task.result()
            except asyncio.CancelledError:
                completed_error = "Background subagent was cancelled before it started."
            except Exception as exc:  # noqa: BLE001
                completed_error = f"Background subagent failed before it started: {exc}"
            if start_acknowledged is not None and not start_acknowledged.done():
                start_acknowledged.set_result(
                    completed_error
                    or str(
                        getattr(completed_result, "content", "")
                        or "Background subagent ended before claiming its runtime identity."
                    )
                )
            try:
                runtime.release_subagent_task(subagent_id, expected_task=done_task)
            except Exception:
                logger.exception(
                    "Failed to release completed background subtask %s",
                    subagent_id,
                )
            try:
                runtime.release_subagent_slot(subagent_id)
            except Exception:
                logger.exception(
                    "Failed to release capacity slot for completed background subtask %s",
                    subagent_id,
                )
            if completed_error and not done_task.cancelled():
                # _run_single_subtask reports failures via subagent.done. The
                # callback only prevents an unhandled task exception warning.
                logger.debug(
                    "Background subtask %s completed with an error after reporting its failure",
                    subagent_id,
                    exc_info=True,
                )

        task.add_done_callback(_release_background_task)
        return subagent_id

    async def resume_background_subtask(
        self,
        *,
        subagent_id: str,
        prompt: str,
        context: ToolExecutionContext | None,
    ) -> str:
        """Resume a stopped agent from its canonical context checkpoint."""

        runtime = require_runtime_from_context(context)
        record = runtime.get_subagent(subagent_id)
        if record is not None and record.status == "running":
            raise RuntimeError(f"Subagent {subagent_id} is already running.")
        if record is None:
            load_persisted = getattr(runtime, "load_persisted_subagent", None)
            record = load_persisted(subagent_id) if callable(load_persisted) else None
        if record is None:
            raise RuntimeError(
                f"Subagent {subagent_id} has no durable runtime record to resume."
            )

        from backend.agent.checkpoint import load_latest_run_checkpoint

        conversation_id = str(
            getattr(context, "conversation_id", "") if context is not None else ""
        ).strip()
        checkpoint = load_latest_run_checkpoint(
            subagent_id,
            conversation_id=conversation_id or None,
        )
        if checkpoint is None or not isinstance(checkpoint.context_snapshot, dict):
            raise RuntimeError(
                f"Subagent {subagent_id} has no canonical context checkpoint to resume."
            )
        resume_snapshot = dict(checkpoint.context_snapshot)
        if not resume_snapshot:
            raise RuntimeError(
                f"Subagent {subagent_id} has an empty context checkpoint."
            )

        previous_config: dict[str, Any] = dict(record.resume_config)
        runtime_config = runtime.get_subagent_task_metadata(subagent_id) or {}
        previous_config.update(runtime_config)

        agent_type = record.agent_type
        prompt_summary = record.prompt_summary or f"Resume {agent_type}"

        resume_workspace_root: Path | None = None
        if str(previous_config.get("isolation") or "") == "worktree":
            raw_worktree = str(previous_config.get("worktree_path") or "").strip()
            current_root = Path(context.workspace_root).resolve() if context and context.workspace_root else None
            if not raw_worktree or current_root is None:
                raise RuntimeError(
                    f"Subagent {subagent_id} used worktree isolation, but its worktree is unavailable."
                )
            candidate = Path(raw_worktree).resolve()
            allowed_root = (current_root / ".minicode" / "worktrees").resolve()
            try:
                candidate.relative_to(allowed_root)
            except ValueError as exc:
                raise RuntimeError(
                    f"Subagent {subagent_id} has an invalid persisted worktree path."
                ) from exc
            if not candidate.is_dir():
                raise RuntimeError(
                    f"Subagent {subagent_id}'s persisted worktree no longer exists."
                )
            resume_workspace_root = candidate

        raw_persisted_cwd = str(previous_config.get("cwd") or "").strip()
        if raw_persisted_cwd:
            current_root = (
                Path(context.workspace_root).resolve()
                if context and context.workspace_root
                else None
            )
            candidate = Path(raw_persisted_cwd).expanduser().resolve()
            if current_root is None:
                raise RuntimeError(
                    f"Subagent {subagent_id} used a cwd override without an active workspace."
                )
            try:
                candidate.relative_to(current_root)
            except ValueError as exc:
                raise RuntimeError(
                    f"Subagent {subagent_id} has an invalid persisted cwd."
                ) from exc
            if not candidate.is_dir():
                raise RuntimeError(
                    f"Subagent {subagent_id}'s persisted cwd no longer exists."
                )

        subagent_metadata = {
            "read_only": bool(
                record.read_only
            ),
            "write_scope": list(
                record.write_scope
            ),
            "cancel_with_parent": bool(
                record.cancel_with_parent
            ),
            "detach_from_parent": bool(
                record.detach_from_parent
            ),
            "team_mode": bool(previous_config.get("team_mode")),
            "plan_mode_required": bool(previous_config.get("plan_mode_required")),
            "plan_slug": str(previous_config.get("plan_slug") or ""),
            "team_name": str(previous_config.get("team_name") or ""),
            "cwd": str(previous_config.get("cwd") or ""),
            **(
                {"isolation": "worktree"}
                if str(previous_config.get("isolation") or "") == "worktree"
                else {}
            ),
            **(
                {
                    "model": (
                        f"{previous_config.get('provider')}/{previous_config['model']}"
                        if previous_config.get("provider")
                        else str(previous_config["model"])
                    )
                }
                if previous_config.get("model")
                else {}
            ),
            **(
                {
                    "reasoning_effort": str(
                        previous_config.get("reasoning_effort")
                        or previous_config.get("effort")
                    )
                }
                # A legacy/bare adapter may have no resolvable provider/model.
                # Its persisted "off" value was inherited state, not an
                # explicit override. Replaying effort without a model would
                # manufacture an invalid child configuration on resume.
                if previous_config.get("model")
                and (
                    previous_config.get("reasoning_effort")
                    or previous_config.get("effort")
                )
                else {}
            ),
        }
        if "session_toolset_policy" in previous_config:
            try:
                subagent_metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = (
                    restore_toolset_policy(
                        previous_config["session_toolset_policy"],
                        label="persisted child session tool capability policy",
                    )
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"Subagent {subagent_id} has an invalid persisted tool capability policy."
                ) from exc
        persisted_profile = previous_config.get("execution_profile")
        if isinstance(persisted_profile, dict):
            # Internal-only key: model-authored Task arguments are normalized
            # through ``_subagent_metadata`` and cannot set execution
            # capabilities.  Durable resume must nevertheless restore the
            # original profile so a hierarchical child does not silently lose
            # delegation capabilities after recovery.
            subagent_metadata["_execution_profile"] = (
                execution_profile_for_background_resume(
                    AgentExecutionProfile.from_mapping(persisted_profile)
                )
            )
        persisted_task_name = str(previous_config.get("task_name") or "").strip()
        if persisted_task_name:
            subagent_metadata["_task_name"] = persisted_task_name
        llm_resolution = await self._prepare_subagent_llm_resolution(
            context=context,
            agent_type=agent_type,
            subagent_metadata=subagent_metadata,
        )
        acquired = await runtime.acquire_subagent_slot(
            subagent_id,
            cancel_event=context.cancel_event if context is not None else None,
        )
        if not acquired:
            with suppress(Exception):
                await _close_subagent_llm_resolution(llm_resolution)
            raise RuntimeError(f"Subagent {subagent_id} resume was cancelled before it started.")
        spawned = False
        start_acknowledged = asyncio.get_running_loop().create_future()
        try:
            self._spawn_background_subtask(
                description=prompt_summary,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subagent_metadata=subagent_metadata,
                subagent_id=subagent_id,
                wait_for_slot=False,
                resume_snapshot=resume_snapshot,
                resume_workspace_root=resume_workspace_root,
                task_created_checked=True,
                prepared_llm_resolution=llm_resolution,
                start_acknowledged=start_acknowledged,
            )
            spawned = True
            # Do not infer startup from one event-loop yield.  The explicit
            # handshake is completed only after ``start_subagent`` has claimed
            # the next mailbox epoch, so the caller can never observe a stale
            # incarnation immediately after a successful resume.
            start_error = await start_acknowledged
            if start_error:
                raise RuntimeError(start_error)
        except BaseException:
            if not spawned:
                runtime.release_subagent_slot(subagent_id)
                with suppress(Exception):
                    await _close_subagent_llm_resolution(llm_resolution)
            raise
        return subagent_id

    async def _prepare_subagent_llm_resolution(
        self,
        *,
        context: ToolExecutionContext | None,
        agent_type: str,
        subagent_metadata: dict[str, Any] | None,
    ) -> _SubagentLLMResolution:
        """Build the canonical child config before the child is published."""

        parent_metadata = self._metadata_from_context(context)
        run_context = context.run_context if context is not None else None
        inherited_llm = (
            (run_context.subagent_parent_runtime.get("llm") if run_context else None)
            or (context.llm if context is not None else None)
            or self._resolve_llm()
        )
        requested = _subagent_metadata(subagent_metadata)
        return await _resolve_subagent_llm(
            inherited_llm,
            parent_metadata=parent_metadata,
            run_context=run_context,
            agent_type=agent_type,
            model_override=str(requested.get("model") or ""),
            effort_override=str(requested.get("effort") or ""),
            workspace_root=context.workspace_root if context is not None else None,
            build_adapter=True,
        )

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
        resume_snapshot: dict[str, Any] | None = None,
        resume_workspace_root: Path | None = None,
        task_created_checked: bool = False,
        prepared_llm_resolution: _SubagentLLMResolution | None = None,
        start_acknowledged: asyncio.Future[str] | None = None,
    ) -> ToolResult:
        llm_resolution = prepared_llm_resolution
        try:
            if llm_resolution is None:
                llm_resolution = await self._prepare_subagent_llm_resolution(
                    context=context,
                    agent_type=agent_type,
                    subagent_metadata=subagent_metadata,
                )
        except ValueError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                status="blocked",
                display_summary="Invalid subagent model configuration",
                result_kind="subagent",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                content=f"Subagent LLM initialization failed: {exc}",
                is_error=True,
                status="failed",
                display_summary="Subagent LLM initialization failed",
                result_kind="subagent",
            )

        assert llm_resolution is not None
        effective_subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        parent_metadata = self._metadata_from_context(context)
        rollout_budget = parent_metadata.get("_rollout_budget")
        reservation_id = effective_subagent_id
        reserved_quota = 0
        if isinstance(rollout_budget, RolloutBudget) and rollout_budget.token_limit > 0:
            child_settings = (
                llm_resolution.config.agent
                if isinstance(getattr(llm_resolution.config, "agent", None), AgentSettings)
                else self._resolve_agent_settings()
            )
            requested_quota = max(0, int(child_settings.max_turn_tokens or 0))
            reserved_quota = rollout_budget.reserve(
                reservation_id,
                requested_quota,
            )
            if reserved_quota <= 0:
                if llm_resolution.owns_llm:
                    with suppress(Exception):
                        await _close_subagent_llm_resolution(llm_resolution)
                return ToolResult(
                    content=(
                        "Subagent was not started because the root rollout token "
                        "budget has no unreserved capacity."
                    ),
                    is_error=True,
                    status="blocked",
                    display_summary="Root rollout budget exhausted",
                    result_kind="subagent",
                )
            if requested_quota <= 0 or reserved_quota < requested_quota:
                bounded_settings = replace(
                    child_settings,
                    max_turn_tokens=reserved_quota,
                )
                llm_resolution = replace(
                    llm_resolution,
                    config=replace(
                        llm_resolution.config,
                        agent=bounded_settings,
                    ),
                )
        try:
            return await self._run_single_subtask_impl(
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                context=context,
                subtask_index=subtask_index,
                total_subtasks=total_subtasks,
                subagent_id=effective_subagent_id,
                cancel_event=cancel_event,
                subagent_metadata=subagent_metadata,
                background=background,
                resume_snapshot=resume_snapshot,
                resume_workspace_root=resume_workspace_root,
                task_created_checked=task_created_checked,
                llm_resolution=llm_resolution,
                start_acknowledged=start_acknowledged,
                rollout_reservation_id=(reservation_id if reserved_quota > 0 else ""),
            )
        finally:
            if reserved_quota > 0:
                rollout_budget.release_reservation(reservation_id)
            if llm_resolution.owns_llm:
                try:
                    await _close_subagent_llm_resolution(llm_resolution)
                except Exception:
                    logger.debug(
                        "Failed closing child LLM adapter for %s",
                        llm_resolution.model,
                        exc_info=True,
                    )

    async def _run_single_subtask_impl(
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
        resume_snapshot: dict[str, Any] | None = None,
        resume_workspace_root: Path | None = None,
        task_created_checked: bool = False,
        llm_resolution: _SubagentLLMResolution,
        start_acknowledged: asyncio.Future[str] | None = None,
        rollout_reservation_id: str = "",
    ) -> ToolResult:
        """Run one isolated subagent loop with progress reporting.

        Returns a structured ``ToolResult`` that includes the subagent summary,
        duration, iteration count, and tool-call statistics.
        """
        llm = llm_resolution.llm
        tool_registry = self._resolve_tool_registry()
        permission_checker = (
            context.permission_checker
            if context is not None and context.permission_checker is not None
            else self._resolve_permission_checker()
        )

        subagent_id = subagent_id or f"subagent-{uuid4().hex[:8]}"
        parent_id = context.task_id if context and context.task_id else context.session_id if context else ""
        emit_event = context.emit_event if context else None
        runtime = require_runtime_from_context(context)
        parent_metadata = self._metadata_from_context(context)
        parent_run_context = context.run_context
        subagent_config = _subagent_metadata(subagent_metadata)
        restored_session_policy = (
            (subagent_metadata or {}).get(SESSION_TOOLSET_POLICY_METADATA_KEY)
            if isinstance(subagent_metadata, dict)
            else None
        )
        if restored_session_policy is not None:
            try:
                restored_session_policy = restore_toolset_policy(
                    restored_session_policy,
                    label="child session tool capability policy",
                )
            except ValueError as exc:
                raise RuntimeError(
                    "child session tool capability policy is invalid"
                ) from exc
        child_task_name = _normalize_child_task_name(
            (subagent_metadata or {}).get("_task_name")
            if isinstance(subagent_metadata, dict)
            else ""
        )
        fork_turns = _normalize_fork_turns(
            (subagent_metadata or {}).get("_fork_turns", "none")
            if isinstance(subagent_metadata, dict)
            else "none"
        )
        fork_snapshot = (
            None
            if resume_snapshot
            else _fork_snapshot_for_child(parent_metadata, fork_turns)
        )
        # Journal and UI events carry the actual resolved child config, not the
        # request spelling. Resume can therefore recreate the same durable
        # child even if the parent's active model later changes.
        subagent_config["provider"] = llm_resolution.provider
        subagent_config["model"] = llm_resolution.model
        subagent_config["effort"] = llm_resolution.effort
        subagent_config["team_mode"] = team_mode = _is_team_subagent(subagent_config)
        raw_execution_profile = (
            (subagent_metadata or {}).get("_execution_profile")
            if isinstance(subagent_metadata, dict)
            else None
        )
        if isinstance(raw_execution_profile, AgentExecutionProfile):
            execution_profile = raw_execution_profile
        elif isinstance(raw_execution_profile, dict):
            execution_profile = AgentExecutionProfile.from_mapping(
                raw_execution_profile
            )
        else:
            execution_profile = build_agent_execution_profile(
                team_mode=team_mode,
                background=background,
                agent_triggers_enabled=feature_enabled("agent_triggers"),
            )
        if execution_profile.team_mode != team_mode:
            raise ValueError(
                "child execution profile role does not match teammate ownership metadata"
            )
        if background and execution_profile.delivery not in {"background", "persistent"}:
            raise ValueError(
                "background child requires a background or persistent execution profile"
            )
        if not background and execution_profile.delivery != "foreground":
            raise ValueError(
                "foreground child requires a foreground execution profile"
            )
        subagent_config["execution_profile"] = execution_profile.to_dict()
        parent_session_policy = _persisted_session_toolset_policy(parent_metadata)
        if restored_session_policy is not None:
            parent_session_policy = restored_session_policy.to_mapping()
        if parent_session_policy is not None:
            subagent_config["session_toolset_policy"] = parent_session_policy
        if child_task_name:
            subagent_config["task_name"] = child_task_name
        if fork_turns != "none":
            subagent_config["fork_turns"] = fork_turns
        # A child owns its cancellation signal. Reusing the parent's event lets a
        # child deadline cancel the parent and every sibling sharing that context.
        subagent_cancel_event = cancel_event or asyncio.Event()
        parent_run_id = str(parent_metadata.get("run_id", ""))

        # TaskCreated is a gate, not an audit-only notification. Run it before
        # allocating the child runtime/worktree so a veto cannot
        # leave a half-created task behind.  Background callers perform this
        # gate before queueing; foreground callers do it here.
        if not task_created_checked:
            task_created_hook_result = await _run_task_created_hook(
                parent_run_context.hook_manager,
                task_id=subagent_id,
                subject=description,
                description=prompt,
                teammate_name=agent_type,
            )
            task_created_blocked, task_created_message = _hook_veto(task_created_hook_result)
            if task_created_blocked:
                return ToolResult(
                    content=task_created_message,
                    is_error=True,
                    status="blocked",
                    display_summary="Task creation blocked by hook",
                    result_kind="subagent",
                )
        try:
            # Explicit background work is detached by default. Explicit flags
            # always win.
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
                task_id=child_task_name,
                session_id=str(getattr(context, "session_id", "") or ""),
                objective=description,
                cancel_with_parent=cancel_with_parent,
                detach_from_parent=detach_from_parent,
                read_only=subagent_config["read_only"],
                write_scope=subagent_config["write_scope"],
                resume_config={
                    "read_only": bool(subagent_config.get("read_only")),
                    "write_scope": list(subagent_config.get("write_scope") or []),
                    "cancel_with_parent": bool(cancel_with_parent),
                    "detach_from_parent": bool(detach_from_parent),
                    "isolation": str(subagent_config.get("isolation") or ""),
                    "cwd": str(subagent_config.get("cwd") or ""),
                    "provider": str(subagent_config.get("provider") or ""),
                    "model": str(subagent_config.get("model") or ""),
                    "reasoning_effort": str(subagent_config.get("effort") or ""),
                    "team_mode": bool(subagent_config.get("team_mode")),
                    "plan_mode_required": bool(
                        subagent_config.get("plan_mode_required")
                    ),
                    "plan_slug": str(subagent_config.get("plan_slug") or ""),
                    "team_name": str(subagent_config.get("team_name") or ""),
                    "teammate_name": str(
                        subagent_config.get("teammate_name") or ""
                    ),
                    "mode": str(subagent_config.get("mode") or ""),
                    "execution_profile": execution_profile.to_dict(),
                    "session_toolset_policy": parent_session_policy,
                    "task_name": child_task_name,
                    "fork_turns": fork_turns,
                },
                teammate_name=str(subagent_config.get("teammate_name") or ""),
                team_name=str(subagent_config.get("team_name") or ""),
                permission_mode=str(subagent_config.get("mode") or "confirm"),
                plan_mode_required=bool(subagent_config.get("plan_mode_required")),
                agent_path_segment=child_task_name,
            ) if runtime is not None else None
            if team_mode and runtime is not None:
                team = runtime.add_swarm_team_member(
                    conversation_id=str(getattr(context, "conversation_id", "") or ""),
                    team_name=str(subagent_config.get("team_name") or ""),
                    member={
                        "id": subagent_id,
                        "role": str(subagent_config.get("teammate_name") or ""),
                        "agent_type": agent_type,
                        "description": description,
                    },
                )
                if team is None:
                    runtime.complete_subagent(
                        subagent_id,
                        "failed",
                        summary="Team disappeared before teammate start",
                        agent_path=str(getattr(subagent_record, "agent_path", "") or ""),
                        mailbox_epoch=int(getattr(subagent_record, "mailbox_epoch", 0) or 0),
                    )
                    return ToolResult(
                        content=f'Team "{subagent_config.get("team_name")}" no longer exists.',
                        is_error=True,
                        status="failed",
                        display_summary="Teammate team missing",
                        result_kind="subagent",
                    )
                runtime_metadata = getattr(runtime, "_subagent_task_metadata", None)
                if isinstance(runtime_metadata, dict):
                    metadata_entry = runtime_metadata.setdefault(subagent_id, {})
                    metadata_entry.update(
                        {
                            "team_mode": True,
                            "plan_mode_required": bool(
                                subagent_config.get("plan_mode_required")
                            ),
                            "plan_slug": str(subagent_config.get("plan_slug") or ""),
                            "team_name": str(subagent_config.get("team_name") or ""),
                            "teammate_name": str(subagent_config.get("teammate_name") or ""),
                            "teammate_id": subagent_id,
                            "mode": str(subagent_config.get("mode") or "confirm"),
                        }
                    )
        except RuntimeError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                status="blocked",
                display_summary="Subagent capacity reached",
                result_kind="subagent",
            )

        if start_acknowledged is not None and not start_acknowledged.done():
            start_acknowledged.set_result("")

        subagent_fence = {
            "agent_path": str(getattr(subagent_record, "agent_path", "") or ""),
            "mailbox_epoch": int(getattr(subagent_record, "mailbox_epoch", 0) or 0),
        }
        if team_mode and not str(subagent_config.get("plan_slug") or ""):
            from backend.agent.plans import bind_plan_owner, plan_slug_from_snapshot

            repository = parent_run_context.conversation_repository
            if repository is not None and context is not None:
                parent_record = repository.get_conversation(context.conversation_id)
                parent_snapshot = dict(
                    getattr(parent_record, "context_snapshot", {}) or {}
                )
                slug = plan_slug_from_snapshot(parent_snapshot)
                if not slug:
                    slug, _plan_path = bind_plan_owner(
                        repository,
                        context.conversation_id,
                        context.workspace_root,
                    )
                subagent_config["plan_slug"] = str(slug or "")
                runtime_metadata = getattr(runtime, "_subagent_task_metadata", None)
                if isinstance(runtime_metadata, dict):
                    runtime_metadata.setdefault(subagent_id, {})["plan_slug"] = str(
                        slug or ""
                    )
        journal_events: list[dict[str, Any]] = []
        journal: ExecutionJournal | None = None
        cached_transcript_seq = -1
        cached_transcript_messages: list[dict[str, Any]] = []
        emitted_transcript_seq = 0

        def _record_journal_events(*events: Any) -> None:
            for event in events:
                if event is None:
                    continue
                to_dict = getattr(event, "to_dict", None)
                payload = to_dict() if callable(to_dict) else event
                if isinstance(payload, dict):
                    journal_events.append(dict(payload))

        def _current_transcript_snapshot() -> dict[str, Any] | None:
            nonlocal cached_transcript_seq, cached_transcript_messages
            source_events = journal_events
            if journal is not None:
                source_events = [event.to_dict() for event in journal.read_events()]
                journal_events[:] = source_events
            if not source_events:
                return None
            try:
                seq = max(int(event.get("seq") or 0) for event in source_events)
            except (TypeError, ValueError):
                seq = len(source_events)
            if seq != cached_transcript_seq:
                from backend.services.subagent_service import build_subagent_transcript_messages

                cached_transcript_messages = build_subagent_transcript_messages(
                    {"events": source_events},
                )
                cached_transcript_seq = seq
            # Event delivery may enqueue this payload while the child continues
            # to append journal entries. Hand transport its own list object so a
            # later cache refresh can never mutate an already-emitted snapshot.
            return {"seq": seq, "messages": list(cached_transcript_messages)}

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
            transcript_snapshot: dict[str, Any] | None = None,
        ) -> bool:
            nonlocal emitted_transcript_seq
            if emit_event is None or not _accepts_current_incarnation(
                require_running=require_running
            ):
                return False
            payload = {**data, **subagent_fence}
            if transcript_snapshot is None:
                transcript_snapshot = _current_transcript_snapshot()
            if transcript_snapshot is not None:
                transcript_seq = int(transcript_snapshot.get("seq") or 0)
                if transcript_seq > emitted_transcript_seq or event_type == "subagent.done":
                    payload["transcript_snapshot"] = transcript_snapshot
                    emitted_transcript_seq = max(emitted_transcript_seq, transcript_seq)
            await emit_event(event_type, payload)
            return True

        # Worktree isolation
        # Created after the capacity check so a blocked delegation never leaves
        # a stray worktree behind. Isolation is fail-closed: a caller that asks
        # for a worktree must never silently run in the shared workspace.
        agent_worktree = None
        parent_workspace_root = (
            Path(context.workspace_root)
            if context is not None and context.workspace_root
            else Path.cwd()
        )
        explicit_child_workspace = None
        raw_child_cwd = str(subagent_config.get("cwd") or "").strip()
        if raw_child_cwd:
            explicit_child_workspace = Path(raw_child_cwd).resolve()
        if subagent_config.get("isolation") == "worktree" and resume_workspace_root is None:
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
            if runtime is not None and not runtime.register_subagent_cleanup_resource(
                subagent_id,
                resource_kind="worktree",
                resource_id=str(agent_worktree.worktree_path),
                metadata={
                    "git_root": str(agent_worktree.git_root),
                    "branch": agent_worktree.branch,
                    "head_commit": agent_worktree.head_commit,
                },
            ):
                from backend.agent.worktree import cleanup_agent_worktree

                await asyncio.to_thread(cleanup_agent_worktree, agent_worktree)
                runtime.complete_subagent(
                    subagent_id,
                    "failed",
                    summary="Worktree ownership could not be persisted.",
                    **subagent_fence,
                )
                return ToolResult(
                    content="Worktree ownership could not be persisted; the subagent was not started.",
                    is_error=True,
                    status="failed",
                    display_summary="Worktree ownership persistence failed",
                    result_kind="subagent",
                )

        async def _cleanup_worktree() -> str:
            """Remove the worktree when unchanged; return a keep-note otherwise."""
            nonlocal agent_worktree
            if agent_worktree is None:
                return ""
            from backend.agent.worktree import cleanup_agent_worktree, has_worktree_changes

            info, agent_worktree = agent_worktree, None
            try:
                had_changes = await asyncio.to_thread(has_worktree_changes, info)
                kept, kept_path = await asyncio.to_thread(cleanup_agent_worktree, info)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Worktree cleanup failed for %s: %s", subagent_id, exc)
                return f"Worktree left at {info.worktree_path} (branch {info.branch}); cleanup failed."
            if runtime is not None and (not kept or had_changes):
                runtime.settle_subagent_cleanup_resource(
                    subagent_id,
                    resource_kind="worktree",
                    resource_id=str(info.worktree_path),
                    state="retained" if kept else "released",
                    receipt="retained_user_changes" if kept else "removed_clean_worktree",
                )
            if kept:
                return (
                    f"The subagent worked in an isolated git worktree with changes kept at: "
                    f"{kept_path} (branch {info.branch})."
                )
            return ""

        if runtime is not None and subagent_record is not None:
            durable_resume_config = dict(subagent_record.resume_config)
            durable_resume_config.update(
                {
                    "cancel_with_parent": bool(subagent_record.cancel_with_parent),
                    "detach_from_parent": bool(subagent_record.detach_from_parent),
                    "plan_slug": str(subagent_config.get("plan_slug") or ""),
                    "worktree_path": str(
                        resume_workspace_root
                        or (
                            agent_worktree.worktree_path
                            if agent_worktree is not None
                            else ""
                        )
                    ),
                }
            )
            updated_record = runtime.update_subagent_resume_config(
                subagent_id,
                durable_resume_config,
                **subagent_fence,
            )
            if updated_record is None:
                await _cleanup_worktree()
                runtime.complete_subagent(
                    subagent_id,
                    "failed",
                    summary="Resume configuration could not be persisted.",
                    **subagent_fence,
                )
                return ToolResult(
                    content="Subagent resume configuration could not be persisted.",
                    is_error=True,
                    status="failed",
                    display_summary="Subagent persistence failed",
                    result_kind="subagent",
                )
            subagent_record = updated_record

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
            if child_task_name:
                start_event.data["task_name"] = child_task_name
            if fork_turns != "none":
                start_event.data["fork_turns"] = fork_turns
            if subagent_record is not None:
                start_event.data["record"] = subagent_record.public_dict()
                start_event.data["parent_run_id"] = parent_run_id
            await _emit_incarnation_event("subagent.start", start_event.data)
        start_hook_result = await _run_subagent_start_hook(
            parent_run_context.hook_manager,
            subagent_id,
            agent_type,
        )
        start_hook_blocked, start_hook_message = _hook_veto(start_hook_result)
        if start_hook_blocked:
            raise RuntimeError(start_hook_message)

        delegated_prompt = self._build_subagent_prompt(
            agent_type,
            prompt,
            workspace_root=(
                explicit_child_workspace
                or (context.workspace_root if context is not None else None)
            ),
        )
        hook_context = str(
            getattr(start_hook_result, "additional_context", "") or ""
        ).strip()
        if hook_context:
            delegated_prompt = (
                f"{delegated_prompt}\n\n"
                "Additional context from the SubagentStart hook:\n"
                f"{hook_context}"
            )
        effective_user_prompt = prompt if resume_snapshot else delegated_prompt
        if team_mode and not resume_snapshot:
            effective_user_prompt = _format_teammate_message(
                "team-lead",
                delegated_prompt,
                summary=description,
            )
        journal_user_metadata: dict[str, Any] = {}
        if runtime is not None:
            try:
                journal = runtime.execution_journal(subagent_id)
                journal_events[:] = [event.to_dict() for event in journal.read_events()]
                journal_user_metadata = {
                        "provider_content": effective_user_prompt,
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
                        "read_only": bool(subagent_config.get("read_only")),
                        "write_scope": list(subagent_config.get("write_scope") or []),
                        "isolation": str(subagent_config.get("isolation") or ""),
                        "cwd": str(subagent_config.get("cwd") or ""),
                        "provider": str(subagent_config.get("provider") or ""),
                        "model": str(subagent_config.get("model") or ""),
                        "reasoning_effort": str(subagent_config.get("effort") or ""),
                        "team_mode": bool(subagent_config.get("team_mode")),
                        "plan_mode_required": bool(
                            subagent_config.get("plan_mode_required")
                        ),
                        "mode": str(subagent_config.get("mode") or ""),
                        "execution_profile": execution_profile.to_dict(),
                        "task_name": child_task_name,
                        "fork_turns": fork_turns,
                        "team_name": str(subagent_config.get("team_name") or ""),
                        "teammate_name": str(
                            subagent_config.get("teammate_name") or ""
                        ),
                        "plan_slug": str(subagent_config.get("plan_slug") or ""),
                        "parent_run_id": parent_run_id,
                        "conversation_id": str(
                            getattr(context, "conversation_id", "") or ""
                        ),
                        "session_id": str(
                            getattr(context, "session_id", "") or ""
                        ),
                        "worktree_path": str(
                            resume_workspace_root
                            or (agent_worktree.worktree_path if agent_worktree is not None else "")
                        ),
                        "worktree_branch": str(
                            agent_worktree.branch if agent_worktree is not None else ""
                        ),
                }
            except Exception as journal_exc:
                raise RuntimeError(
                    f"Subagent journal could not be opened for {subagent_id}; "
                    "the child was not started."
                ) from journal_exc

        sub_settings = (
            llm_resolution.config.agent
            if isinstance(getattr(llm_resolution.config, "agent", None), AgentSettings)
            else self._resolve_agent_settings()
        )
        sub_budget = (
            llm_resolution.config.token_budget
            if isinstance(getattr(llm_resolution.config, "token_budget", None), TokenBudget)
            else self._resolve_token_budget()
        )
        # Apply a custom agent's tool restrictions (Agent editor). A custom
        # definition can declare a tools whitelist and/or disallowed_tools; those
        # must actually be enforced at runtime (deny rules block the call), not
        # just stored on the definition.
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
            team_mode=team_mode,
            background=background,
            plan_mode_required=bool(subagent_config.get("plan_mode_required")),
            requested_mode=str(subagent_config.get("mode") or ""),
            agent_triggers_enabled=feature_enabled("agent_triggers"),
            execution_profile=execution_profile,
        )
        lifecycle_owner = _SubagentLifecycleOwner(
            subagent_id=subagent_id,
            agent_type=agent_type,
            runtime=runtime,
            hook_manager=parent_run_context.hook_manager,
            team_mode=team_mode,
            teammate_name=str(subagent_config.get("teammate_name") or ""),
            team_name=str(subagent_config.get("team_name") or ""),
            conversation_id=str(getattr(context, "conversation_id", "") or ""),
            subject=description,
        )
        sub_state = AgentState(
            user_message=effective_user_prompt,
            max_iterations=sub_settings.max_iterations,
        )
        # The prompt/runtime surface is derived from the execution profile;
        # ordinary children remain non-delegating while hierarchical profiles
        # may opt into child delegation.
        lifecycle_owner.bind_turn_state(sub_state)
        # Preserve the delegated role in the prompt context. Without this,
        # explore/plan children fall back to the parent's build-mode guidance
        # even though their permission profile is read-only.
        if agent_type in {"explore", "plan"}:
            sub_state.prompt_context["agent_mode"] = agent_type

        sub_state.workspace_context = parent_run_context.workspace_context
        if context is not None:
            sub_state.conversation_id = context.conversation_id
            sub_state.checkpoint_manager = context.checkpoint_manager
        parent_prompt_cache_safe_params = parent_metadata.get("prompt_cache_safe_params")
        effective_child_workspace = (
            resume_workspace_root
            or (agent_worktree.worktree_path if agent_worktree is not None else None)
            or explicit_child_workspace
        )
        inherited_subagent_metadata = sanitize_subagent_runtime_metadata(parent_metadata)
        # Child tools may execute in an isolated worktree, but their transcript
        # is projected through the parent conversation. Keep the parent
        # conversation's workspace as the artifact owner so a later raw/read
        # request can satisfy the same composite owner scope.
        artifact_owner_workspace = str(
            parent_metadata.get("artifact_owner_workspace_root")
            or (context.workspace_root if context is not None else "")
            or ""
        ).strip()
        subagent_metadata_payload = {
            **inherited_subagent_metadata,
            "parent_run_id": parent_run_id,
            "agent_role": f"subagent:{agent_type}",
            "agent_mode": "subagent",
            "query_source": "background" if background else "subagent",
            "run_id": subagent_id,
            "artifact_owner_workspace_root": artifact_owner_workspace,
            **subagent_fence,
            "cancel_event": subagent_cancel_event,
            "retain_completed_checkpoint": True,
            "_journal_user_message": prompt,
            "_journal_user_metadata": journal_user_metadata,
            "_agent_execution_profile": execution_profile,
            "task_name": child_task_name,
            "fork_turns": fork_turns,
            **_narrowed_subagent_scope_metadata(
                inherited_subagent_metadata,
                _nonempty_subagent_metadata(subagent_config),
            ),
        }
        child_session_policy = restored_session_policy
        if child_session_policy is None and parent_session_policy is not None:
            child_session_policy = restore_toolset_policy(
                parent_session_policy,
                label="parent session tool capability policy",
            )
        if child_session_policy is not None:
            subagent_metadata_payload[SESSION_TOOLSET_POLICY_METADATA_KEY] = (
                child_session_policy
            )
        if rollout_reservation_id:
            subagent_metadata_payload["_rollout_reservation_id"] = (
                rollout_reservation_id
            )
        if team_mode:
            from backend.agent.plans import (
                bind_plan_owner,
                get_plan_file_path,
                merge_plan_constraints,
                plan_slug_from_snapshot,
            )

            teammate_plan_path: Path | None = None
            parent_snapshot = {}
            repository = parent_run_context.conversation_repository
            if repository is not None and context is not None:
                parent_record = repository.get_conversation(context.conversation_id)
                parent_snapshot = dict(getattr(parent_record, "context_snapshot", {}) or {})
            slug = str(subagent_config.get("plan_slug") or "") or plan_slug_from_snapshot(parent_snapshot)
            if not slug and repository is not None and context is not None:
                slug, _main_plan_path = bind_plan_owner(
                    repository,
                    context.conversation_id,
                    context.workspace_root,
                )
            if slug:
                subagent_config["plan_slug"] = slug
                subagent_metadata_payload["plan_slug"] = slug
                runtime_metadata = getattr(runtime, "_subagent_task_metadata", None)
                if isinstance(runtime_metadata, dict):
                    runtime_metadata.setdefault(subagent_id, {})["plan_slug"] = slug
                teammate_plan_path = get_plan_file_path(
                    slug,
                    effective_child_workspace or (context.workspace_root if context else None),
                    agent_id=subagent_id,
                )
                sub_context = replace(
                    sub_context,
                    filesystem_constraints=merge_plan_constraints(
                        sub_context.filesystem_constraints,
                        teammate_plan_path,
                    ),
                )

            async def _set_teammate_permission_mode(
                mode: str,
                *,
                source: str = "teammate.plan",
            ) -> None:
                nonlocal sub_context
                _ = source
                # Reject an unsupported mode instead of silently weakening
                # it to "default"; the parent ceiling is applied downstream.
                target_mode = normalize_permission_mode_token(mode)
                rebuilt = transition_subagent_permission_mode(
                    agent_type,
                    context,
                    sub_context,
                    target_mode,
                    read_only=bool(subagent_config.get("read_only")),
                    extra_deny_rules=custom_deny_rules,
                    plan_mode_required=bool(subagent_config.get("plan_mode_required")),
                    agent_triggers_enabled=feature_enabled("agent_triggers"),
                    execution_profile=execution_profile,
                )
                if teammate_plan_path is not None:
                    rebuilt = replace(
                        rebuilt,
                        filesystem_constraints=merge_plan_constraints(
                            rebuilt.filesystem_constraints,
                            teammate_plan_path,
                        ),
                    )
                sub_context = rebuilt
                runtime.update_subagent_lifecycle(
                    subagent_id,
                    permission_mode=target_mode,
                    agent_path=str(subagent_fence.get("agent_path") or ""),
                    mailbox_epoch=int(subagent_fence.get("mailbox_epoch") or 0),
                )

            def _teammate_permission_context_provider() -> PermissionContext:
                return sub_context

            async def _request_teammate_plan_approval(
                *,
                plan: str,
                plan_file_path: str,
            ) -> dict[str, Any]:
                request_id = f"plan_approval:{subagent_id}:{uuid4().hex[:12]}"
                content = json.dumps(
                    {
                        "type": "plan_approval_request",
                        "from": str(
                            subagent_config.get("teammate_name") or subagent_id
                        ),
                        "timestamp": datetime.now(UTC).isoformat(),
                        "plan_file_path": plan_file_path,
                        "plan_content": plan,
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                )
                recipient_epoch = None
                try:
                    runtime.send_swarm_message(
                        sender_id=subagent_id,
                        recipient_id="parent",
                        content=content,
                        conversation_id=str(getattr(context, "conversation_id", "") or ""),
                        team_name=str(subagent_config.get("team_name") or ""),
                        sender_mailbox_epoch=int(subagent_fence.get("mailbox_epoch") or 0),
                        recipient_mailbox_epoch=recipient_epoch,
                    )
                except ValueError as exc:
                    return {"queued": False, "feedback": str(exc)}

                runtime.update_subagent_lifecycle(
                    subagent_id,
                    awaiting_plan_approval=True,
                    active_plan_request_id=request_id,
                    is_idle=False,
                    current_activity="awaiting_plan_approval",
                    agent_path=str(subagent_fence.get("agent_path") or ""),
                    mailbox_epoch=int(subagent_fence.get("mailbox_epoch") or 0),
                )
                subagent_metadata_payload["active_plan_request_id"] = request_id
                subagent_metadata_payload["awaiting_plan_approval"] = True
                return {
                    "queued": True,
                    "awaiting_leader_approval": True,
                    "request_id": request_id,
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
        if effective_child_workspace is not None:
            # The child's toolchain follows AgentLoopSessionContext.workspace_root
            # (loop.py builds tool_ctx.workspace_root and metadata["cwd"] from it),
            # so pointing both at the worktree moves every filesystem/shell tool
            # and the path-escape boundary into the isolated copy.
            subagent_metadata_payload["cwd"] = str(effective_child_workspace)
            subagent_metadata_payload.pop("workspace_context", None)
            sub_state.workspace_context = None
        if isinstance(parent_prompt_cache_safe_params, dict):
            subagent_metadata_payload["parent_prompt_cache_safe_params"] = dict(
                parent_prompt_cache_safe_params
            )

        def _current_prompt_cache_fork_diagnostic() -> dict[str, Any]:
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
        cumulative_iterations = 0
        cumulative_tool_calls = 0

        def _terminal_result_payload(
            *,
            status: str,
            content: str,
            error: str = "",
            reason: str = "",
        ) -> dict[str, Any]:
            """Match the durable SubagentResultRecord shape for every transport."""
            return project_public_subagent_result({
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
                ),
                "usage": terminal_usage,
            })

        sub_context_builder = self._build_subagent_context_builder(
            context=context,
            token_budget=sub_budget,
            agent_settings=sub_settings,
            llm=llm,
            workspace_root=effective_child_workspace,
        )
        if resume_snapshot:
            sub_context_builder.load_snapshot(resume_snapshot)
        elif fork_snapshot is not None:
            sub_context_builder.load_snapshot(fork_snapshot)

        try:
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
                except asyncio.CancelledError:
                    if not answer_task.done():
                        answer_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.gather(answer_task, return_exceptions=True)
                    raise
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
                    ready = approval_ready.get(local_tool_call_id)
                    if ready is None:
                        ready = asyncio.Event()
                        approval_ready[local_tool_call_id] = ready
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

            # A named teammate keeps one child context across its own turns.
            # This is a child run/context owner inside the same MiniCode control
            # plane, not a provider-specific harness/session.
            child_agent_session = AgentSession(
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=self._artifact_store,
                permission_checker=permission_checker,
                agent_settings=sub_settings,
                token_budget=sub_budget,
                context_builder=sub_context_builder,
                approval_handler=subagent_approval_handler,
            )

            async def _run_query_turn(turn_prompt: str, turn_state: AgentState) -> None:
                nonlocal last_tool_name, terminal_status, terminal_reason
                nonlocal terminal_usage, terminal_provider_raw, last_error

                terminal_status = "completed"
                terminal_reason = ""
                terminal_usage = {}
                terminal_provider_raw = {}
                last_error = ""
                summary_parts.clear()
                if journal is not None:

                    async def _commit_child_turn_admission(
                        *,
                        boundary_input: Any,
                        history_start: int,
                        history_end: int,
                    ) -> None:
                        del boundary_input, history_end
                        snapshot = sub_context_builder.export_snapshot()
                        event_id = "user_prompt_" + hashlib.sha256(
                            (
                                f"{subagent_id}\0{max(0, int(history_start))}\0"
                                f"{turn_prompt}"
                            ).encode("utf-8")
                        ).hexdigest()
                        journal.append_once(
                            "user_prompt",
                            {
                                **journal_user_metadata,
                                "content": turn_prompt,
                                "provider_content": turn_prompt,
                                "context_snapshot": snapshot,
                            },
                            event_id=event_id,
                        )
                        journal_events[:] = [
                            event.to_dict() for event in journal.read_events()
                        ]

                    subagent_metadata_payload["commit_turn_admission"] = (
                        _commit_child_turn_admission
                    )

                async def _emit_live_transcript_snapshot(source_event_type: str) -> None:
                    transcript_snapshot = _current_transcript_snapshot()
                    if transcript_snapshot is None:
                        return
                    if int(transcript_snapshot.get("seq") or 0) <= emitted_transcript_seq:
                        return
                    progress_event = AgentEvent.subagent_progress(
                        subagent_id=subagent_id,
                        iteration=turn_state.iterations,
                        max_iterations=sub_settings.max_iterations,
                        tool_name="",
                        detail="",
                        current_activity=description,
                        waiting_on="model",
                        last_progress_at=int(time.time() * 1000),
                        activity_kind="narration",
                        activity_summary=description,
                        user_visible=True,
                        **subagent_fence,
                    )
                    progress_event.data["source_event_type"] = source_event_type
                    await _emit_incarnation_event(
                        "subagent.progress",
                        progress_event.data,
                        transcript_snapshot=transcript_snapshot,
                    )
                # Keep one owner advancing the canonical child query stream;
                # per-event tasks can otherwise reorder durable projections.
                child_run_context = RunContext(
                    lifecycle_runtime=parent_run_context.lifecycle_runtime,
                    execution_journal=journal,
                    mcp_manager=parent_run_context.mcp_manager,
                    mcp_owner_session_id=parent_run_context.mcp_owner_session_id,
                    subagent_parent_runtime=parent_run_context.subagent_parent_runtime,
                    turn_model_snapshot=parent_run_context.turn_model_snapshot,
                    agent_runtime=runtime,
                    hook_manager=parent_run_context.hook_manager,
                    workspace_context=parent_run_context.workspace_context,
                    cost_session_id=parent_run_context.cost_session_id,
                    requires_explicit_workspace=parent_run_context.requires_explicit_workspace,
                    conversation_repository=parent_run_context.conversation_repository,
                )
                if team_mode:
                    child_run_context.permission_mode_setter = _set_teammate_permission_mode
                    child_run_context.permission_context_provider = (
                        _teammate_permission_context_provider
                    )
                    child_run_context.teammate_plan_approval_requester = (
                        _request_teammate_plan_approval
                    )
                query_stream = QueryEngine().submit(QuerySubmission(
                        user_message=turn_prompt,
                        session=child_agent_session,
                        state=turn_state,
                            runtime=AgentLoopSessionContext(
                            permission_context=sub_context,
                            workspace_root=effective_child_workspace,
                            session_id=subagent_id,
                            task_id=subagent_id,
                            task_manager=context.task_manager if context else None,
                            emit_event=subagent_event_bridge,
                                metadata=(
                                {
                                    **subagent_metadata_payload,
                                    "parent_run_id": subagent_id,
                                }
                                if team_mode
                                else subagent_metadata_payload
                                ),
                                run_context=child_run_context,
                            ),
                    ))
                # The producer must not advance the canonical query stream
                # until this projection consumer has delivered the current
                # event. Otherwise later durable facts can overtake the first
                # live snapshot even with a one-slot queue.
                event_queue: asyncio.Queue[
                    tuple[AgentEvent, asyncio.Event] | BaseException | None
                ] = asyncio.Queue(maxsize=1)

                async def _pump_query_events() -> None:
                    try:
                        async for event in query_stream:
                            consumed = asyncio.Event()
                            await event_queue.put((event, consumed))
                            await consumed.wait()
                        await event_queue.put(None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await event_queue.put(exc)

                pump_task = asyncio.create_task(_pump_query_events())
                pending_consumed: asyncio.Event | None = None
                try:
                    while True:
                        if pending_consumed is not None:
                            pending_consumed.set()
                            pending_consumed = None
                        item = await event_queue.get()
                        if item is None:
                            break
                        if isinstance(item, BaseException):
                            raise item
                        event, pending_consumed = item
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
                                    turn_state.prompt_context[
                                        "last_completed_assistant_text"
                                    ] = content
                                    turn_state.reply = content
                                await _emit_live_transcript_snapshot("item.completed")
                        elif event.type == "agent_message.delta":
                            await _emit_live_transcript_snapshot("agent_message.delta")
                        elif event.type == "approval_request":
                            if (
                                can_forward_approval
                                and emit_event is not None
                                and _accepts_current_incarnation()
                            ):
                                local_tool_call_id = str(
                                    event.data.get("tool_call_id")
                                    or event.data.get("id")
                                    or ""
                                ).strip()
                                parent_tool_call_id = f"{subagent_id}:{local_tool_call_id}"
                                if local_tool_call_id not in approval_ready:
                                    approval_ready[local_tool_call_id] = asyncio.Event()
                                bridged_data = {
                                    **event.data,
                                    "tool_call_id": parent_tool_call_id,
                                    "subagent_id": subagent_id,
                                    "source_agent": subagent_id,
                                }
                                await emit_event("approval_request", bridged_data)
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
                            tool_snapshot = None
                            if journal is not None:
                                current_events = journal.read_events()
                                claimed_events = [
                                    persisted.to_dict()
                                    for persisted in current_events
                                    if (
                                        persisted.event_type != "tool_result"
                                        or str(
                                            persisted.payload.get("tool_call_id")
                                            or persisted.payload.get("call_id")
                                            or ""
                                        ).strip() != call_id
                                    )
                                ]
                                claimed_seq = max(
                                    (int(item.get("seq") or 0) for item in claimed_events),
                                    default=0,
                                )
                                from backend.services.subagent_service import build_subagent_transcript_messages

                                tool_snapshot = {
                                    "seq": claimed_seq,
                                    "messages": build_subagent_transcript_messages(
                                        {"events": claimed_events}
                                    ),
                                }
                            if emit_event is not None:
                                progress_event = AgentEvent.subagent_progress(
                                    subagent_id=subagent_id,
                                    iteration=turn_state.iterations,
                                    max_iterations=sub_settings.max_iterations,
                                    tool_name=tool_name,
                                    detail="",
                                    current_activity=description,
                                    waiting_on="tool",
                                    last_progress_at=int(time.time() * 1000),
                                    activity_kind="tool",
                                    activity_summary=description,
                                    user_visible=bool(description),
                                    **subagent_fence,
                                )
                                progress_event.data["source_event_type"] = "tool_call"
                                if call_id:
                                    progress_event.data["tool_call_id"] = call_id
                                await _emit_incarnation_event(
                                    "subagent.progress",
                                    progress_event.data,
                                    transcript_snapshot=tool_snapshot,
                                )
                        elif (
                            event.type == "agent.item"
                            and event.data.get("kind") == "process_text"
                        ):
                            process_text = _user_visible_progress_text(
                                event.data.get("content")
                                or event.data.get("summary")
                                or event.data.get("title")
                            )
                            if process_text:
                                if journal is not None:
                                    _record_journal_events(journal.append(
                                        "system",
                                        {
                                            "kind": "process_text",
                                            "content": process_text,
                                            "source": str(
                                                event.data.get("source")
                                                or "model_preamble"
                                            ),
                                            "transcript_only": True,
                                        },
                                    ))
                                if emit_event is None:
                                    continue
                                progress_event = AgentEvent.subagent_progress(
                                    subagent_id=subagent_id,
                                    iteration=turn_state.iterations,
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
                                item_id = str(
                                    event.data.get("item_id") or event.data.get("id") or ""
                                )
                                if item_id:
                                    progress_event.data["item_id"] = item_id
                                await _emit_incarnation_event(
                                    "subagent.progress", progress_event.data
                                )
                        elif event.type == "error":
                            last_error = str(event.data.get("message", ""))
                        elif event.type == "tool_result":
                            call_id = str(
                                event.data.get("tool_call_id")
                                or event.data.get("id")
                                or ""
                            ).strip()
                            last_tool_name = str(
                                event.data.get("tool_name")
                                or event.data.get("name")
                                or ""
                            ).strip()
                            if journal is not None:
                                journal_events[:] = [
                                    persisted.to_dict()
                                    for persisted in journal.read_events()
                                ]
                                await _emit_incarnation_event(
                                    "subagent.progress",
                                    AgentEvent.subagent_progress(
                                        subagent_id=subagent_id,
                                        iteration=turn_state.iterations,
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
                            try:
                                from backend.agent.checkpoint import save_run_checkpoint

                                checkpoint_snapshot = (
                                    sub_context_builder.export_snapshot()
                                )
                                checkpoint_receipt: dict[str, Any] = {}
                                save_run_checkpoint(
                                    receipt=checkpoint_receipt,
                                    session_id=subagent_id,
                                    user_message=turn_prompt,
                                    iterations=turn_state.iterations,
                                    reply=turn_state.reply,
                                    messages=checkpoint_snapshot.get("history", []),
                                    context_snapshot=checkpoint_snapshot,
                                    tool_calls=turn_state.tool_calls,
                                    active_skills=turn_state.active_skills,
                                    disabled_tools=turn_state.disabled_tools,
                                    stopped_reason="in_progress",
                                    last_mutation_index=turn_state._last_mutation_index,
                                    run_id=subagent_id,
                                    conversation_id=str(turn_state.conversation_id or ""),
                                    resume_payload={
                                        "run_id": subagent_id,
                                        "role": f"subagent:{agent_type}",
                                        **(
                                            {
                                                "worktree_path": str(
                                                    agent_worktree.worktree_path
                                                )
                                            }
                                            if agent_worktree is not None
                                            else {}
                                        ),
                                    },
                                )
                                if journal is not None:
                                    _record_journal_events(
                                        journal.append_lifecycle(
                                            "checkpoint_saved",
                                            {
                                                "sequence": int(
                                                    checkpoint_receipt.get("sequence")
                                                    or 0
                                                ),
                                                "context_revision": str(
                                                    checkpoint_receipt.get(
                                                        "context_revision"
                                                    )
                                                    or ""
                                                ),
                                            },
                                        )
                                    )
                            except Exception as checkpoint_exc:
                                turn_state.mark_transition(
                                    "checkpoint_save_failed",
                                    error_type=type(checkpoint_exc).__name__,
                                )
                                if journal is not None:
                                    _record_journal_events(
                                        journal.append_lifecycle(
                                            "checkpoint_save_failed",
                                            {
                                                "error_type": type(
                                                    checkpoint_exc
                                                ).__name__,
                                                "error": str(checkpoint_exc),
                                            },
                                        )
                                    )
                                raise RuntimeError(
                                    "Subagent mutation completed but its recovery checkpoint could not be persisted"
                                ) from checkpoint_exc
                        elif event.type == "done":
                            terminal_status = str(
                                event.data.get("status") or "completed"
                            )
                            terminal_reason = str(event.data.get("reason") or "")
                            if isinstance(event.data.get("usage"), dict):
                                terminal_usage = dict(event.data["usage"])
                            provider_raw = provider_raw_from_event_data(event.data)
                            if provider_raw:
                                terminal_provider_raw = dict(provider_raw)
                finally:
                    if pending_consumed is not None:
                        pending_consumed.set()
                    if not pump_task.done():
                        pump_task.cancel()
                        # QueryEngine commits its cancelled terminal before it
                        # re-raises CancelledError.  During that commit it may
                        # yield the final run/terminal events through this
                        # acknowledged queue.  Waiting for the pump without
                        # consuming those events deadlocks cancellation: the
                        # pump waits for ``consumed`` while this task waits for
                        # the pump.  Keep draining and acknowledging until the
                        # canonical query stream has finished its terminal
                        # commit.  The owning background subagent task remains
                        # registered for the whole drain, so outer cancellation
                        # boundaries can apply their normal bounded wait and
                        # cleanup_pending contract.
                        while not pump_task.done():
                            queued_item = asyncio.create_task(event_queue.get())
                            completed, _pending = await asyncio.wait(
                                {pump_task, queued_item},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if queued_item in completed:
                                drained = queued_item.result()
                                if isinstance(drained, tuple):
                                    _event, consumed = drained
                                    consumed.set()
                            else:
                                queued_item.cancel()
                                with suppress(asyncio.CancelledError, Exception):
                                    await queued_item
                        with suppress(asyncio.CancelledError, Exception):
                            await pump_task
            await _run_query_turn(effective_user_prompt, sub_state)

            if not team_mode:
                while True:
                    exit_decision = await lifecycle_owner.after_subagent_stop(
                        sub_state
                    )
                    if exit_decision.action == "terminal":
                        break
                    if exit_decision.action != "continue":
                        raise RuntimeError(
                            "Ordinary subagent reached an invalid post-stop state"
                        )
                    sub_state = AgentState(
                        user_message=exit_decision.prompt,
                        max_iterations=sub_settings.max_iterations,
                    )
                    lifecycle_owner.bind_turn_state(sub_state)
                    sub_state.workspace_context = parent_run_context.workspace_context
                    if context is not None:
                        sub_state.conversation_id = context.conversation_id
                        sub_state.checkpoint_manager = context.checkpoint_manager
                    await _run_query_turn(exit_decision.prompt, sub_state)

            if team_mode:
                teammate_name = lifecycle_owner.teammate_name
                team_name = lifecycle_owner.team_name
                conversation_id = lifecycle_owner.conversation_id
                current_prompt = effective_user_prompt
                shutdown_requested = False
                while True:
                    streamed_summary = "".join(summary_parts).strip()
                    turn_summary = streamed_summary or sub_state.reply.strip()
                    if terminal_status == "cancelled":
                        raise asyncio.CancelledError
                    if terminal_status == "failed":
                        raise RuntimeError(
                            last_error or terminal_reason or "Teammate turn failed"
                        )
                    if not turn_summary:
                        raise RuntimeError("Teammate turn ended without a final response.")

                    cumulative_iterations += sub_state.iterations
                    cumulative_tool_calls += len(sub_state.tool_calls)

                    exit_decision = await lifecycle_owner.after_subagent_stop(
                        sub_state
                    )
                    if exit_decision.action == "terminal":
                        terminal_reason = exit_decision.prompt or exit_decision.gate
                        break
                    if exit_decision.action == "continue":
                        current_prompt = exit_decision.prompt
                    elif exit_decision.action == "idle":
                        if bool(subagent_metadata_payload.get("awaiting_plan_approval")):
                            runtime.update_subagent_lifecycle(
                                subagent_id,
                                is_idle=False,
                                current_activity="awaiting_plan_approval",
                                **subagent_fence,
                            )
                        else:
                            runtime.update_subagent_lifecycle(
                                subagent_id,
                                is_idle=True,
                                current_activity="idle",
                                **subagent_fence,
                            )
                        idle_notification = {
                            "type": "idle_notification",
                            "from": teammate_name,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "idleReason": "available",
                            "summary": _subagent_display_summary(turn_summary),
                        }
                        runtime.send_swarm_message(
                            sender_id=subagent_id,
                            recipient_id="parent",
                            content=json.dumps(idle_notification, ensure_ascii=False),
                            conversation_id=conversation_id,
                            team_name=team_name,
                            sender_mailbox_epoch=int(
                                subagent_fence.get("mailbox_epoch") or 0
                            ),
                        )
                        if emit_event is not None:
                            await _emit_incarnation_event(
                                "subagent.progress",
                                AgentEvent.subagent_progress(
                                    subagent_id=subagent_id,
                                    iteration=cumulative_iterations,
                                    max_iterations=sub_settings.max_iterations,
                                    tool_name="",
                                    detail="",
                                    current_activity=(
                                        "Awaiting team leader plan approval"
                                        if bool(
                                            subagent_metadata_payload.get(
                                                "awaiting_plan_approval"
                                            )
                                        )
                                        else "Idle"
                                    ),
                                    waiting_on=(
                                        "approval"
                                        if bool(
                                            subagent_metadata_payload.get(
                                                "awaiting_plan_approval"
                                            )
                                        )
                                        else "message"
                                    ),
                                    last_progress_at=int(time.time() * 1000),
                                    activity_kind="status",
                                    activity_summary="Idle",
                                    user_visible=True,
                                    **subagent_fence,
                                ).data,
                            )

                        selected_claim = None
                        selected_message = None
                        while not subagent_cancel_event.is_set():
                            claims = runtime.claim_swarm_messages(
                                participant_id=subagent_id,
                                mailbox_epoch=int(
                                    subagent_fence.get("mailbox_epoch") or 0
                                ),
                                conversation_id=conversation_id,
                                since_seq=0,
                                limit=100,
                            )
                            if not claims:
                                try:
                                    await asyncio.wait_for(
                                        subagent_cancel_event.wait(), timeout=0.5
                                    )
                                except asyncio.TimeoutError:
                                    continue
                                break

                            parsed: list[tuple[Any, dict[str, Any] | None]] = []
                            for claim in claims:
                                try:
                                    value = json.loads(str(claim.message.content or ""))
                                except (TypeError, ValueError):
                                    value = None
                                parsed.append((claim, value if isinstance(value, dict) else None))

                            active_request_id = str(
                                subagent_metadata_payload.get(
                                    "active_plan_request_id"
                                )
                                or ""
                            ).strip()
                            plan_claim = next(
                                (
                                    (claim, payload)
                                    for claim, payload in parsed
                                    if payload is not None
                                    and payload.get("type") == "plan_approval_response"
                                    and str(payload.get("request_id") or "")
                                    == active_request_id
                                    and str(claim.message.sender_id or "")
                                    == parent_run_id
                                    and str(claim.message.team_name or "") == team_name
                                    and int(
                                        claim.message.recipient_mailbox_epoch or 0
                                    )
                                    == int(subagent_fence.get("mailbox_epoch") or 0)
                                    and isinstance(payload.get("approved"), bool)
                                ),
                                None,
                            )
                            if plan_claim is not None:
                                claim, payload = plan_claim
                                approved = bool(payload.get("approved"))
                                if approved:
                                    target_mode = str(
                                        payload.get("permission_mode") or "confirm"
                                    ).strip()
                                    if target_mode not in {
                                        "confirm",
                                        "auto",
                                        "bypass",
                                        "plan",
                                    }:
                                        target_mode = "confirm"
                                    await _set_teammate_permission_mode(
                                        target_mode,
                                        source="teammate.plan_approved",
                                    )
                                runtime.update_subagent_lifecycle(
                                    subagent_id,
                                    awaiting_plan_approval=False,
                                    active_plan_request_id="",
                                    current_activity=(
                                        "approved" if approved else "plan_rejected"
                                    ),
                                    **subagent_fence,
                                )
                                subagent_metadata_payload[
                                    "awaiting_plan_approval"
                                ] = False
                                subagent_metadata_payload[
                                    "active_plan_request_id"
                                ] = ""
                                runtime.ack_swarm_message_claims([claim])
                                remaining = [
                                    item
                                    for item in claims
                                    if item.claim_token != claim.claim_token
                                ]
                                if remaining:
                                    runtime.release_swarm_message_claims(remaining)
                                if approved:
                                    current_prompt = _format_teammate_message(
                                        "team-lead",
                                        "Your plan was approved. Continue with implementation.",
                                    )
                                    selected_claim = claim
                                    selected_message = claim.message
                                    break
                                feedback = str(
                                    payload.get("feedback")
                                    or "Plan needs revision"
                                ).strip()
                                current_prompt = _format_teammate_message(
                                    "team-lead", feedback
                                )
                                selected_claim = claim
                                selected_message = claim.message
                                break

                            shutdown_claim = next(
                                (
                                    (claim, payload)
                                    for claim, payload in parsed
                                    if payload is not None
                                    and payload.get("type") == "shutdown_request"
                                    and str(claim.message.sender_id or "")
                                    == parent_run_id
                                    and str(claim.message.team_name or "") == team_name
                                    and int(
                                        claim.message.recipient_mailbox_epoch or 0
                                    )
                                    == int(subagent_fence.get("mailbox_epoch") or 0)
                                    and str(payload.get("request_id") or "").strip()
                                    and str(payload.get("from") or "").strip()
                                    == parent_run_id
                                ),
                                None,
                            )
                            if shutdown_claim is not None:
                                claim, payload = shutdown_claim
                                request_id = str(payload.get("request_id") or "").strip()
                                reservation = runtime.reserve_lifecycle_response(
                                    response_kind="shutdown_response",
                                    participant_id=subagent_id,
                                    mailbox_epoch=int(subagent_fence.get("mailbox_epoch") or 0),
                                    request_id=request_id,
                                    target_id=parent_run_id,
                                )
                                if reservation:
                                    response = json.dumps(
                                        {
                                            "type": "shutdown_response",
                                            "request_id": request_id,
                                            "from": teammate_name,
                                            "approve": True,
                                        },
                                        ensure_ascii=False,
                                    )
                                    runtime.send_swarm_message(
                                        sender_id=subagent_id,
                                        recipient_id="parent",
                                        content=response,
                                        conversation_id=conversation_id,
                                        team_name=team_name,
                                        sender_mailbox_epoch=int(
                                            subagent_fence.get("mailbox_epoch") or 0
                                        ),
                                    )
                                    runtime.commit_lifecycle_response(
                                        response_kind="shutdown_response",
                                        participant_id=subagent_id,
                                        mailbox_epoch=int(subagent_fence.get("mailbox_epoch") or 0),
                                        request_id=request_id,
                                        reservation_token=reservation,
                                    )
                                runtime.ack_swarm_message_claims([claim])
                                remaining = [
                                    item for item in claims if item.claim_token != claim.claim_token
                                ]
                                if remaining:
                                    runtime.release_swarm_message_claims(remaining)
                                shutdown_requested = True
                                break
                            leader_claim = next(
                                (
                                    (claim, payload)
                                    for claim, payload in parsed
                                    if str(claim.message.sender_id or "")
                                    == parent_run_id
                                ),
                                None,
                            )
                            selected_claim, _ = (
                                shutdown_claim
                                or leader_claim
                                or parsed[0]
                            )
                            selected_message = selected_claim.message
                            current_prompt = _format_teammate_message(
                                (
                                    "team-lead"
                                    if str(selected_message.sender_id or "")
                                    == parent_run_id
                                    else str(selected_message.sender_id or "unknown")
                                ),
                                str(selected_message.content or ""),
                            )
                            runtime.ack_swarm_message_claims([selected_claim])
                            remaining = [
                                item
                                for item in claims
                                if item.claim_token != selected_claim.claim_token
                            ]
                            if remaining:
                                runtime.release_swarm_message_claims(remaining)
                            break

                        if subagent_cancel_event.is_set():
                            raise asyncio.CancelledError
                        if shutdown_requested:
                            break
                        if selected_message is None and not current_prompt:
                            continue

                    if shutdown_requested:
                        break

                    runtime.update_subagent_lifecycle(
                        subagent_id,
                        is_idle=False,
                        current_activity=description,
                        **subagent_fence,
                    )
                    sub_state = AgentState(
                        user_message=current_prompt,
                        max_iterations=sub_settings.max_iterations,
                    )
                    lifecycle_owner.bind_turn_state(sub_state)
                    sub_state.workspace_context = parent_run_context.workspace_context
                    if context is not None:
                        sub_state.conversation_id = context.conversation_id
                        sub_state.checkpoint_manager = context.checkpoint_manager
                    await _run_query_turn(current_prompt, sub_state)

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
                    ordinary_input_tokens=(
                        int(terminal_usage.get("ordinary_input_tokens") or 0)
                        if "ordinary_input_tokens" in terminal_usage
                        else None
                    ),
                    prompt_cache_total_tokens=(
                        int(terminal_usage.get("prompt_cache_total_tokens") or 0)
                        if "prompt_cache_total_tokens" in terminal_usage
                        else None
                    ),
                    reasoning_output_tokens=int(terminal_usage.get("reasoning_output_tokens") or 0),
                    model_id=getattr(llm, "_model", None),
                    provider=provider,
                    session_id=str(
                        parent_run_context.cost_session_id
                        or (context.session_id if context else "")
                    ),
                    input_includes_cache_read=bool(
                        terminal_usage.get("input_includes_cache_read", True)
                    ),
                    input_includes_cache_write=bool(
                        terminal_usage.get("input_includes_cache_write", True)
                    ),
                    cost_usd=float(terminal_usage.get("cost_usd") or 0.0),
                )
            display_summary = _subagent_display_summary(summary)
            tool_call_count = (
                cumulative_tool_calls + len(sub_state.tool_calls)
                if team_mode
                else len(sub_state.tool_calls)
            )

            if terminal_status == "cancelled":
                raise asyncio.CancelledError
            if terminal_status == "failed":
                raise RuntimeError(last_error or terminal_reason or "Subagent run failed")

            result_status = "partial" if terminal_status == "partial" else "completed"
            result_text = summary
            if result_status == "partial" and last_error and last_error not in result_text:
                result_text = f"{result_text}\n\n{last_error}".strip()
            full_result_text = result_text
            worktree_note = await _cleanup_worktree()
            if worktree_note:
                result_text += f"\n\n{worktree_note}"
                full_result_text = result_text
            result_text, result_artifact_id = _externalize_large_subagent_result(
                self._artifact_store,
                subagent_id=subagent_id,
                content=result_text,
            )
            result_record = None
            completed_record = None
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
                    result_record.public_dict(content_override=result_text)
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
                    done_event.data["record"] = completed_record.public_dict()
                prompt_cache_fork = _current_prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await _emit_incarnation_event(
                    "subagent.done", done_event.data, require_running=False
                )
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
            task_runtime_metadata = (
                runtime.get_subagent_task_metadata(subagent_id)
                if runtime is not None
                else None
            )
            cancel_reason = str(
                (task_runtime_metadata or {}).get("cancel_reason") or "cancelled"
            ).strip()[:128]
            record = None
            retained = "".join(summary_parts).strip()
            with suppress(Exception):
                worktree_note = await _cleanup_worktree()
                if worktree_note:
                    logger.info("Subagent %s cleanup: %s", subagent_id, worktree_note)
            if runtime is not None:
                result_record = runtime.store_subagent_result(
                    subagent_id,
                    status="cancelled",
                    content=retained,
                    error="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=len(sub_state.tool_calls),
                    terminal_reason=cancel_reason,
                    usage=terminal_usage,
                    **subagent_fence,
                )
                if result_record is None:
                    raise RuntimeError(
                        f"Durable cancellation result could not be stored for subagent {subagent_id}."
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
                    termination_reason=cancel_reason,
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    usage=terminal_usage,
                    **subagent_fence,
                )
                if record is not None:
                    done_event.data["record"] = record.public_dict()
                done_event.data["result"] = _terminal_result_payload(
                    status="cancelled",
                    content=retained,
                    error="cancelled",
                    reason=cancel_reason,
                )
                prompt_cache_fork = _current_prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await _emit_incarnation_event(
                    "subagent.done", done_event.data, require_running=False
                )
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
            if runtime is not None:
                result_record = runtime.store_subagent_result(
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
                if result_record is None:
                    return ToolResult(
                        content=(
                            f"Subagent {subagent_id} failed, but its durable result could not be stored. "
                            "Completion was withheld pending runtime recovery."
                        ),
                        is_error=True,
                        status="failed",
                        error_kind="subagent_result_persistence",
                        recoverable=False,
                        result_kind="subagent",
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
                    done_event.data["record"] = record.public_dict()
                done_event.data["result"] = _terminal_result_payload(
                    status=failure_status,
                    content=partial_text,
                    error=f"{type(exc).__name__}: {exc}",
                    reason=terminal_reason or type(exc).__name__,
                )
                prompt_cache_fork = _current_prompt_cache_fork_diagnostic()
                if prompt_cache_fork:
                    done_event.data["prompt_cache_fork"] = prompt_cache_fork
                await _emit_incarnation_event(
                    "subagent.done", done_event.data, require_running=False
                )
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
        runtime = require_runtime_from_context(context)
        subagent_ids = [f"subagent-{uuid4().hex[:8]}" for _ in tasks]
        results: list[ToolResult | Exception | None] = [None] * total
        parent_cancel_event = context.cancel_event if context is not None else None

        async def run_one(index: int) -> None:
            subagent_id = subagent_ids[index]
            try:
                acquired = await runtime.acquire_subagent_slot(
                    subagent_id,
                    cancel_event=parent_cancel_event,
                )
                if not acquired:
                    results[index] = ToolResult(
                        content="Subagent was cancelled before it started.",
                        is_error=False,
                        status="cancelled",
                        display_summary="Subagent cancelled before start",
                        result_kind="subagent",
                    )
                    return
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
            except asyncio.CancelledError:
                if parent_cancel_event is not None and parent_cancel_event.is_set():
                    raise
                results[index] = ToolResult(
                    content="Subagent was stopped.",
                    is_error=False,
                    status="cancelled",
                    display_summary="Subagent stopped",
                    result_kind="subagent",
                )
            except Exception as exc:  # noqa: BLE001
                results[index] = exc
            finally:
                runtime.release_subagent_slot(subagent_id)
                current_task = asyncio.current_task()
                if current_task is not None:
                    runtime.release_subagent_task(subagent_id, expected_task=current_task)

        worker_tasks = [asyncio.create_task(run_one(index)) for index in range(total)]
        parent_metadata = metadata_from_context(context)
        registered_count = 0
        try:
            for index, worker_task in enumerate(worker_tasks):
                runtime.register_subagent_task(
                    subagent_ids[index],
                    worker_task,
                    parent_run_id=str(parent_metadata.get("run_id") or ""),
                    owner_task_id=str(getattr(context, "task_id", "") or ""),
                    session_id=str(getattr(context, "session_id", "") or ""),
                    agent_type=tasks[index].get("agent_type", "general-purpose"),
                    prompt_summary=tasks[index]["description"],
                    pending=True,
                )
                registered_count += 1
        except Exception as exc:  # noqa: BLE001
            for worker_task in worker_tasks:
                worker_task.cancel()
            for index in range(registered_count):
                runtime.release_subagent_task(
                    subagent_ids[index],
                    expected_task=worker_tasks[index],
                )
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            return ToolResult(
                content=f"Parallel subagents could not be registered: {exc}",
                is_error=True,
                status="failed",
                display_summary="Parallel subagent registration failed",
                result_kind="subagent",
            )
        await asyncio.gather(*worker_tasks)

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
        team_mode: bool = False,
        background: bool = False,
        plan_mode_required: bool = False,
        requested_mode: str = "",
        agent_triggers_enabled: bool = False,
        execution_profile: AgentExecutionProfile | None = None,
    ) -> PermissionContext:
        return build_subagent_permission_context(
            agent_type,
            parent_context,
            read_only=read_only,
            extra_deny_rules=extra_deny_rules,
            team_mode=team_mode,
            background=background,
            plan_mode_required=plan_mode_required,
            requested_mode=requested_mode,
            agent_triggers_enabled=agent_triggers_enabled,
            execution_profile=execution_profile,
        )

    @staticmethod
    def _is_recursive_subagent_call(context: ToolExecutionContext | None) -> bool:
        if context is None:
            return False
        permission = context.permission
        if resolve_agent_execution_profile(permission, context.metadata) is not None:
            return True
        # Legacy/durable fallback for runs created before execution profiles
        # were persisted.  New authorization decisions are made from the
        # resolved profile in ``execute`` rather than these identity strings.
        if str(permission.source or "").startswith(("subagent:", "teammate:")):
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
    def _metadata_from_context(context: ToolExecutionContext | None) -> dict[str, Any]:
        return metadata_from_context(context)

    @staticmethod
    def _build_subagent_context_builder(
        *,
        context: ToolExecutionContext | None,
        token_budget: TokenBudget,
        agent_settings: AgentSettings,
        llm: LLMAdapter | None = None,
        workspace_root: str | Path | None = None,
    ) -> ContextBuilder:
        # A worker cannot see the parent conversation. Its delegated
        # prompt is self-contained, while ContextBuilder still loads workspace
        # instructions normally. Copying history leaks sibling objectives.
        return ContextBuilder(
            token_budget=token_budget,
            agent_settings=agent_settings,
            llm=llm,
            workspace_root=workspace_root,
        )
