from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.loop_preflight import prepare_turn_input
from backend.hooks.dispatcher import HookExecution, compile_hook_matcher
from backend.hooks.discovery import discover_hook_snapshot, snapshot_from_settings
from backend.hooks.manager import HookEvent, HookManager, _HookEntry, _hook_input
from backend.hooks.reducer import reduce_hook_executions
from backend.permissions.context import PermissionContext, ToolExecutionContext


def _command(script: Path) -> tuple[str, str]:
    if sys.platform == "win32":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        return f"& {quote(sys.executable)} {quote(script)}", "powershell"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", "bash"


@pytest.mark.parametrize(
    ("event", "extra_keys"),
    [
        (HookEvent.SESSION_START, {"model", "permission_mode", "source"}),
        (HookEvent.SESSION_END, {"reason"}),
        (HookEvent.SUBAGENT_START, {"turn_id", "model", "permission_mode", "agent_id", "agent_type"}),
        (HookEvent.SUBAGENT_STOP, {"turn_id", "model", "permission_mode", "agent_id", "agent_type", "stop_hook_active", "agent_transcript_path", "last_assistant_message"}),
        (HookEvent.USER_PROMPT_SUBMIT, {"turn_id", "model", "permission_mode", "prompt"}),
        (HookEvent.PRE_TOOL_USE, {"turn_id", "model", "permission_mode", "tool_name", "tool_input", "tool_use_id"}),
        (HookEvent.POST_TOOL_USE, {"turn_id", "model", "permission_mode", "tool_name", "tool_input", "tool_use_id", "tool_response"}),
        (HookEvent.PERMISSION_REQUEST, {"turn_id", "model", "permission_mode", "tool_name", "tool_input"}),
        (HookEvent.PRE_COMPACT, {"turn_id", "model", "trigger"}),
        (HookEvent.POST_COMPACT, {"turn_id", "model", "trigger"}),
        (HookEvent.STOP, {"turn_id", "model", "permission_mode", "stop_hook_active", "last_assistant_message"}),
        (HookEvent.INTERRUPT, {"turn_id", "model", "permission_mode"}),
    ],
)
def test_codex_event_input_shapes(event, extra_keys, tmp_path):
    fields = {
        "SESSION_ID": "thread", "TURN_ID": "turn", "MODEL": "model",
        "PERMISSION_MODE": "default", "TOOL_NAME": "read_file", "TOOL_CALL_ID": "call",
        "TOOL_ARGS_JSON": '{"path":"a"}', "TOOL_RESULT": '"42"',
        "USER_PROMPT": "hello", "SESSION_SOURCE": "startup", "SESSION_END_REASON": "other",
        "SUBAGENT_ID": "child", "SUBAGENT_AGENT_TYPE": "worker", "STOP_HOOK_ACTIVE": "true",
    }
    payload = _hook_input(event, fields, tmp_path)
    assert set(payload) == {"session_id", "transcript_path", "cwd", "hook_event_name"} | extra_keys
    assert payload["hook_event_name"] == "".join(part.title() for part in event.value.split("_"))
    assert payload["session_id"] == "thread" and payload["transcript_path"] is None
    if event == HookEvent.POST_TOOL_USE:
        assert payload["tool_response"] == "42"
    if event == HookEvent.SUBAGENT_STOP:
        assert payload["stop_hook_active"] is True


