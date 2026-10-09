"""A permission-mode switch approves only what the new mode itself allows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.services.conversation_permission_service import plan_permission_mode_update
from backend.tools.command_tool import RunCommandTool
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.turn_wait_state import TurnWaitState
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin


class _Registry:
    def __init__(self) -> None:
        self._tool = RunCommandTool(None)

    def get_tool(self, name: str):
        return self._tool if name == "run_command" else None


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
        self.permission_checker = PermissionChecker(PermissionSettings(), workspace_root=workspace)
        self.permission_context = self.permission_checker.build_context(mode="confirm")
        self.tool_registry = _Registry()


async def _switch_mode(session: _Session, mode: str, commands: dict[str, str]):
    waiters = {}
    for call_id, command in commands.items():
        session.turn_wait_state.pending_approval_payloads[call_id] = {
            "type": "control_request",
            "request_id": call_id,
            "request": {"subtype": "can_use_tool", "tool_name": "run_command", "input": {"command": command}},
            "conversation_id": "conversation-1",
        }
        waiter = asyncio.get_running_loop().create_future()
        session.turn_wait_state.register_waiter(call_id, waiter)
        waiters[call_id] = waiter
    plan = plan_permission_mode_update({"mode": mode}, active_conversation_id="conversation-1")
    # Production order: commit the new mode first, then settle pending prompts.
    session.permission_context = session.permission_checker.build_context(mode=mode)
    approved = await session.auto_approve_pending_tool_approvals(
        reason=plan.auto_approve_reason,
        conversation_id=plan.conversation_id,
    )
    return approved, waiters


@pytest.mark.parametrize("mode", ["bypass", "auto"])
def test_mode_switch_keeps_destructive_commands_pending(tmp_path: Path, mode: str) -> None:
    session = _Session(tmp_path)

    async def scenario():
        approved, waiters = await _switch_mode(session, mode, {
            "call-force-push": "git push --force origin main",
            "call-hard-reset": "git reset --hard HEAD~20",
        })
        assert approved == []
        assert not any(waiter.done() for waiter in waiters.values())
        assert set(session.turn_wait_state.pending_approval_payloads) == {"call-force-push", "call-hard-reset"}

    asyncio.run(scenario())


def test_bypass_switch_still_approves_what_bypass_runs_without_asking(tmp_path: Path) -> None:
    session = _Session(tmp_path)

    async def scenario():
        approved, waiters = await _switch_mode(session, "bypass", {
            "call-tests": "npm test",
            "call-force-push": "git push --force origin main",
        })
        assert approved == ["call-tests"]
        assert waiters["call-tests"].done()
        assert waiters["call-tests"].result()["action"] == "approve"
        assert not waiters["call-force-push"].done()

    asyncio.run(scenario())
