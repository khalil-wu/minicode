from __future__ import annotations

import asyncio
from pathlib import Path

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import populate_prompt_context
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.plan_tool import UpdatePlanTool


def test_agent_mode_plan_uses_codex_conversational_instructions_without_changing_permissions(tmp_path: Path) -> None:
    permission = PermissionContext(mode="bypass", workspace_root=tmp_path)
    state = AgentState(user_message="Plan a feature")
    populate_prompt_context(state=state, metadata={"agent_mode": "plan"}, workspace_root=tmp_path, permission_context=permission)
    builder = ContextBuilder()
    asyncio.run(builder.start_turn(state.user_message, state))
    messages = asyncio.run(builder.build(state))
    instruction = next(message.content for message in messages if message.role == "developer")
    assert "# Plan Mode (Conversational)" in instruction
    assert "<proposed_plan>" in instruction
    assert "Tests, builds, or checks" in instruction
    assert "must not perform **mutating** actions" in instruction
    assert "request_user_input" in instruction
    assert "ask_user" not in instruction
    assert "exit_plan_mode" not in instruction and "Plan file" not in instruction
    assert permission.mode == "bypass"
    assert state.prompt_context["environment"]["permission"]["file_system_type"] == "unrestricted"
    assert "# Plan Mode (Conversational)" not in messages[-1].content


def test_build_mode_explicitly_ends_conversational_plan_and_leaves_permission_plan_separate(tmp_path: Path) -> None:
    state = AgentState(user_message="Implement")
    populate_prompt_context(state=state, metadata={"agent_mode": "build"}, workspace_root=tmp_path, permission_context=PermissionContext(mode="auto"))
    default = ContextBuilder._build_collaboration_mode_block(state)
    assert "# Collaboration Mode: Default" in default
    assert "previous instructions for other modes" in default
    assert "request_user_input" in default
    populate_prompt_context(state=state, metadata={"agent_mode": "build"}, workspace_root=tmp_path,
                          permission_context=PermissionContext(mode="plan", filesystem_constraints={"plan_files":[str(tmp_path / "plan.md")]}))
    legacy = ContextBuilder._build_collaboration_mode_block(state)
    assert "# Collaboration Mode: Default" in legacy
    assert "exit_plan_mode" not in legacy
    assert state.prompt_context["collaboration_mode"] == "default"
    assert state.prompt_context["environment"]["permission"]["mode"] == "plan"
    assert state.prompt_context["plan_file_path"] == str(tmp_path / "plan.md")


def test_update_plan_returns_a_real_error_in_conversational_plan_without_mutating_turn_checklist() -> None:
    emitted = []
    async def emit(name, data):
        emitted.append((name, data))
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), metadata={"agent_mode":"plan"}, emit_event=emit)
    result = asyncio.run(UpdatePlanTool().execute({"plan":[{"step":"Implement", "status":"in_progress"}]}, context))
    assert result.is_error
    assert "TODO/checklist" in result.content
    assert emitted == []
    context.metadata["agent_mode"] = "build"
    result = asyncio.run(UpdatePlanTool().execute({"plan":[{"step":"Implement", "status":"in_progress"}]}, context))
    assert not result.is_error
    assert emitted[0][0] == "turn.plan.updated"