@pytest.mark.asyncio
async def test_codex_hooks_run_with_current_turn_and_separate_rewrite_from_approval(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "pathlib.Path(__file__).with_name(payload['hook_event_name'] + '.json').write_text(json.dumps(payload), encoding='utf-8')\n"
        "if payload['hook_event_name'] == 'PreToolUse':\n"
        "    output = {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow', 'updatedInput': {'path': 'changed'}, 'additionalContext': 'check first'}}\n"
        "else:\n"
        "    output = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}\n"
        "print(json.dumps(output))\n",
        encoding="utf-8",
    )
    command, shell = _command(script)
    handler = {"type": "command", "command": command, "shell": shell}
    manager = HookManager.from_settings({"hooks": {
        "PreToolUse": [{"matcher": "read_file", "hooks": [handler]}],
        "PermissionRequest": [{"matcher": "read_file", "hooks": [handler]}],
    }}, workspace_root=tmp_path)
    context = ToolExecutionContext(
        permission=PermissionContext(approval_policy="never"), session_id="window",
        conversation_id="thread", turn_id="turn", workspace_root=tmp_path,
        model_execution=SimpleNamespace(model="current-model"),
        metadata={"hook_session_id": "thread", "agent_id": "child", "agent_type": "worker"},
    )
    manager.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    pre = await manager.run_pre_tool("read_file", {"path": "original"}, tool_call_id="call", session_id="window")
    approval = await manager.run_permission_request("read_file", pre.updated_input, tool_call_id="call", session_id="window")
    assert pre.updated_input == {"path": "changed"}
    assert pre.additional_context == "check first" and pre.permission_decision == "" and not pre.blocked
    assert approval.permission_decision == "allow" and not approval.blocked
    pre_input = json.loads((tmp_path / "PreToolUse.json").read_text(encoding="utf-8"))
    request_input = json.loads((tmp_path / "PermissionRequest.json").read_text(encoding="utf-8"))
    assert (pre_input["session_id"], pre_input["turn_id"], pre_input["model"], pre_input["permission_mode"]) == ("thread", "turn", "current-model", "bypassPermissions")
    assert (pre_input["agent_id"], pre_input["agent_type"], pre_input["tool_use_id"]) == ("child", "worker", "call")
    assert request_input["tool_input"] == {"path": "changed"} and "tool_use_id" not in request_input


def _execution(payload, *, code=0, stderr="", order=0):
    entry = _HookEntry(matcher=compile_hook_matcher("*"), entry_id=str(order), source="project", source_path="hooks.json")
    return HookExecution(entry, json.dumps(payload), stderr, code, order, order, 1)


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        (HookEvent.PRE_TOOL_USE, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}),
        (HookEvent.PRE_TOOL_USE, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}),
        (HookEvent.PRE_TOOL_USE, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"path": "changed"}}}),
        (HookEvent.PRE_TOOL_USE, {"decision": "approve"}),
        (HookEvent.PERMISSION_REQUEST, {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedInput": {}}}}),
        (HookEvent.POST_TOOL_USE, {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedMCPToolOutput": "changed"}}),
        (HookEvent.USER_PROMPT_SUBMIT, {"hookSpecificOutput": {"additionalContext": "missing event"}}),
        (HookEvent.STOP, {"decision": "block", "reason": ""}),
    ],
)
def test_invalid_codex_outputs_are_reported_without_control_effects(event, payload):
    result = reduce_hook_executions(event, [_execution(payload)], expected_event_name=event.value)
    assert result.failed and result.errors and result.run_summaries[0]["status"] == "failed"
    assert not result.blocked and not result.prevent_continuation
    assert result.permission_decision == "" and result.updated_input is None and result.additional_context == ""


def test_pre_tool_latest_completed_rewrite_and_block_precedence():
    first = _execution({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"path": "first"}}}, order=1)
    second = _execution({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"path": "second"}}}, order=0)
    result = reduce_hook_executions(HookEvent.PRE_TOOL_USE, [first, second], expected_event_name="pre_tool_use")
    assert result.updated_input == {"path": "first"} and result.permission_decision == ""
    deny = _execution({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "policy"}}, order=2)
    result = reduce_hook_executions(HookEvent.PRE_TOOL_USE, [first, deny], expected_event_name="pre_tool_use")
    assert result.blocked and result.message == "policy" and result.updated_input is None


def test_exit_two_requires_stderr_and_permission_deny_wins():
    empty = reduce_hook_executions(HookEvent.PRE_TOOL_USE, [_execution({}, code=2)], expected_event_name="pre_tool_use")
    assert empty.failed and not empty.blocked and "stderr" in empty.errors[0]
    allow = _execution({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})
    deny = _execution({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "no"}}}, order=1)
    result = reduce_hook_executions(HookEvent.PERMISSION_REQUEST, [deny, allow], expected_event_name="permission_request")
    assert result.blocked and result.permission_decision == "deny" and result.message == "no"


