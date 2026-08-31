"""User-facing ``subagent.plan_review`` WS command."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.ws.handlers.misc import handle_subagent_plan_review


def _request_message(request_id: str) -> Any:
    @dataclass
    class _Msg:
        content: str
        recipient_id: str = "parent"
        sender_id: str = "t1"
        team_name: str = "team-a"
        sender_mailbox_epoch: int = 3
        message_id: str = "m-1"

    return _Msg(
        content=json.dumps(
            {
                "type": "plan_approval_request",
                "from": "tester",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "planFilePath": "/ws/.minicode/plans/t1.md",
                "planContent": "# Plan",
                "requestId": request_id,
            }
        )
    )


@dataclass
class _Teammate:
    status: str = "running"
    parent_run_id: str = "run-parent"
    team_name: str = "team-a"
    teammate_name: str = "tester"
    mailbox_epoch: int = 3


@dataclass
class _ParentRun:
    run_id: str = "run-parent"
    conversation_id: str = "conv-1"


class _FakeRuntime:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.released = 0
        self.committed = 0

    def list_swarm_messages(self, **kwargs: Any) -> list[Any]:
        return [_request_message("plan_approval:t1:abc")]

    def get_subagent(self, subagent_id: str) -> Any:
        return _Teammate() if subagent_id == "t1" else None

    def get_run(self, run_id: str) -> Any:
        return _ParentRun() if run_id == "run-parent" else None

    def reserve_lifecycle_response(self, **kwargs: Any) -> str:
        return "token-1"

    def release_lifecycle_response(self, **kwargs: Any) -> bool:
        self.released += 1
        return True

    def commit_lifecycle_response(self, **kwargs: Any) -> bool:
        self.committed += 1
        return True

    def send_swarm_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class _FakeSession:
    session_id = "sess-1"
    active_conversation_id = "conv-1"
    conversation_repo = None

    def __init__(self) -> None:
        self.results: list[tuple[str, str, dict[str, Any]]] = []
        self.errors: list[Any] = []
        self.ws_manager = None
        self.session_lifecycle = SimpleNamespace(
            current_workspace_root=lambda: None,
            workspace_root=None,
        )

    @staticmethod
    def resolve_requested_workspace(requested_workspace: str | None = None) -> Path:
        return Path(requested_workspace or ".").expanduser().resolve()

    async def send_event(self, event: Any) -> None:
        self.errors.append(event)

    async def emit_command_result(
        self,
        command: str,
        message: str,
        *,
        level: str = "info",
        title: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.results.append((command, level, dict(data or {})))


@pytest.fixture()
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> _FakeRuntime:
    runtime = _FakeRuntime()
    import backend.agent.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "default_runtime", lambda: runtime)
    return runtime


def test_plan_review_approve_sends_user_decision(
    fake_runtime: _FakeRuntime,
) -> None:
    async def run() -> None:
        session = _FakeSession()
        handled = await handle_subagent_plan_review(
            session,
            {
                "conversation_id": "conv-1",
                "subagent_id": "t1",
                "request_id": "plan_approval:t1:abc",
                "approved": True,
            },
        )
        assert handled is True
        assert fake_runtime.committed == 1
        payload = json.loads(fake_runtime.sent[0]["content"])
        assert payload["approved"] is True
        assert payload["permissionMode"] == "confirm"
        command, level, data = session.results[-1]
        assert (command, level) == ("subagent.plan_review", "info")
        assert data["approved"] is True and data["granted_permission_mode"] == "confirm"

    asyncio.run(run())


def test_plan_review_reject_keeps_mode_absent(
    fake_runtime: _FakeRuntime,
) -> None:
    async def run() -> None:
        session = _FakeSession()
        await handle_subagent_plan_review(
            session,
            {
                "conversation_id": "conv-1",
                "subagent_id": "t1",
                "request_id": "plan_approval:t1:abc",
                "approved": False,
            },
        )
        payload = json.loads(fake_runtime.sent[0]["content"])
        assert payload["approved"] is False
        assert "permissionMode" not in payload

    asyncio.run(run())


def test_plan_review_rejects_unknown_request(
    fake_runtime: _FakeRuntime,
) -> None:
    async def run() -> None:
        session = _FakeSession()
        await handle_subagent_plan_review(
            session,
            {
                "conversation_id": "conv-1",
                "subagent_id": "t1",
                "request_id": "missing-request",
                "approved": True,
            },
        )
        error_event = session.errors[-1]
        assert error_event.type == "command.result"
        assert "missing-request" in str(error_event.data.get("message") or "")
        assert fake_runtime.sent == []
        # A failed reservation must not leak the lifecycle fence.
        assert fake_runtime.released == 0

    asyncio.run(run())
