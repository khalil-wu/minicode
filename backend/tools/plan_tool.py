"""MiniCode plan management and checklist tool."""

from __future__ import annotations

import inspect
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.agent.message import AgentEvent
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

_VALID_INPUT_STATUSES = {"pending", "in_progress", "completed"}


def _safe_session_filename(value: Any) -> str:
    text = str(value or "session").strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-") or "session"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class UpdatePlanTool(BaseTool):
    """Create, replace, or clear the turn's visible task checklist."""

    name = "update_plan"
    result_kind = "plan"
    activity_kind = "status"
    display_label = "Update plan"
    mutates_workspace = False
    read_only = False  # mutates session plan state
    permission = PermissionLevel.AUTO

    def is_capability_available(self, context=None) -> bool:
        return context is None or context.mode != "plan"
    description = (
        "Updates the task plan. Provide an optional explanation and a list of plan items, each with a step and "
        "status. At most one step can be in_progress at a time."
    )

    def check_permission(
        self,
        args: dict[str, Any] | None = None,
        context: PermissionContext | None = None,
    ) -> PermissionLevel | None:
        # Codex treats update_plan as a TODO/checklist tool and explicitly
        # disallows it in Plan mode. Plan-mode proposals use exit_plan_mode.
        if context is not None and context.mode == "plan":
            return PermissionLevel.ALWAYS_DENY
        return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "array",
                        "description": "The list of steps",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string", "description": "Task step text."},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Step status.",
                                },
                            },
                            "required": ["step", "status"],
                            "additionalProperties": False,
                        },
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Optional explanation for this plan update.",
                    },
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        unexpected = set(args) - {"plan", "explanation"}
        if unexpected:
            return ToolResult(
                content=f"update_plan received unsupported fields: {', '.join(sorted(unexpected))}.",
                is_error=True,
            )
        plan = args.get("plan")
        if not isinstance(plan, list):
            return ToolResult(content="update_plan requires a plan array.", is_error=True)
        steps: list[dict[str, str]] = []
        for index, item in enumerate(plan):
            if not isinstance(item, dict):
                return ToolResult(content=f"Plan step {index + 1} must be an object.", is_error=True)
            unexpected_item = set(item) - {"step", "status"}
            if unexpected_item:
                return ToolResult(
                    content=(
                        f"Plan step {index + 1} received unsupported fields: "
                        f"{', '.join(sorted(unexpected_item))}."
                    ),
                    is_error=True,
                )
            title = item.get("step")
            raw_status = item.get("status")
            if not isinstance(title, str) or not isinstance(raw_status, str):
                return ToolResult(
                    content=f"Plan step {index + 1} requires string step and status fields.",
                    is_error=True,
                )
            if raw_status not in _VALID_INPUT_STATUSES:
                return ToolResult(
                    content=f"Invalid status '{raw_status}'; use pending / in_progress / completed.",
                    is_error=True,
                )
            steps.append({"step": title, "status": raw_status})

        raw_explanation = args.get("explanation")
        if raw_explanation is not None and not isinstance(raw_explanation, str):
            return ToolResult(content="update_plan explanation must be a string.", is_error=True)
        explanation = raw_explanation if isinstance(raw_explanation, str) else None

        emit_event = getattr(context, "emit_event", None) if context else None
        if emit_event is not None:
            metadata = getattr(context, "metadata", None)
            metadata = metadata if isinstance(metadata, dict) else {}
            await emit_event(
                "turn.plan.updated",
                AgentEvent.turn_plan_updated(
                    thread_id=str(getattr(context, "conversation_id", "") or ""),
                    turn_id=str(metadata.get("run_id") or metadata.get("turn_id") or ""),
                    explanation=explanation,
                    plan=steps,
                ).data,
            )

        return ToolResult(content="Plan updated")