def test_non_json_number_cannot_be_used_as_hook_rewrite():
    execution = _execution({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"value": float("nan")}}})
    result = reduce_hook_executions(HookEvent.PRE_TOOL_USE, [execution], expected_event_name="pre_tool_use")
    assert result.failed and result.updated_input is None and not result.blocked


@pytest.mark.asyncio
async def test_session_start_stop_prevents_prompt_hook_and_input_admission(tmp_path):
    script = tmp_path / "stop.py"
    script.write_text("import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'continue': False, 'stopReason': 'pause'}))\n", encoding="utf-8")
    command, shell = _command(script)
    manager = HookManager.from_settings({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": command, "shell": shell}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "shell": shell}]}],
    }}, workspace_root=tmp_path)
    scheduled = []
    result = await prepare_turn_input(
        "hello", state=SimpleNamespace(user_message="hello", prompt_context={}),
        turn_kernel=SimpleNamespace(schedule_user_input=scheduled.append), session_id=str(tmp_path),
        deadline=None, cancel_event=None, hook_manager=manager,
    )
    assert result.blocked and result.block_message == "pause"
    assert result.prompt_hook_result is None and scheduled == []


def test_codex_discovery_rejects_unsupported_handlers_and_keeps_declared_duplicates():
    snapshot = snapshot_from_settings({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "echo yes", "additionalContextLimit": 0},
        {"type": "command", "command": "echo yes", "additionalContextLimit": 0},
        {"type": "prompt", "prompt": "not supported"},
        {"type": "agent", "prompt": "not supported"},
        {"type": "http", "url": "https://example.com"},
    ]}]}}, workspace_root=None)
    assert len(snapshot.entries) == 2
    assert all(entry.additional_context_limit == 0 for entry in snapshot.entries)
    assert len(snapshot.warnings) == 3


@pytest.mark.asyncio
async def test_layered_discovery_runs_each_duplicate_hook(tmp_path, monkeypatch):
    from backend.hooks.runners import HookExecutionResult

    handlers = [{"type": "command", "command": "same"}] * 2
    layer = SimpleNamespace(source=SimpleNamespace(kind="user", file=str(tmp_path / "config.toml")),
                            config={"hooks": {"PreToolUse": [{"hooks": handlers}]}})
    requirements = SimpleNamespace(
        disable_all_hooks=None, allow_managed_hooks_only=None, allowed_http_hook_urls=None,
        http_hook_allowed_env_vars=None, restricts_customization_to_plugins=lambda _: False,
        value_for=lambda _: None, source_for=lambda _: None,
    )
    stack = SimpleNamespace(requirements=requirements, get_layers=lambda: [layer], effective_config=lambda: {}, fingerprint="fixture")
    snapshot = discover_hook_snapshot(config_stack=stack, workspace_root=tmp_path, workspace_trusted=True, plugin_sources=())
    manager = HookManager.from_snapshot(snapshot, workspace_root=tmp_path)
    calls = []

    async def execute(self, entry, event, env_extras, **kwargs):
        calls.append(entry.entry_id)
        return HookExecutionResult("", "", 0)

    monkeypatch.setattr(HookManager, "_execute_entry", execute)
    result = await manager.run_pre_tool("read_file", {})
    assert len(calls) == len(set(calls)) == 2
    assert len(result.run_summaries) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_long_hook_context_is_preserved_and_readable_by_its_owner(tmp_path, monkeypatch, asynchronous):
    import backend.agent.tool_result_persistence as storage

    monkeypatch.setattr(storage, "TOOL_RESULT_DATA_DIR", tmp_path / "outputs")
    original = "开头" + "中间内容" * 100 + "结尾"
    script = tmp_path / "context.py"
    script.write_text(
        "import json, sys\njson.load(sys.stdin)\n"
        f"print(json.dumps({{'hookSpecificOutput': {{'hookEventName': 'UserPromptSubmit', 'additionalContext': {original!r}}}}}))\n",
        encoding="utf-8",
    )
    command, shell = _command(script)
    manager = HookManager.from_settings({"hooks": {"UserPromptSubmit": [{"hooks": [{
        "type": "command", "command": command, "shell": shell,
        "additionalContextLimit": 100, "async": asynchronous,
    }]}]}}, workspace_root=tmp_path)
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="thread", workspace_root=tmp_path)
    manager.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    result = await manager.run_user_prompt_submit("hello")
    if asynchronous:
        await manager.drain_async_hooks()
        preview, = manager.take_async_context()
    else:
        preview = result.additional_context
    assert "tokens truncated" in preview and "开头" in preview and "结尾" in preview
    saved_path = Path(preview.split("Full hook output saved to: ", 1)[1])
    assert saved_path.read_text(encoding="utf-8") == original
    assert storage.is_tool_result_path(saved_path, conversation_id="thread", workspace_root=tmp_path)
    assert not storage.is_tool_result_path(saved_path, conversation_id="other", workspace_root=tmp_path)


