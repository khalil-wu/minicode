from __future__ import annotations

import asyncio

from backend.agent.run_context import RunContext
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import PermissionLevel
from backend.tools.plan_tool import EnterPlanModeTool, ExitPlanModeTool


def test_exit_plan_mode_uses_the_canonical_owner_plan_file(tmp_path):
    asyncio.run(_test_exit_plan_mode_uses_the_canonical_owner_plan_file(tmp_path))


async def _test_exit_plan_mode_uses_the_canonical_owner_plan_file(tmp_path):
    plan_path = tmp_path / "session-plan.md"
    plan_path.write_text(
        "# Plan\n\n1. Inspect the failing path\n2. Patch the root cause\n",
        encoding="utf-8",
    )
    setter_calls: list[tuple[str, str]] = []

    async def set_mode(mode: str, *, source: str) -> None:
        setter_calls.append((mode, source))

    ctx = ToolExecutionContext(
        permission=PermissionContext(
            mode="plan",
            filesystem_constraints={"plan_files": [str(plan_path)]},
        ),
        session_id="session-1",
        run_context=RunContext(permission_mode_setter=set_mode),
    )

    result = await ExitPlanModeTool().execute(
        {"command_prompts": [{"tool": "run_command", "prompt": "Run focused tests"}]},
        context=ctx,
    )

    assert result.is_error is False
    assert result.status == "completed"
    assert result.result_kind == "plan"
    assert "User has approved your plan" in result.content
    assert "Inspect the failing path" in result.content
    assert setter_calls == [("confirm", "exit_plan_mode")]


def test_exit_plan_mode_requires_confirmation_in_plan_mode():
    tool = ExitPlanModeTool()

    assert (
        tool.check_permission(
            context=PermissionContext(
                mode="plan",
                filesystem_constraints={"plan_files": ["C:/workspace/plan.md"]},
            )
        )
        == PermissionLevel.CONFIRM
    )
    assert tool.check_permission(context=PermissionContext(mode="confirm")) == PermissionLevel.ALWAYS_DENY


def test_enter_plan_mode_switches_context_to_plan_without_restoring_writes(tmp_path):
    asyncio.run(_test_enter_plan_mode_switches_context_to_plan_without_restoring_writes(tmp_path))


async def _test_enter_plan_mode_switches_context_to_plan_without_restoring_writes(tmp_path):
    setter_calls: list[tuple[str, str]] = []

    async def set_mode(mode: str, *, source: str) -> None:
        setter_calls.append((mode, source))

    ctx = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="unit"),
        session_id="session-1",
        workspace_root=tmp_path,
        run_context=RunContext(permission_mode_setter=set_mode),
    )

    result = await EnterPlanModeTool(workspace_root=tmp_path).execute(
        {"reason": "Need design approval first."},
        context=ctx,
    )

    assert result.is_error is False
    assert ctx.permission.mode == "plan"
    assert ctx.permission.source == "enter_plan_mode"
    assert ctx.permission.pre_plan_mode == "bypass"
    assert len(ctx.permission.filesystem_constraints["plan_files"]) == 1
    assert setter_calls == [("plan", "enter_plan_mode")]