class ExitPlanModeTool(BaseTool):
    """Submit a draft plan and wait for user approval before implementation."""

    name = "exit_plan_mode"
    result_kind = "plan"
    activity_kind = "status"
    display_label = "Submit plan"
    mutates_workspace = False
    read_only = False
    always_load = True
    permission = PermissionLevel.CONFIRM

    def is_capability_available(self, context=None) -> bool:
        return context is not None and context.mode == "plan"

    def capability_permission_level(self, context=None):
        return PermissionLevel.CONFIRM if self.is_capability_available(context) else PermissionLevel.ALWAYS_DENY
    description = (
        "Present the plan for approval and leave plan mode after the user approves. "
        "Use only after the plan has been written to the session plan file."
    )

    def __init__(self, workspace_root: Path | None = None) -> None:
        # The session plan file's owner is the live permission context
        # (backend.agent.plans), which applies the slug/containment/symlink
        # guards. The registry factory hands every tool the workspace root, so
        # the argument is accepted and deliberately not stored: a tool-local
        # root would be a second source of truth for the same file.
        del workspace_root

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command_prompts": {
                        "type": "array",
                        "description": "Optional categories of prompts needed to implement the plan.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string", "enum": ["run_command"]},
                                "prompt": {"type": "string"},
                            },
                            "required": ["tool", "prompt"],
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            },
        )

    def get_execution_schema(self) -> ToolSchema:
        """Accept host-owned approval fields without exposing them to models."""

        parameters = dict(self.get_schema().parameters)
        properties = dict(parameters.get("properties") or {})
        properties.update(
            {
                "plan": {
                    "type": "string",
                    "description": "Plan text approved or edited by the user.",
                },
                "plan_file_path": {
                    "type": "string",
                    "description": "Host-owned path of the session plan shown for approval.",
                },
            }
        )
        parameters["properties"] = properties
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=parameters,
        )

    def check_permission(
        self,
        args: dict[str, Any] | None = None,
        context: PermissionContext | None = None,
    ) -> PermissionLevel | None:
        if context is None or context.mode != "plan":
            return PermissionLevel.ALWAYS_DENY
        source = str(context.source or "")
        if source.startswith("teammate:"):
            # Claude teammates never show a local user approval. Required-plan
            # teammates route to the leader mailbox; voluntary Plan mode exits
            # locally.
            return PermissionLevel.AUTO
        return PermissionLevel.CONFIRM

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        if context is None or not isinstance(getattr(context, "permission", None), PermissionContext):
            return ToolResult(content="exit_plan_mode requires a live permission context.", is_error=True)
        if context.permission.mode != "plan":
            return ToolResult(
                content="You are not in plan mode. Continue implementation if the plan was already approved.",
                is_error=True,
            )
        from backend.agent.plans import current_plan_paths, read_plan, write_plan

        unexpected = set(args) - {"command_prompts", "plan", "plan_file_path"}
        if unexpected:
            return ToolResult(
                content=f"exit_plan_mode received unsupported fields: {', '.join(sorted(unexpected))}.",
                is_error=True,
            )
        allowed_prompts = args.get("command_prompts")
        if allowed_prompts is not None:
            if not isinstance(allowed_prompts, list):
                return ToolResult(content="command_prompts must be an array.", is_error=True)
            for index, item in enumerate(allowed_prompts):
                if (
                    not isinstance(item, dict)
                    or set(item) != {"tool", "prompt"}
                    or item.get("tool") != "run_command"
                    or not isinstance(item.get("prompt"), str)
                    or not item["prompt"].strip()
                ):
                    return ToolResult(
                        content=(
                            f"command_prompts[{index}] must contain exactly tool='run_command' "
                            "and a non-empty prompt string."
                        ),
                        is_error=True,
                    )
        paths = current_plan_paths(context.permission)
        if len(paths) != 1:
            return ToolResult(content="No exact plan-file owner is bound to this session.", is_error=True)
        path = paths[0]
        edited_plan = args.get("plan")
        if edited_plan is not None and not isinstance(edited_plan, str):
            return ToolResult(content="The edited plan must be Markdown text.", is_error=True)
        if isinstance(edited_plan, str):
            write_plan(path, edited_plan)
        plan = (edited_plan if isinstance(edited_plan, str) else read_plan(path) or "").strip()
        if not plan:
            return ToolResult(
                content=f"No plan file was found at {path}. Write the plan before calling exit_plan_mode.",
                is_error=True,
            )

        permission_source = str(context.permission.source or "")
        is_teammate = permission_source.startswith("teammate:")
        plan_required = permission_source.endswith(":required_plan")
        # Restore the mode the session held before entering plan mode. The
        # WebSocket host still rereads permission_previous_mode from the
        # repository, so this value only governs standalone SDK/loop callers;
        # keep the explicit pre-plan mode rather than inventing a second mode
        # sessions on those paths.
        pre_plan_mode = str(getattr(context.permission, "pre_plan_mode", "") or "").strip()
        approved_permission_mode = pre_plan_mode if pre_plan_mode and pre_plan_mode != "plan" else "confirm"
        if is_teammate and plan_required:
            mailbox_requester = (context.metadata or {}).get(
                "teammate_plan_approval_requester"
            )
            if not callable(mailbox_requester):
                return ToolResult(
                    content="Required teammate Plan approval mailbox is unavailable.",
                    is_error=True,
                )
            response = await _maybe_await(
                mailbox_requester(
                    plan=plan,
                    plan_file_path=str(path),
                )
            )
            if not isinstance(response, dict) or not response.get("queued"):
                feedback = str((response or {}).get("feedback") or "").strip()
                return ToolResult(
                    content=(
                        "Could not submit the plan to the team leader. Stay in Plan mode."
                        + (f" Feedback: {feedback}" if feedback else "")
                    ),
                    is_error=True,
                    result_kind="plan",
                    status="blocked",
                    display_summary="Plan approval request failed",
                )
            return ToolResult(
                content=(
                    "Plan submitted to the team leader for approval. "
                    "Remain in Plan mode until the matching mailbox response arrives.\n\n"
                    f"request_id: {response.get('request_id')}\n"
                    f"plan_file_path: {path}"
                ),
                result_kind="plan",
                status="waiting",
                display_summary="Awaiting team leader approval",
            )

        setter = (context.metadata or {}).get("permission_mode_setter")
        if callable(setter):
            # The host rereads permission_previous_mode from the repository;
            # model/run metadata is intentionally not trusted here.
            await _maybe_await(
                setter(
                    approved_permission_mode or "confirm",
                    source="exit_plan_mode",
                )
            )

        return ToolResult(
            content=(
                "User has approved your plan. You can now start coding. "
                f"\n\n## Approved Plan:\n{plan}"
            ),
            result_kind="plan",
            status="completed",
            display_summary="Exited plan mode",
        )


