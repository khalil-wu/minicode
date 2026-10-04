import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from backend.agent.prompting import build_git_status_context_async
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import sandbox_policy_for_permission_context
from backend.sandbox.runner import SandboxUnavailableError
from backend.services.subagent_service import build_subagent_transcript_messages

START = 1_790_834_473_040


def event(kind, time, payload):
    return {"event_type": kind, "event_id": f"event-{time}-{kind}", "ts_ms": time, "payload": payload}


@pytest.mark.parametrize("status", ["failed", "completed", "partial", "cancelled"])
def test_pre_prompt_terminal_uses_the_real_durable_turn_start(status):
    journal = {"agent_id": "subagent-preflight", "events": [
        event("system", START, {"lifecycle": "turn_started"}),
        event("system", START+3000, {"lifecycle": "error", "message": "sandbox unavailable", "recoverable": True}),
        event("terminal", START+3456, {"status": status, "reason": "preflight", "summary": ""}),
    ]}
    original = deepcopy(journal)
    messages = build_subagent_transcript_messages(journal)
    assert len(messages) == 1
    assistant = messages[0]
    assert assistant["role"] == "assistant"
    assert assistant["timestamp"] == START
    assert assistant["completed_at"] == START+3456
    assert assistant["duration_ms"] == 3456
    assert assistant["terminal_status"] == ("interrupted" if status == "cancelled" else status)
    if status == "failed":
        assert assistant["failure_message"] == "sandbox unavailable"
        assert assistant["failure_recoverable"] is True
    assert journal == original


def test_user_prompt_does_not_overwrite_preflight_start_or_flush_its_run():
    journal = {"agent_id": "subagent-many-turns", "events": [
        event("system", START, {"lifecycle": "turn_started"}),
        event("user_prompt", START+1000, {"content": "first task"}),
        event("assistant", START+1700, {"content": "first result"}),
        event("terminal", START+2000, {"status": "completed"}),
        event("system", START+10000, {"lifecycle": "turn_started"}),
        event("user_prompt", START+11000, {"content": "second task"}),
        event("assistant", START+13000, {"content": "second result"}),
        event("terminal", START+14000, {"status": "completed"}),
    ]}
    messages = build_subagent_transcript_messages(journal)
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["duration_ms"] == 2000
    assert messages[3]["duration_ms"] == 4000
    assert messages[1]["timestamp"] == START
    assert messages[3]["timestamp"] == START+10000
    assert messages[3]["content"] == "second result"


def test_legacy_tool_first_journal_uses_its_retained_observation_not_epoch_zero():
    journal = {"agent_id": "subagent-legacy", "events": [
        event("tool_use", START+1000, {"tool_call": {"id": "call-read", "name": "read_file", "arguments": {"file_path": "a.txt"}}}),
        event("terminal", START+2000, {"status": "cancelled"}),
    ]}
    assistant = build_subagent_transcript_messages(journal)[0]
    assert assistant["duration_ms"] == 1000
    assert assistant["timestamp"] == START+1000
    assert assistant["blocks"][0]["record"]["id"] == "call-read"


def test_terminal_elapsed_receipt_remains_authoritative():
    journal = {"events": [event("system", START, {"lifecycle": "turn_started"}),
                           event("terminal", START+5000, {"status": "failed", "duration_ms": 7})]}
    assert build_subagent_transcript_messages(journal)[0]["duration_ms"] == 7


def test_optional_git_snapshot_does_not_escalate_an_unavailable_readonly_command(monkeypatch, tmp_path):
    calls = []

    async def unavailable(argv, *, root, context, sandbox_policy, timeout):
        calls.append((argv, context.permission.sandbox_mode, sandbox_policy))
        raise SandboxUnavailableError("native sandbox account state is unavailable")

    monkeypatch.setattr("backend.tools.git_support._run_git", unavailable)
    permission = PermissionContext(mode="plan", sandbox_mode="read-only", approval_policy="never", workspace_root=tmp_path)
    policy = sandbox_policy_for_permission_context(tmp_path, permission)
    context = ToolExecutionContext(permission=permission, sandbox_policy=policy, workspace_root=tmp_path)
    captured_permission = context.permission
    note = asyncio.run(build_git_status_context_async(tmp_path, context=context))
    assert "snapshot is unavailable" in note
    assert "does not mean the workspace is clean" in note
    assert "commands remain subject" in note
    assert len(calls) == 1
    assert calls[0][1] == "read-only"
    assert calls[0][2] is policy
    assert context.permission is captured_permission


def test_unexpected_git_programming_failure_is_not_suppressed(monkeypatch, tmp_path):
    async def broken(*args, **kwargs):
        raise RuntimeError("unexpected programming error")

    monkeypatch.setattr("backend.tools.git_support._run_git", broken)
    with pytest.raises(RuntimeError, match="unexpected programming error"):
        asyncio.run(build_git_status_context_async(Path(tmp_path)))