@pytest.mark.asyncio
async def test_spilled_plain_stdout_keeps_its_complete_semantic_content(tmp_path, monkeypatch):
    import backend.hooks.task_output as output

    monkeypatch.setattr(output, "TOOL_RESULT_DATA_DIR", tmp_path)
    capture = output.HookTaskOutput(scope_id="thread", task_id="hook", max_memory_bytes=4)
    await capture.write_stdout(b"first ")
    await capture.write_stderr(b"diagnostic")
    await capture.write_stdout(b"last")
    await capture.finish()
    assert capture.spilled_to_disk
    assert await capture.semantic_stdout_text() == "first last"


@pytest.mark.asyncio
async def test_session_end_runs_after_its_last_turn_was_cancelled(tmp_path):
    script = tmp_path / "end.py"
    script.write_text("import pathlib, sys\nsys.stdin.readline()\npathlib.Path(__file__).with_suffix('.done').write_text('done')\n", encoding="utf-8")
    command, shell = _command(script)
    manager = HookManager.from_settings({"hooks": {"SessionEnd": [{"hooks": [{
        "type": "command", "command": command, "shell": shell, "timeout": 3,
    }]}]}}, workspace_root=tmp_path)
    cancelled = asyncio.Event()
    cancelled.set()
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="thread", workspace_root=tmp_path,
                                   cancel_event=cancelled, deadline_monotonic=0)
    manager.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    result = await manager.run_session_end(session_id="thread")
    assert script.with_suffix('.done').read_text() == "done"
    assert not result.failed and result.run_summaries[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_closing_child_session_reaps_its_hooks_without_cancelling_parent(tmp_path):
    import psutil

    from backend.agent.query_engine import AgentSession
    from backend.config import AgentSettings, TokenBudget
    from backend.tools.registry import ToolRegistry

    script = tmp_path / "owned_hook.py"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "pathlib.Path(__file__).with_name(request['session_id'] + '.pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command, shell = _command(script)
    entry = _HookEntry(matcher=compile_hook_matcher("*"), entry_id="owned", command=command, shell=shell, run_async=True)
    parent = HookManager(hooks={HookEvent.USER_PROMPT_SUBMIT: [entry]}, workspace_root=tmp_path, scope_id="parent")
    child = parent.fork_for_subagent(workspace_root=tmp_path, session_id="child")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)
    parent.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    child.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    child_session = AgentSession(llm=None, tool_registry=ToolRegistry(), artifact_store=None,
                                 permission_checker=None, agent_settings=AgentSettings(), token_budget=TokenBudget(),
                                 hook_manager=child)
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "child.pid"
    try:
        await parent.run_user_prompt_submit("parent")
        await child.run_user_prompt_submit("child")

        async def wait_for_children():
            while not parent_pid.exists() or not child_pid.exists():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_for_children(), timeout=10)
        parent_process, child_process = int(parent_pid.read_text()), int(child_pid.read_text())
        await child_session.aclose()
        assert not psutil.pid_exists(child_process) and child.pending_async_hooks == 0
        assert psutil.pid_exists(parent_process) and parent.pending_async_hooks == 1
    finally:
        await child_session.aclose()
        await parent.finalize_async_hooks()
