from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import AgentEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.grants import permission_profile_payload
from backend.tools.request_permissions import RequestPermissionsTool
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.turn_wait_state import TurnWaitState


class Session(SessionApprovalRuntimeMixin):
    def __init__(self):
        self.session_id = "session"
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache = {}
        self.permission_context = PermissionContext()
        self.send_event = AsyncMock()
        self.conversation_repo = None
        self.config = SimpleNamespace(agent=SimpleNamespace(approval_timeout_seconds=None))


@pytest.mark.asyncio
async def test_permission_reply_racing_waiter_preserves_subset_owner_and_session_scope(tmp_path: Path):
    session = Session()
    requested_path = tmp_path / "approved"
    foreign_path = tmp_path / "not-requested"
    receipt = []

    async def emit(event_type, data):
        wire = session.build_approval_request_payload(AgentEvent(type=event_type, data=data))
        receipt.append(wire)
        assert wire["request"]["subtype"] == "request_permissions"
        wrong_owner = {"conversation_id": "other", "turn_id": "turn", "message_id": "answer"}
        assert session.approval_response_owner_error("call", wrong_owner)
        command = {"request_id": "call", "conversation_id": "owner", "turn_id": "turn", "message_id": "answer",
                   "response": {"subtype": "success", "response": {
                       "permissions": {"file_system": {"write": [str(requested_path), str(foreign_path)]}}, "scope": "session"}}}
        request_id, response = session._normalize_control_response(command)
        assert not session.approval_response_owner_error(request_id, response)
        assert session._resolve_pending_approval(request_id, response)
        assert not session._resolve_pending_approval(request_id, response)

    grants = session.permission_grants_for_conversation("owner")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path,
        conversation_id="owner", turn_id="turn", tool_call_id="call", authorization_digest="frozen-digest",
        metadata={"assistant_message_id": "answer"}, permission_grants=grants,
        emit_event=emit, approval_handler=session.approval_handler)
    result = await RequestPermissionsTool().execute({"permissions": {"file_system": {"write": [str(requested_path)]}}}, context)
    assert not result.is_error
    response = json.loads(result.content)
    assert response["scope"] == "session"
    assert response["permissions"]["file_system"] == {"write": [str(requested_path)]}
    assert receipt[0]["request"]["request_digest"] == "frozen-digest"
    assert session.turn_wait_state.pending_approval_payloads == {}
    grants.end_turn("turn")
    assert permission_profile_payload(grants.granted("local", "next"))["file_system"] == {"write": [str(requested_path)]}
    assert session.permission_grants_for_conversation("other").granted("local", "next").is_empty
    assert grants.granted("remote", "next").is_empty


@pytest.mark.asyncio
async def test_permission_cancellation_never_records_a_grant(tmp_path: Path):
    session = Session()
    published = asyncio.Event()

    async def emit(event_type, data):
        session.build_approval_request_payload(AgentEvent(type=event_type, data=data))
        published.set()

    grants = session.permission_grants_for_conversation("owner")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path,
        conversation_id="owner", turn_id="turn", tool_call_id="call", permission_grants=grants,
        emit_event=emit, approval_handler=session.approval_handler)
    execution = asyncio.create_task(RequestPermissionsTool().execute({"permissions": {"network": {"enabled": True}}}, context))
    await published.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert grants.granted("local", "turn").is_empty
    assert session.turn_wait_state.pending_approval_payloads == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype", ["request_permissions", "write_stdin"])
async def test_internal_authority_request_does_not_borrow_generic_session_approval(subtype):
    session = Session()
    details = {"permissions": {"network": {"enabled": True}}} if subtype == "request_permissions" else {"stdin_action": {"input": "literal"}}
    data = {"subtype": subtype, "tool_call_id": "call", "tool_name": "request_permissions" if subtype == "request_permissions" else "monitor",
            "args": {"command_id": "owned", "chars": "literal"}, "conversation_id": "owner", **details}
    wire = session.build_approval_request_payload(AgentEvent(type="approval_request", data=data))
    session._mark_session_approved(data["tool_name"], data["args"], payload=wire)
    assert not session._resolve_pending_approval("call", {"action": "approve", "auto_approved": True})
    wait = asyncio.create_task(session.approval_handler("call"))
    await asyncio.sleep(0)
    assert not wait.done()
    assert session._resolve_pending_approval("call", {"action": "reject"})
    reply = await wait
    if subtype == "request_permissions":
        assert reply["permissions"] == {} and reply["scope"] == "turn"
    else:
        assert reply["action"] == "reject"
