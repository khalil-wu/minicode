from __future__ import annotations

import asyncio

from backend.agent.run_context import RunContext
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import PermissionLevel
from backend.tools.plan_tool import EnterPlanModeTool, ExitPlanModeTool


def test_exit_plan_mode_rejects_the_retired_json_draft_plan_shape(tmp_path):
    asyncio.run(_test_exit_plan_mode_rejects_the_retired_json_draft_plan_shape(tmp_path))


async def _test_exit_plan_mode_rejects_the_retired_json_draft_plan_shape(tmp_path):
    """The JSON draft-plan compat adapter is gone; the Markdown contract is the only one.

    ``get_execution_schema`` declares ``plan`` as a string with
    ``additionalProperties: False``, so the chokepoint's ``validate_tool_input``
    already refused this shape before ``execute`` ran — the adapter was
    unreachable code that also wrote ``.minicode/plans/<session>.json`` through a
    ``Path.cwd()`` fallback, bypassing backend/agent/plans.py's
    slug/containment/symlink guards.
    """
    from backend.tools.base import validate_tool_input

    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    ctx = ToolExecutionContext(
        permission=PermissionContext(mode="plan"),
        session_id="session-1",
        emit_event=emit,
        run_context=RunContext(),
    )
    tool = ExitPlanModeTool(workspace_root=tmp_path)
    legacy_args = {
        "plan": [
            {"step": "Inspect the failing path"},
            {"step": "Patch the root cause"},
        ],
        "explanation": "Ready to implement.",
    }

    assert "Additional properties are not allowed" in validate_tool_input(
        tool, dict(legacy_args)
    )
    assert "is not of type 'string'" in validate_tool_input(
        tool, {"plan": legacy_args["plan"]}
    )

    result = await tool.execute(legacy_args, context=ctx)

    assert result.is_error is True
    assert result.status != "draft"
    assert events == []
    assert not (tmp_path / ".minicode" / "plans" / "session-1.json").exists()


def test_enter_plan_mode_switches_context_to_plan_without_restoring_writes(tmp_path):
    asyncio.run(_test_enter_plan_mode_switches_context_to_plan_without_restoring_writes(tmp_path))


async def _test_enter_plan_mode_switches_context_to_plan_without_restoring_writes(tmp_path):
    setter_calls: list[tuple[str, str]] = []

    async def set_mode(mode: str, *, source: str) -> None:
        setter_calls.append((mode, source))

    ctx = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="unit"),
        run_context=RunContext(permission_mode_setter=set_mode),
    )

    result = await EnterPlanModeTool(workspace_root=tmp_path).execute(
        {"reason": "Need design approval first."},
        context=ctx,
    )

    assert result.is_error is False
    assert ctx.permission.mode == "plan"
    assert ctx.permission.source == "enter_plan_mode"
    assert setter_calls == [("plan", "enter_plan_mode")]


def test_enter_plan_mode_is_unavailable_in_every_agent_context() -> None:
    tool = EnterPlanModeTool()

    assert tool.check_permission(
        context=PermissionContext(source="subagent:implement")
    ) == PermissionLevel.ALWAYS_DENY
    assert tool.check_permission(
        context=PermissionContext(source="teammate:reviewer")
    ) == PermissionLevel.ALWAYS_DENY
    assert tool.check_permission(
        context=PermissionContext(source="runtime")
    ) == PermissionLevel.AUTO


def test_required_plan_teammate_submits_to_leader_mailbox(tmp_path) -> None:
    async def run() -> None:
        plan_path = tmp_path / "teammate-plan.md"
        plan_path.write_text("# Plan\n\n1. Inspect\n2. Implement", encoding="utf-8")
        requests: list[dict[str, str]] = []
        setter_calls: list[tuple[str, str]] = []

        async def request_approval(**kwargs):
            requests.append(dict(kwargs))
            return {"queued": True, "request_id": "plan-request-1"}

        async def set_mode(mode: str, *, source: str) -> None:
            setter_calls.append((mode, source))

        ctx = ToolExecutionContext(
            permission=PermissionContext(
                mode="plan",
                source="teammate:reviewer:required_plan",
                filesystem_constraints={"plan_files": [str(plan_path)]},
            ),
            run_context=RunContext(
                teammate_plan_approval_requester=request_approval,
                permission_mode_setter=set_mode,
            ),
        )

        tool = ExitPlanModeTool()
        assert tool.check_permission(
            context=ctx.permission
        ) == PermissionLevel.AUTO
        result = await tool.execute({}, context=ctx)

        assert result.is_error is False
        assert result.status == "waiting"
        assert "plan-request-1" in result.content
        assert requests == [
            {
                "plan": "# Plan\n\n1. Inspect\n2. Implement",
                "plan_file_path": str(plan_path),
            }
        ]
        assert setter_calls == []
        assert ctx.permission.mode == "plan"

    asyncio.run(run())


def test_non_required_teammate_exits_plan_locally_without_user_prompt(tmp_path) -> None:
    async def run() -> None:
        plan_path = tmp_path / "voluntary-plan.md"
        plan_path.write_text("# Plan\n\nInspect then implement", encoding="utf-8")
        setter_calls: list[tuple[str, str]] = []

        async def set_mode(mode: str, *, source: str) -> None:
            setter_calls.append((mode, source))

        ctx = ToolExecutionContext(
            permission=PermissionContext(
                mode="plan",
                source="teammate:reviewer",
                filesystem_constraints={"plan_files": [str(plan_path)]},
            ),
            run_context=RunContext(permission_mode_setter=set_mode),
        )

        result = await ExitPlanModeTool().execute({}, context=ctx)

        assert result.is_error is False
        assert result.status == "completed"
        assert setter_calls == [("confirm", "exit_plan_mode")]

    asyncio.run(run())
