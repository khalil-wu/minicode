"""A permission-mode switch approves only what the new mode itself allows."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.config import permission_settings_from_config
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.permissions.exec_policy import ExecPolicyOwner
from backend.sandbox import SandboxPolicy
from backend.services.conversation_permission_service import plan_permission_mode_update
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry
from backend.tools.write_file import WriteFileTool
from backend.tests.native_command_harness import configure_command_context, authorize_command_call
from backend.tests.test_codex_mcp_approval_producer import proxy as mcp_proxy
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.turn_wait_state import TurnWaitState
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin


def test_permission_mode_event_keeps_its_owner_while_delivery_is_pending(tmp_path: Path) -> None:
    delivered: list[dict] = []

    async def scenario() -> None:
        release = asyncio.Event()

        async def send_payload(payload: dict, **_kwargs) -> None:
            await release.wait()
            delivered.append(payload)

        workspace = tmp_path / "owner-a"
        session = SimpleNamespace(
            session_id="permission-session",
            active_conversation_id="owner-a",
            permission_context=SimpleNamespace(mode="bypass", source="frontend.ui"),
            session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: workspace),
            send_payload=send_payload,
        )
        delivery = asyncio.create_task(SessionPermissionRuntimeMixin.emit_permission_mode_updated(session))
        await asyncio.sleep(0)
        session.active_conversation_id = "owner-b"
        workspace = tmp_path / "owner-b"
        session.permission_context.mode = "confirm"
        release.set()
        await delivery

    asyncio.run(scenario())
    assert delivered == [{
        "type": "permission.mode.updated",
        "session_id": "permission-session",
        "conversation_id": "owner-a",
        "workspace_root": str(tmp_path / "owner-a"),
        "mode": "bypass",
        "source": "frontend.ui",
    }]


class _Session(SessionApprovalRuntimeMixin):
    def __init__(self, workspace: Path) -> None:
        self.session_id = "session-1"
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache = {}
        self._settled_approval_notifications: set[str] = set()
        self.send_event = AsyncMock()
        self.permission_checker = PermissionChecker(permission_settings_from_config({}), workspace_root=workspace)
        self.permission_context = self.permission_checker.build_context(mode="confirm")
        self.tool_registry = ToolRegistry()
        self.tool_registry.register(WriteFileTool())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bypass", "auto"])
@pytest.mark.parametrize("kind", ["command", "mcp"])
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_mode_switch_retains_frozen_source_request_until_its_explicit_response(tmp_path: Path, mode: str, kind: str, action: str) -> None:
    session = _Session(tmp_path)
    command_tool = RunCommandTool(None)
    context = configure_command_context(command_tool,
        ToolExecutionContext(session.permission_context, workspace_root=tmp_path,
            conversation_id="conversation-1", session_id=session.session_id, turn_id="issuing-turn",
            metadata={"assistant_message_id": "issuing-answer", "run_id": "issuing-turn"}),
        sandbox_policy=SandboxPolicy.bypass())
    transport = None
    if kind == "command":
        tool = command_tool
        args = {"command": "echo original-owned"}
        captured = (await authorize_command_call(ToolCallEvent(id="inspect-command", name=tool.name, arguments=args), context))
        home = tmp_path / "native-policy"
        (home / "rules").mkdir(parents=True)
        (home / "rules/default.rules").write_text("\n".join(
            f'prefix_rule(pattern={json.dumps(list(command))}, decision="prompt")'
            for command in captured.request.exec_policy.parsed_commands), encoding="utf-8")
        stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(home / "settings.json")), {}),))
        context.run_context.exec_policy_owner = ExecPolicyOwner(home=home, stack=stack)
    else:
        tool, transport, _ = mcp_proxy({}, "prompt")
        args = {"value": "original-owned"}
    registry = ToolRegistry()
    registry.register(tool)
    session.tool_registry = registry
    context.tool_registry = registry
    context.run_context.active_tool_registry = registry
    builder = ContextBuilder()
    builder.bind_tool_context(context)
    state = AgentState(user_message="Perform the original action")
    batch = ToolBatchRunner(ctx=builder, state=state, tool_registry=registry,
        permission_checker=context.permission_checker, approval_handler=session.approval_handler,
        skill_manager=None, permission_context=context.permission, tool_ctx=context)
    emitted, ready = [], asyncio.Event()
    async def execute():
        async for event in batch.run([ToolCallEvent(id="frozen-call", name=tool.name, arguments=args)]):
            emitted.append(event)
            if event.type == "approval_request":
                event.data.update(conversation_id=context.conversation_id, turn_id=context.turn_id,
                    message_id="issuing-answer")
                session.build_approval_request_payload(event)
                ready.set()
    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(ready.wait(), 8)
        original = copy.deepcopy(session.turn_wait_state.pending_approval_payloads["frozen-call"])
        original_profile = context.sandbox_policy
        assert ("available_decisions" if kind == "command" else "mcp_approval") in original["request"]
        # Change the session default and even add a local explicit allow.
        # The issuing batch and immutable pending request retain their owners.
        session.permission_checker = PermissionChecker(permission_settings_from_config({
            "permissions": {"content_allow_rules": [tool.policy_identity]},
        }), tmp_path)
        session.permission_context = session.permission_checker.build_context(mode=mode)
        plan = plan_permission_mode_update({"mode": mode}, active_conversation_id="conversation-1",
            config=context.model_execution.config)
        assert not plan.error_event
        assert await session.auto_approve_pending_tool_approvals(reason=plan.auto_approve_reason,
            conversation_id=plan.conversation_id) == []
        assert not task.done()
        assert session.turn_wait_state.pending_approval_payloads["frozen-call"] == original
        assert context.sandbox_policy is original_profile
        assert context.permission.mode == "confirm"
        assert not session._resolve_pending_approval("frozen-call", {"action": "approve", "auto_approved": True})
        wrong = {"action": action, "conversation_id": "another-conversation", "turn_id": context.turn_id,
            "message_id": "issuing-answer", "request_digest": original["request"]["request_digest"]}
        assert session.approval_response_owner_error("frozen-call", wrong)
        response = {**wrong, "conversation_id": context.conversation_id}
        if kind == "command":
            response["decision"] = "approved" if action == "approve" else "abort"
        assert not session.approval_response_owner_error("frozen-call", response)
        assert not session._resolve_pending_approval("frozen-call", {**response, "request_digest": "different-request"})
        assert session._resolve_pending_approval("frozen-call", response)
        if kind == "command" and action == "reject":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)
        else:
            await asyncio.wait_for(task, 15)
        record = state.tool_calls[-1]
        assert record.request_digest == original["request"]["request_digest"]
        assert record.status == ("success" if action == "approve" else "cancelled" if kind == "command" else "blocked")
        if kind == "mcp":
            assert [entry[1] for entry in transport.calls] == ([args] if action == "approve" else [])
        elif action == "approve":
            assert any(event.type == "tool_result" and "original-owned" in str(event.data.get("summary")) for event in emitted)
        assert not session.turn_wait_state.pending_approval_payloads
        assert not session.turn_wait_state.pending_approvals
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_bypass_switch_keeps_ordinary_local_prompt_behavior(tmp_path: Path) -> None:
    session = _Session(tmp_path)

    async def scenario():
        payload = {"type": "control_request", "request_id": "local-write", "conversation_id": "conversation-1",
            "request": {"subtype": "can_use_tool", "tool_name": "write_file",
                "input": {"file_path": str(tmp_path / "local.txt"), "content": "original"}}}
        session.turn_wait_state.pending_approval_payloads["local-write"] = payload
        waiter = asyncio.get_running_loop().create_future()
        session.turn_wait_state.register_waiter("local-write", waiter)
        session.permission_context = session.permission_checker.build_context(mode="bypass")
        assert await session.auto_approve_pending_tool_approvals(reason="permission_mode_bypass",
            conversation_id="conversation-1") == ["local-write"]
        assert waiter.result()["action"] == "approve"

    asyncio.run(scenario())
