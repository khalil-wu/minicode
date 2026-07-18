"""Deterministic workflow orchestration over subagents and swarm tasks."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.agent.runtime import AgentRuntime, MAX_CONCURRENT_SUBAGENTS, default_runtime
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_tools import TaskTool
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.subagent_runtime import runtime_from_context

WORKFLOW_MODES = ("parallel", "pipeline", "phases")


def _runtime(context: ToolExecutionContext | None) -> AgentRuntime:
    return runtime_from_context(context) or default_runtime()


def _conversation_id(context: ToolExecutionContext | None) -> str:
    return str(getattr(context, "conversation_id", "") or "").strip()


def _actor_id(context: ToolExecutionContext | None) -> str:
    if context is not None:
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        for key in ("run_id", "agent_id", "parent_run_id"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        if context.task_id:
            return context.task_id
    return "main"


def _step_id(raw: Any, index: int) -> str:
    value = str(raw or "").strip()
    return value or f"step-{index}"


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


def _default_read_only(agent_type: str, role: str) -> bool:
    text = f"{agent_type} {role}".lower()
    return any(marker in text for marker in ("explore", "plan", "verify", "verification", "review", "research", "audit", "scout"))


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)].rstrip() + "\n[truncated upstream output]"


def _dependency_output_context(
    runtime: AgentRuntime,
    task_ids: list[str],
    *,
    max_chars: int = 12000,
) -> str:
    if not task_ids:
        return ""
    sections: list[str] = []
    remaining = max_chars
    for task_id in task_ids:
        task = runtime.get_swarm_task(task_id)
        if task is None:
            continue
        title = task.title or task.task_id
        node = task.node_id or task.task_id
        header = f"### {node}: {title}\nTask ID: {task.task_id}\nStatus: {task.status}"
        output_lines: list[str] = []
        for index, output in enumerate(task.outputs, 1):
            content = str(output.content or "").strip()
            if not content:
                continue
            author = output.author_id or "unknown"
            output_lines.append(f"Output {index} by {author}:\n{content}")
        body = "\n\n".join(output_lines) if output_lines else "No output was attached by this upstream node."
        section = f"{header}\n\n{body}"
        if remaining <= 0:
            break
        section = _truncate_text(section, remaining)
        sections.append(section)
        remaining -= len(section)
    if not sections:
        return ""
    return (
        "Upstream workflow context (completed dependencies):\n"
        "Use these upstream outputs as handoff context for this step.\n\n"
        + "\n\n".join(sections)
    )


async def _emit_workflow_event(
    context: ToolExecutionContext | None,
    workflow_id: str,
    event: dict[str, Any],
) -> None:
    emit = context.emit_event if context else None
    if emit is None:
        return
    await emit(
        "subagent.event",
        {
            "subagent_id": workflow_id,
            "event": event,
            "display_scope": "agents",
            "panel_hint": "subagents",
        },
    )


async def _emit_task_update_event(
    context: ToolExecutionContext | None,
    task: dict[str, Any],
) -> None:
    emit = context.emit_event if context else None
    if emit is None:
        return
    await emit(
        "subagent.event",
        {
            "subagent_id": str(task.get("assignee") or "swarm"),
            "event": {"type": "task_updated", "task": task},
            "display_scope": "agents",
            "panel_hint": "subagents",
        },
    )


class WorkflowTool(BaseTool):
    """Create a deterministic multi-agent workflow plan and launch ready steps."""

    name = "workflow"
    description = (
        "Orchestrate multiple subagents as a deterministic workflow. "
        "Creates shared swarm tasks for every step, records dependencies, and starts ready steps in the background."
    )
    permission = PermissionLevel.AUTO
    result_kind = "subagent"
    activity_kind = "genericTool"
    display_scope = "agents"
    panel_hint = "subagents"
    deferred_catalog_scopes = ("coordination",)

    def __init__(
        self,
        *,
        llm_provider: Any | None = None,
        tool_registry_provider: Any | None = None,
        artifact_store: Any | None = None,
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
        step_schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Stable local step id."},
                "title": {"type": "string", "description": "Short step title shown in the shared task board."},
                "prompt": {"type": "string", "description": "Complete prompt for the subagent step."},
                "agent_type": {"type": "string", "description": "Subagent type, such as explore, plan, implement, or verification."},
                "role": {"type": "string", "description": "Human-readable role shown for this workflow node."},
                "objective": {"type": "string", "description": "Concise objective shown in the Agent workbench."},
                "phase": {"type": "string", "description": "Optional phase name. Used by phases mode."},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Step ids that must complete before this step is ready.",
                },
                "required_for_final": {
                    "type": "boolean",
                    "description": "Whether the parent must wait for this step before finalizing.",
                },
                "read_only": {
                    "type": "boolean",
                    "description": "Whether this node should be treated as read-only investigation/review.",
                },
                "write_scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file/path scopes this node may modify.",
                },
            },
            "required": ["title", "prompt"],
        }
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workflow name."},
                    "workflow_id": {
                        "type": "string",
                        "description": "Existing workflow id to resume pending nodes after a restart.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(WORKFLOW_MODES),
                        "description": "parallel launches every step; pipeline links each step to the previous; phases links later phases to earlier phases.",
                    },
                    "steps": {
                        "type": "array",
                        "items": step_schema,
                        "description": "Workflow steps to register and run.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Per-subagent timeout passed to task; default 300, max 600.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Start ready steps asynchronously and return immediately. Defaults true.",
                    },
                },
                "anyOf": [
                    {"required": ["name", "steps"]},
                    {"required": ["workflow_id"]},
                ],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        workflow_id_arg = str(args.get("workflow_id") or "").strip()
        raw_steps = args.get("steps")
        if workflow_id_arg and not raw_steps:
            return await self._resume_existing_workflow(args, context=context)

        workflow_name = str(args.get("name") or "").strip()
        if not workflow_name:
            return self._error_result("Missing name argument")
        if not isinstance(raw_steps, list) or not raw_steps:
            return self._error_result("Missing steps argument")
        mode = str(args.get("mode") or "parallel").strip()
        if mode not in WORKFLOW_MODES:
            return self._error_result(f"Unsupported workflow mode: {mode}")

        steps = self._normalize_steps(raw_steps, mode)
        if not steps:
            return self._error_result("No valid workflow steps were provided")

        workflow_id = f"workflow-{uuid4().hex[:8]}"
        runtime = _runtime(context)
        step_to_task: dict[str, str] = {}
        for step in steps:
            task = runtime.create_swarm_task(
                title=f"{workflow_name}: {step['title']}",
                description=step["prompt"],
                assignee=str(step.get("assignee") or step["step_id"]),
                conversation_id=_conversation_id(context),
                status="blocked" if step["depends_on"] else "pending",
                priority="normal",
                team_name=workflow_id,
                created_by=_actor_id(context),
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                workflow_mode=mode,
                node_id=step["node_id"],
                agent_type=step["agent_type"],
                role=step["role"],
                objective=step["objective"],
                required_for_final=step["required_for_final"],
                read_only=step["read_only"],
                write_scope=step["write_scope"],
            )
            step["task_id"] = task.task_id
            step_to_task[step["step_id"]] = task.task_id

        for step in steps:
            blocked_by = [step_to_task[item] for item in step["depends_on"] if item in step_to_task]
            if blocked_by:
                runtime.update_swarm_task(step["task_id"], {"blocked_by": blocked_by, "status": "blocked"})

        timeout_seconds = float(args.get("timeout_seconds") or 300.0)
        self._register_launcher(
            runtime,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            workflow_mode=mode,
            context=context,
            timeout_seconds=timeout_seconds,
            steps_by_task_id={str(step["task_id"]): dict(step) for step in steps},
        )

        ready_steps = [step for step in steps if not step["depends_on"]]
        launch_steps = ready_steps[:MAX_CONCURRENT_SUBAGENTS]
        launched_steps: list[dict[str, Any]] = []
        launched_summary = "No ready workflow steps were launched."
        if bool(args.get("run_in_background", True)) and launch_steps:
            task_result = await self._launch_ready_steps(
                launch_steps,
                context=context,
                timeout_seconds=timeout_seconds,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                workflow_mode=mode,
            )
            launched_summary = task_result.content
            if not task_result.is_error and task_result.status == "running":
                launched_steps = launch_steps
                for step in launched_steps:
                    runtime.update_swarm_task(step["task_id"], {"status": "in_progress"})

        event = {
            "type": "workflow_started",
            "workflow_id": workflow_id,
            "name": workflow_name,
            "mode": mode,
            "steps": [
                {
                    "step_id": step["step_id"],
                    "node_id": step["node_id"],
                    "task_id": step["task_id"],
                    "title": step["title"],
                    "role": step["role"],
                    "agent_type": step["agent_type"],
                    "objective": step["objective"],
                    "phase": step["phase"],
                    "depends_on": [step_to_task[item] for item in step["depends_on"] if item in step_to_task],
                    "depends_on_nodes": list(step["depends_on"]),
                    "blocked_by": [step_to_task[item] for item in step["depends_on"] if item in step_to_task],
                    "required_for_final": step["required_for_final"],
                    "read_only": step["read_only"],
                    "write_scope": step["write_scope"],
                    "ready": step in ready_steps,
                }
                for step in steps
            ],
        }
        await _emit_workflow_event(context, workflow_id, event)

        lines = [
            f"Workflow {workflow_id} ({workflow_name}) registered in {mode} mode.",
            f"Steps: {len(steps)} total, {len(ready_steps)} ready, {len(launched_steps)} launched, {len(steps) - len(ready_steps)} blocked.",
            "Shared task ids:",
        ]
        for index, step in enumerate(steps, 1):
            deps = ", ".join(step_to_task[item] for item in step["depends_on"] if item in step_to_task) or "-"
            lines.append(f"{index}. {step['task_id']} [{step['title']}] blocked_by={deps}")
        lines.append("Launch result:")
        lines.append(launched_summary)
        return ToolResult(
            content="\n".join(lines),
            display_summary=f"Workflow: {workflow_name}",
            result_kind="subagent",
            display_scope="agents",
            status="running" if launched_steps else "blocked",
        )

    async def _resume_existing_workflow(
        self,
        args: dict[str, Any],
        *,
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        workflow_id = str(args.get("workflow_id") or "").strip()
        runtime = _runtime(context)
        tasks = runtime.list_swarm_tasks(
            team_name=workflow_id,
            conversation_id=_conversation_id(context),
            limit=100,
        )
        if not tasks:
            return self._error_result(f"Workflow not found or has no tasks: {workflow_id}")
        workflow_name = str(args.get("name") or "").strip() or next((task.workflow_name for task in tasks if task.workflow_name), workflow_id)
        workflow_mode = str(args.get("mode") or "").strip() or next((task.workflow_mode for task in tasks if task.workflow_mode), "workflow")
        timeout_seconds = float(args.get("timeout_seconds") or 300.0)
        self._register_launcher(
            runtime,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            workflow_mode=workflow_mode,
            context=context,
            timeout_seconds=timeout_seconds,
            steps_by_task_id={},
        )
        result = await runtime.resume_pending_workflow(
            workflow_id,
            conversation_id=_conversation_id(context),
        )
        resumed_tasks = result.get("resumed_tasks") if isinstance(result, dict) else []
        if isinstance(resumed_tasks, list):
            for task in resumed_tasks:
                if isinstance(task, dict):
                    await _emit_task_update_event(context, task)
        await _emit_workflow_event(
            context,
            workflow_id,
            {
                "type": "workflow_nodes_resumed",
                "workflow_id": workflow_id,
                "tasks": resumed_tasks if isinstance(resumed_tasks, list) else [],
                "launched": bool(result.get("launched")) if isinstance(result, dict) else False,
                "launch_summary": str(result.get("launch_summary") or "") if isinstance(result, dict) else "",
                "launch_error": str(result.get("launch_error") or "") if isinstance(result, dict) else "",
            },
        )

        resumed_count = len(resumed_tasks) if isinstance(resumed_tasks, list) else 0
        launched = bool(result.get("launched")) if isinstance(result, dict) else False
        launch_error = str(result.get("launch_error") or "") if isinstance(result, dict) else ""
        lines = [
            f"Workflow {workflow_id} ({workflow_name}) resume checked.",
            f"Resumed/launched: {resumed_count} pending node(s).",
        ]
        if launch_error:
            lines.append(f"Launch error: {launch_error}")
        elif isinstance(result, dict) and result.get("launch_summary"):
            lines.append(str(result["launch_summary"]))
        if launch_error:
            return ToolResult(
                content="\n".join(lines),
                is_error=True,
                display_summary=f"Workflow resume failed: {workflow_name}",
                result_kind="subagent",
                display_scope="agents",
                status="failed",
            )
        snapshot = runtime.workflow_completion_snapshot(
            workflow_id,
            conversation_id=_conversation_id(context),
        )
        return ToolResult(
            content="\n".join(lines),
            display_summary=f"Workflow resume: {workflow_name}",
            result_kind="subagent",
            display_scope="agents",
            status="running" if launched else "success" if snapshot.get("complete") else "blocked",
        )

    def _register_launcher(
        self,
        runtime: AgentRuntime,
        *,
        workflow_id: str,
        workflow_name: str,
        workflow_mode: str,
        context: ToolExecutionContext | None,
        timeout_seconds: float,
        steps_by_task_id: dict[str, dict[str, Any]],
    ) -> None:
        async def launch_workflow_tasks(tasks: list[Any]) -> ToolResult:
            launch_steps = [self._step_from_task(task, steps_by_task_id) for task in tasks]
            return await self._launch_ready_steps(
                launch_steps,
                context=context,
                timeout_seconds=timeout_seconds,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                workflow_mode=workflow_mode,
            )

        runtime.register_workflow_launcher(workflow_id, launch_workflow_tasks)

    def _step_from_task(
        self,
        task: Any,
        steps_by_task_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        step = dict(steps_by_task_id.get(str(getattr(task, "task_id", ""))) or {})
        if not step:
            step = {
                "step_id": str(getattr(task, "node_id", "") or getattr(task, "task_id", "")),
                "node_id": str(getattr(task, "node_id", "") or getattr(task, "task_id", "")),
                "title": str(getattr(task, "title", "") or getattr(task, "task_id", "")),
                "prompt": str(getattr(task, "description", "") or ""),
                "agent_type": str(getattr(task, "agent_type", "") or getattr(task, "role", "") or "general-purpose"),
                "objective": str(getattr(task, "objective", "") or getattr(task, "title", "")),
                "depends_on": list(getattr(task, "blocked_by", []) or []),
                "required_for_final": bool(getattr(task, "required_for_final", True)),
                "read_only": bool(getattr(task, "read_only", False)),
                "write_scope": list(getattr(task, "write_scope", []) or []),
            }
        step["task_id"] = str(getattr(task, "task_id", "") or step.get("task_id") or "")
        step["title"] = str(step.get("title") or getattr(task, "title", "") or step["task_id"])
        step["prompt"] = str(step.get("prompt") or getattr(task, "description", "") or "")
        step["agent_type"] = str(step.get("agent_type") or getattr(task, "agent_type", "") or getattr(task, "role", "") or "general-purpose")
        step["objective"] = str(step.get("objective") or getattr(task, "objective", "") or step["title"])
        step["blocked_by"] = list(getattr(task, "blocked_by", []) or step.get("blocked_by") or [])
        step["depends_on"] = list(step.get("depends_on") or step["blocked_by"])
        return step

    def _normalize_steps(self, raw_steps: list[Any], mode: str) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        previous_step_id = ""
        last_phase = ""
        previous_phase_step_ids: list[str] = []
        current_phase_step_ids: list[str] = []
        for index, item in enumerate(raw_steps[:20], 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not title or not prompt:
                continue
            step_id = _step_id(item.get("id"), index)
            phase = str(item.get("phase") or "").strip()
            depends_on = _string_list(item.get("depends_on"))
            if mode == "pipeline" and previous_step_id and not depends_on:
                depends_on = [previous_step_id]
            if mode == "phases":
                if phase != last_phase:
                    previous_phase_step_ids = current_phase_step_ids
                    current_phase_step_ids = []
                    last_phase = phase
                if previous_phase_step_ids and not depends_on:
                    depends_on = list(previous_phase_step_ids)
                current_phase_step_ids.append(step_id)
            agent_type = str(item.get("agent_type") or "general-purpose").strip() or "general-purpose"
            role = str(item.get("role") or agent_type).strip() or agent_type
            objective = str(item.get("objective") or title).strip() or title
            read_only = _bool_field(item.get("read_only"), _default_read_only(agent_type, role))
            step = {
                "step_id": step_id,
                "node_id": _step_id(item.get("node_id") or item.get("id"), index),
                "title": title,
                "prompt": prompt,
                "agent_type": agent_type,
                "role": role,
                "objective": objective,
                "phase": phase,
                "depends_on": [dep for dep in depends_on if dep != step_id],
                "required_for_final": _bool_field(item.get("required_for_final"), True),
                "read_only": read_only,
                "write_scope": _string_list(item.get("write_scope")),
                "assignee": str(item.get("assignee") or "").strip(),
            }
            steps.append(step)
            previous_step_id = step_id
        return steps

    async def _launch_ready_steps(
        self,
        steps: list[dict[str, Any]],
        *,
        context: ToolExecutionContext | None,
        timeout_seconds: float,
        workflow_id: str,
        workflow_name: str,
        workflow_mode: str,
    ) -> ToolResult:
        if self._artifact_store is None:
            return self._error_result("Workflow runtime is not configured")
        task_tool = TaskTool(
            llm_provider=self._llm_provider,
            tool_registry_provider=self._tool_registry_provider,
            artifact_store=self._artifact_store,
            permission_checker_provider=self._permission_checker_provider,
            agent_settings_provider=self._agent_settings_provider,
            token_budget_provider=self._token_budget_provider,
        )
        runtime = _runtime(context)
        payload = {
            "parallel_tasks": [
                {
                    "description": f"{step['title']} ({step['task_id']})",
                    "prompt": (
                        f"Workflow step task_id={step['task_id']}.\n"
                        f"{_dependency_output_context(runtime, _string_list(step.get('blocked_by')))}\n\n"
                        f"{step['prompt']}"
                    ).strip(),
                    "agent_type": step["agent_type"],
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "workflow_mode": workflow_mode,
                    "node_id": step["node_id"],
                    "task_id": step["task_id"],
                    "objective": step["objective"],
                    "depends_on": list(step["depends_on"]),
                    "blocked_by": _string_list(step.get("blocked_by")),
                    "required_for_final": step["required_for_final"],
                    "read_only": step["read_only"],
                    "write_scope": step["write_scope"],
                    "current_activity": "Launched by workflow",
                }
                for step in steps
            ],
            "run_in_background": True,
            "timeout_seconds": min(max(timeout_seconds, 30.0), 600.0),
        }
        if len(steps) == 1:
            only = payload["parallel_tasks"][0]
            return await task_tool.execute(
                {
                    "description": only["description"],
                    "prompt": only["prompt"],
                    "agent_type": only["agent_type"],
                    "workflow_id": only["workflow_id"],
                    "workflow_name": only["workflow_name"],
                    "workflow_mode": only["workflow_mode"],
                    "node_id": only["node_id"],
                    "task_id": only["task_id"],
                    "objective": only["objective"],
                    "depends_on": only["depends_on"],
                    "blocked_by": only["blocked_by"],
                    "required_for_final": only["required_for_final"],
                    "read_only": only["read_only"],
                    "write_scope": only["write_scope"],
                    "current_activity": only["current_activity"],
                    "run_in_background": True,
                    "timeout_seconds": payload["timeout_seconds"],
                },
                context=context,
            )
        return await task_tool.execute(payload, context=context)
