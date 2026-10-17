"""Real Source empty app-approval forms retain their owner and explicit choice."""
from types import SimpleNamespace

from mcp import types
import pytest

from backend.bootstrap.app import AppBootstrap
from backend.mcp.client import MCPClient
from backend.permissions.approval_policy import ApprovalPolicy
from backend.permissions.context import PermissionContext
from backend.sandbox.policy import PermissionProfile
from backend.tests.test_mcp_client_owner_routing import _BootstrapElicitationSession


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["never", "on-request", {
    "granular": {"sandbox_approval": False, "rules": False, "mcp_elicitations": False},
}], ids=["never-full-write", "on-request", "ordinary-elicitation-disabled"])
@pytest.mark.parametrize("action,persist", [("accept", "session"), ("accept", "always"), ("decline", None)])
async def test_source_computer_use_empty_schema_requires_explicit_app_choice_and_keeps_owner(monkeypatch, policy, action, persist):
    parsed = ApprovalPolicy.parse(policy)
    permission = PermissionContext(approval_policy=parsed.kind,
        granular_approval=parsed.granular.to_dict() if parsed.granular else None)
    choice = {"action": action, **({"content": {"persist": persist}} if persist else {})}
    session = _BootstrapElicitationSession(response=choice)
    owner = {"session_id": "native-session", "conversation_id": "native-conversation", "turn_id": "native-turn",
        "message_id": "native-message", "task_id": "native-task", "mcp_permission_profile": PermissionProfile.disabled(),
        "permission_context_provider": lambda: permission}
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda params: (session, params["_minicode_owner"])
    client = MCPClient("cua_repl", elicitation_handler=bootstrap._handle_mcp_elicitation)
    client._active_tool_request_owners = {1: owner}
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    meta = {"codex_approval_kind": "mcp_tool_call", "connector_id": "computer-use", "connector_name": "Computer Use",
        "persist": ["session", "always"], "riskLevel": "low",
        "tool_params": {"app": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
        "tool_params_display": [{"name": "app", "display_name": "App", "value": "Calculator"}]}
    request = types.ElicitRequestFormParams(message="Allow Codex to use Calculator?", requestedSchema={}, **{"_meta": meta})
    result = await client._sdk_elicitation_callback(SimpleNamespace(request_id=1), request)
    assert isinstance(result, types.ElicitResult)
    assert result.action == action
    assert result.content == ({"persist": persist} if action == "accept" else None)
    assert len(session.sent) == 1  # Never/full-write must not silently accept it.
    payload = session.sent[0]
    assert {field: payload[field] for field in ("conversation_id", "turn_id", "message_id", "task_id")} == {
        "conversation_id": "native-conversation", "turn_id": "native-turn", "message_id": "native-message", "task_id": "native-task"}
    assert payload["request"]["schema"] == {}
    assert payload["request"]["_meta"] == meta
    assert payload["request"]["question"] == "Allow Codex to use Calculator?"
    assert session.turn_wait_state.waiter_ids() == set()
    assert session.turn_wait_state.pending_approval_payloads == {}
    assert session.terminal[-1]["reason"] == ("mcp_elicitation_resolved" if action == "accept" else "mcp_elicitation_rejected")
