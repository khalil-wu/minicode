"""User-review gate for teammate plan approval requests.

Approval is the user's decision: only bypass leaders may answer
teammate plan requests directly; every other session surfaces the request
and waits for ``subagent.plan_review``.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from backend.agent.mailbox_delivery import _handle_parent_plan_approval_requests
from backend.agent.run_context import RunContext


def _request_message(request_id: str = "plan_approval:t1:abc") -> Any:
    @dataclass
    class _Msg:
        content: str
        recipient_id: str = "parent"
        sender_id: str = "t1"
        team_name: str = "team-a"
        sender_mailbox_epoch: int = 3
        message_id: str = "m-1"

        def public_dict(self) -> dict[str, Any]:
            return {"message_id": self.message_id}

    return _Msg(
        content=json.dumps(
            {
                "type": "plan_approval_request",
                "from": "tester",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "plan_file_path": "/ws/.minicode/plans/t1.md",
                "plan_content": "# Plan",
                "request_id": request_id,
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
        self.reserved = 0
        self.committed = 0

    def list_swarm_messages(self, **kwargs: Any) -> list[Any]:
        return [_request_message()]

    def get_subagent(self, subagent_id: str) -> Any:
        return _Teammate() if subagent_id == "t1" else None

    def get_run(self, run_id: str) -> Any:
        return _ParentRun() if run_id == "run-parent" else None

    def reserve_lifecycle_response(self, **kwargs: Any) -> str:
        self.reserved += 1
        return "token-1"

    def release_lifecycle_response(self, **kwargs: Any) -> bool:
        return True

    def commit_lifecycle_response(self, **kwargs: Any) -> bool:
        self.committed += 1
        return True

    def send_swarm_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def _run_context(mode: str) -> RunContext:
    return RunContext(
        permission_context_provider=lambda: type("_Ctx", (), {"mode": mode})()
    )


def test_confirm_mode_surfaces_request_and_waits_for_user() -> None:
    async def run() -> None:
        runtime = _FakeRuntime()
        events: list[tuple[str, dict[str, Any]]] = []

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        metadata: dict[str, Any] = {}
        run_context = _run_context("confirm")
        handled = await _handle_parent_plan_approval_requests(
            runtime=runtime,
            parent_run_id="run-parent",
            conversation_id="conv-1",
            emit_event=emit,
            metadata=metadata,
            run_context=run_context,
        )
        assert handled == 0
        assert runtime.sent == []
        assert [(kind, data["request_id"]) for kind, data in events] == [
            ("subagent.plan_approval_requested", "plan_approval:t1:abc")
        ]

        # A repeated poll must not re-surface the same pending request.
        await _handle_parent_plan_approval_requests(
            runtime=runtime,
            parent_run_id="run-parent",
            conversation_id="conv-1",
            emit_event=emit,
            metadata=metadata,
            run_context=run_context,
        )
        assert len(events) == 1

    asyncio.run(run())


def test_bypass_mode_leader_answers_directly() -> None:
    async def run() -> None:
        runtime = _FakeRuntime()
        events: list[tuple[str, dict[str, Any]]] = []

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        handled = await _handle_parent_plan_approval_requests(
            runtime=runtime,
            parent_run_id="run-parent",
            conversation_id="conv-1",
            emit_event=emit,
            metadata={},
            run_context=_run_context("bypass"),
        )
        assert handled == 1
        assert runtime.reserved == 1 and runtime.committed == 1
        assert len(runtime.sent) == 1
        payload = json.loads(runtime.sent[0]["content"])
        assert payload["approved"] is True
        assert payload["permission_mode"] == "confirm"

    asyncio.run(run())


def test_auto_mode_requires_user_decision_too() -> None:
    async def run() -> None:
        runtime = _FakeRuntime()
        events: list[tuple[str, dict[str, Any]]] = []

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        handled = await _handle_parent_plan_approval_requests(
            runtime=runtime,
            parent_run_id="run-parent",
            conversation_id="conv-1",
            emit_event=emit,
            metadata={},
            run_context=_run_context("auto"),
        )
        assert handled == 0
        assert runtime.sent == []
        assert events and events[0][0] == "subagent.plan_approval_requested"

    asyncio.run(run())