class EnterPlanModeTool(BaseTool):
    """Request a read-only planning turn."""

    name = "enter_plan_mode"
    result_kind = "plan"
    activity_kind = "status"
    display_label = "Enter plan mode"
    mutates_workspace = False
    read_only = False
    always_load = True
    permission = PermissionLevel.AUTO

    def is_capability_available(self, context=None) -> bool:
        if context is None:
            return True
        from backend.tools.subagent_context import is_subagent_permission_context
        return context.mode != "plan" and not is_subagent_permission_context(context)
    description = (
        "Enter read-only plan mode for investigation and design. "
        "Use when you need to inspect and propose a plan before making changes. "
        "Do not use this to bypass approval; finish with exit_plan_mode."
    )

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = workspace_root

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Optional legacy explanation for entering plan mode.",
                    },
                },
                "additionalProperties": False,
            },
        )

    def check_permission(self, args=None, context=None):
        from backend.tools.subagent_context import is_subagent_permission_context

        if context is not None and is_subagent_permission_context(context):
            return PermissionLevel.ALWAYS_DENY
        return PermissionLevel.AUTO

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        unexpected = set(args) - {"reason"}
        if unexpected or (
            "reason" in args and not isinstance(args.get("reason"), str)
        ):
            return ToolResult(content="enter_plan_mode does not accept arguments.", is_error=True)
        if context is not None:
            current = getattr(context, "permission", None)
            if isinstance(current, PermissionContext) and current.mode != "plan":
                metadata = getattr(context, "metadata", None)
                setter = metadata.get("permission_mode_setter") if isinstance(metadata, dict) else None
                if callable(setter):
                    await _maybe_await(setter("plan", source="enter_plan_mode"))
                provider = metadata.get("permission_context_provider") if isinstance(metadata, dict) else None
                if callable(provider):
                    refreshed = provider()
                    if isinstance(refreshed, PermissionContext):
                        context.permission = refreshed
                if context.permission.mode != "plan":
                    # Standalone SDK/loop callers do not have the WebSocket
                    # repository callback. Preserve the same immutable
                    # transition locally so the next model iteration sees
                    # plan permissions instead of continuing with build mode.
                    plan_constraints = dict(context.permission.filesystem_constraints)
                    if self._workspace_root is not None:
                        session_id = _safe_session_filename(getattr(context, "session_id", ""))
                        plan_path = (
                            self._workspace_root
                            / ".minicode"
                            / "plans"
                            / f"{session_id}.md"
                        ).resolve()
                        plan_constraints["plan_files"] = [str(plan_path)]
                    context.permission = replace(
                        context.permission,
                        mode="plan",
                        source="enter_plan_mode",
                        pre_plan_mode=current.mode,
                        approval_policy="on-request",
                        sandbox_mode="read-only",
                        filesystem_constraints=plan_constraints,
                    )
        return ToolResult(
            content=(
                "Plan mode is active. Workspace changes are disabled except for the exact session plan file. "
                "Write or edit that Markdown plan, then call exit_plan_mode for approval."
            ),
            result_kind="plan",
            display_summary="Entered plan mode",
        )
