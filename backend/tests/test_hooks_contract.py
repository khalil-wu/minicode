"""Hooks: JSON stdout contract + new events (post_tool_use_failure, post_compact)."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.hooks.dispatcher import HookExecution, compile_hook_matcher
from backend.hooks.manager import (
    HookEvent,
    HookManager,
    HookResult,
    _HookEntry,
    _parse_json_stdout,
)
from backend.hooks.reducer import reduce_hook_executions
from backend.hooks.runners import (
    HookExecutionError,
    HookExecutionResult,
    _dynamic_async_timeout_seconds,
    _entry_timeout_seconds,
)


def test_parse_json_stdout_object():
    assert _parse_json_stdout('{"decision": "block", "feedback": "nope"}') == {
        "decision": "block",
        "feedback": "nope",
    }


def test_parse_json_stdout_rejects_non_json():
    assert _parse_json_stdout("plain text output") is None
    assert _parse_json_stdout("") is None
    assert _parse_json_stdout("42") is None  # bare number, not an object


def test_parse_json_stdout_tolerates_whitespace():
    assert _parse_json_stdout('  \n  {"feedback": "x"}  \n') == {"feedback": "x"}


def test_hook_manager_has_one_dispatch_and_execution_pipeline():
    assert callable(HookManager._run_event)
    assert callable(HookManager._execute_entry)
    for legacy_name in (
        "_run_event_legacy",
        "_exec_prompt_hook",
        "_exec_agent_hook",
        "_exec",
    ):
        assert not hasattr(HookManager, legacy_name)


def test_hook_result_additional_context_property():
    assert HookResult(additional_context="ctx").has_additional_context is True
    assert HookResult().has_additional_context is False
    assert HookResult(additional_context="   ").has_additional_context is False


def test_hook_result_updated_input_property():
    assert HookResult(updated_input="rewrite this").has_updated_input is True
    assert HookResult().has_updated_input is False
    assert HookResult(updated_input="   ").has_updated_input is False


def test_new_hook_events_exist():
    # The new events must be registered so has_hooks/run_* can target them.
    assert HookEvent.POST_TOOL_USE_FAILURE.value == "post_tool_use_failure"
    assert HookEvent.NOTIFICATION.value == "notification"
    assert HookEvent.POST_COMPACT.value == "post_compact"
    assert HookEvent.PERMISSION_REQUEST.value == "permission_request"
    assert HookEvent.PERMISSION_DENIED.value == "permission_denied"
    assert HookEvent.STOP_FAILURE.value == "stop_failure"
    assert HookEvent.SUBAGENT_START.value == "subagent_start"
    assert HookEvent.SUBAGENT_STOP.value == "subagent_stop"
    assert HookEvent.TEAMMATE_IDLE.value == "teammate_idle"
    assert HookEvent.TASK_CREATED.value == "task_created"
    assert HookEvent.TASK_COMPLETED.value == "task_completed"
    assert HookEvent.ELICITATION.value == "elicitation"
    assert HookEvent.ELICITATION_RESULT.value == "elicitation_result"
    assert HookEvent.CONFIG_CHANGE.value == "config_change"
    assert HookEvent.WORKTREE_CREATE.value == "worktree_create"
    assert HookEvent.WORKTREE_REMOVE.value == "worktree_remove"
    assert HookEvent.INSTRUCTIONS_LOADED.value == "instructions_loaded"
    assert HookEvent.CWD_CHANGED.value == "cwd_changed"
    assert HookEvent.FILE_CHANGED.value == "file_changed"
    assert HookEvent.SESSION_END.value == "session_end"


def test_discovery_event_registry_is_the_model_event_registry():
    from backend.hooks.discovery import _EVENT_KEYS
    from backend.hooks.models import HookEvent as ModelHookEvent

    assert _EVENT_KEYS == {event.value for event in ModelHookEvent}


def test_event_mismatch_cannot_apply_hook_decisions():
    from backend.hooks.dispatcher import HookExecution
    from backend.hooks.reducer import reduce_hook_executions

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "mismatch",
            "source": "project",
            "source_path": "hooks.json",
            "display_order": 0,
            "configured_order": 0,
            "completion_order": 0,
            "duration_ms": 1,
            "status_message": "",
            "additional_context_limit": None,
        },
    )()
    reduction = reduce_hook_executions(
        HookEvent.PRE_TOOL_USE,
        [
            HookExecution(
                entry=entry,
                stdout=(
                    '{"event":"post_tool_use","permission_decision":"deny",'
                    '"permission_decision_reason":"wrong event",'
                    '"updated_input":{"blocked":true},'
                    '"additional_context":"wrong context","feedback":"wrong"}'
                ),
                stderr="",
                exit_code=2,
                configured_order=0,
                completion_order=0,
                duration_ms=1,
            )
        ],
        expected_event_name="pre_tool_use",
    )
    assert reduction.blocked is False
    assert reduction.permission_decision == ""
    assert reduction.updated_input is None
    assert reduction.additional_context == ""
    assert reduction.feedback == ""


def _runtime_entry(**overrides: object) -> _HookEntry:
    values: dict[str, object] = {
        "matcher": compile_hook_matcher("*"),
        "raw_matcher": "*",
        "hook_type": "command",
        "entry_id": "hook-1",
        "source": "project",
        "source_path": "hooks.json",
        "command": "unused",
    }
    values.update(overrides)
    return _HookEntry(**values)


def _python_hook_command(script: Path) -> tuple[str, str]:
    if sys.platform == "win32":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        return f"& {quote(sys.executable)} {quote(script)}", "powershell"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", "bash"


def _hook_tool_context(events: list[tuple[str, dict[str, object]]]) -> SimpleNamespace:
    async def emit_event(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    return SimpleNamespace(
        emit_event=emit_event,
        metadata={"conversation_id": "conversation-1"},
        conversation_id="conversation-1",
        session_id="session-1",
        cancel_event=None,
    )


def test_condition_matcher_is_prepared_once_for_preview_and_execution(monkeypatch) -> None:
    matching = _runtime_entry(
        entry_id="matching",
        raw_matcher="run_command",
        matcher=compile_hook_matcher("run_command"),
        condition="run_command(git status:*)",
    )
    skipped = _runtime_entry(
        entry_id="skipped",
        raw_matcher="run_command",
        matcher=compile_hook_matcher("run_command"),
        condition="run_command(npm install:*)",
    )
    manager = HookManager(hooks={HookEvent.PRE_TOOL_USE: [matching, skipped]})
    registry_calls: list[str] = []
    validation_calls: list[dict[str, object]] = []

    class Registry:
        def get_tool(self, name: str) -> object:
            registry_calls.append(name)
            return object()

    def validate(_tool: object, arguments: dict[str, object]) -> str:
        validation_calls.append(dict(arguments))
        return ""

    executed: list[str] = []

    async def execute_entry(
        self: HookManager,
        entry: _HookEntry,
        event: HookEvent,
        env_extras: dict[str, str] | None,
        **_kwargs: object,
    ) -> HookExecutionResult:
        del self, event, env_extras
        executed.append(entry.entry_id)
        return HookExecutionResult('{"additional_context":"matched"}', "", 0)

    monkeypatch.setattr("backend.tools.base.validate_tool_input", validate)
    monkeypatch.setattr(HookManager, "_execute_entry", execute_entry)
    manager.bind_runtime(llm=None, tool_registry=Registry(), tool_context=None)
    fields = {
        "TOOL_NAME": "run_command",
        "TOOL_ARGS_JSON": json.dumps({"command": "git status --short"}),
    }

    preview = manager.preview(
        HookEvent.PRE_TOOL_USE,
        match_target="run_command",
        env_extras=fields,
    )
    result = asyncio.run(
        manager.run_pre_tool("run_command", {"command": "git status --short"})
    )

    assert [item["key"] for item in preview] == ["matching"]
    assert executed == ["matching"]
    assert result.additional_context == "matched"
    assert registry_calls == ["run_command", "run_command"]
    assert validation_calls == [
        {"command": "git status --short"},
        {"command": "git status --short"},
    ]


def test_dynamic_async_handshake_is_session_owned_and_suppressed(tmp_path) -> None:
    script = tmp_path / "dynamic_hook.py"
    script.write_text(
        "\n".join(
            (
                "import json, sys, time",
                "sys.stdin.readline()",
                'print(json.dumps({"async": True, "asyncTimeout": 1000}), flush=True)',
                "time.sleep(0.15)",
                'print(json.dumps({"additional_context": "async context", "suppress_output": True}), flush=True)',
            )
        ),
        encoding="utf-8",
    )
    command, shell = _python_hook_command(script)
    entry = _runtime_entry(command=command, shell=shell, async_timeout=2.0)
    manager = HookManager(
        hooks={HookEvent.USER_PROMPT_SUBMIT: [entry]},
        workspace_root=tmp_path,
    )
    events: list[tuple[str, dict[str, object]]] = []
    manager.bind_runtime(
        llm=None,
        tool_registry=SimpleNamespace(),
        tool_context=_hook_tool_context(events),
    )

    async def scenario() -> None:
        result = await manager.run_user_prompt_submit("hello")
        assert result.run_summaries[0]["status"] == "backgrounded"
        assert manager.pending_async_hooks == 1
        await manager.drain_async_hooks()

    asyncio.run(scenario())

    assert manager.pending_async_hooks == 0
    assert manager.take_async_context() == ("async context",)
    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == "agent.item"
    assert payload["kind"] == "hook_response"
    assert payload["status"] == "completed"
    assert "asyncTimeout" not in str(payload.get("content", ""))
    assert "async context" not in str(payload.get("content", ""))


def test_config_async_hook_is_session_owned_and_drained(tmp_path) -> None:
    script = tmp_path / "configured_async_hook.py"
    script.write_text(
        "\n".join(
            (
                "import json, sys, time",
                "sys.stdin.readline()",
                "time.sleep(0.15)",
                'print(json.dumps({"additional_context": "configured context"}), flush=True)',
            )
        ),
        encoding="utf-8",
    )
    command, shell = _python_hook_command(script)
    # Generous timeout: under full-suite load Windows process spawn plus the
    # scripted 0.15s sleep can approach a tighter budget, which would flake
    # the drain assertion below. The contract under test is session ownership
    # and context capture, not timeout enforcement (see
    # test_dynamic_async_timeout_terminates_owned_process).
    entry = _runtime_entry(command=command, shell=shell, run_async=True, async_timeout=10.0)
    manager = HookManager(
        hooks={HookEvent.USER_PROMPT_SUBMIT: [entry]},
        workspace_root=tmp_path,
    )
    manager.bind_runtime(
        llm=None,
        tool_registry=SimpleNamespace(),
        tool_context=_hook_tool_context([]),
    )

    async def scenario() -> None:
        result = await manager.run_user_prompt_submit("hello")
        assert result.run_summaries == ()
        assert manager.pending_async_hooks == 1
        await manager.drain_async_hooks()

    asyncio.run(scenario())

    assert manager.pending_async_hooks == 0
    assert manager.take_async_context() == ("configured context",)


def test_dynamic_async_timeout_terminates_owned_process(tmp_path) -> None:
    script = tmp_path / "timed_async_hook.py"
    script.write_text(
        "\n".join(
            (
                "import json, sys, time",
                "sys.stdin.readline()",
                'print(json.dumps({"async": True, "asyncTimeout": 50}), flush=True)',
                "time.sleep(2)",
            )
        ),
        encoding="utf-8",
    )
    command, shell = _python_hook_command(script)
    entry = _runtime_entry(command=command, shell=shell, async_timeout=2.0)
    events: list[tuple[str, dict[str, object]]] = []
    manager = HookManager(
        hooks={HookEvent.USER_PROMPT_SUBMIT: [entry]},
        workspace_root=tmp_path,
    )
    manager.bind_runtime(
        llm=None,
        tool_registry=SimpleNamespace(),
        tool_context=_hook_tool_context(events),
    )

    async def scenario() -> float:
        await manager.run_user_prompt_submit("hello")
        started = time.monotonic()
        await manager.drain_async_hooks()
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())

    assert elapsed < 1.5
    assert manager.pending_async_hooks == 0
    assert len(events) == 1
    assert events[0][1]["status"] == "failed"
    assert "timed out after 0.05s" in str(events[0][1].get("content", ""))


def test_hook_timeout_sources_remain_distinct() -> None:
    manager = HookManager.from_settings(
        {
            "hooks": {
                "session_end": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": "first"},
                            {"type": "command", "command": "second", "timeout": 9},
                        ],
                    }
                ]
            }
        }
    )
    defaulted, explicit = manager.hooks[HookEvent.SESSION_END]

    assert defaulted.async_timeout is None
    assert _entry_timeout_seconds(defaulted, HookEvent.SESSION_END) == 1.0
    assert explicit.async_timeout == 3.0
    assert _entry_timeout_seconds(explicit, HookEvent.SESSION_END) == 3.0
    assert _dynamic_async_timeout_seconds({"async": True}) == 15.0
    assert _dynamic_async_timeout_seconds({"async": True, "asyncTimeout": 250}) == 0.25
    with pytest.raises(HookExecutionError, match="asyncTimeout must be a positive number"):
        _dynamic_async_timeout_seconds({"async": True, "asyncTimeout": 0})


def test_suppress_output_keeps_structured_decision_and_context() -> None:
    execution = HookExecution(
        entry=_runtime_entry(),
        stdout=json.dumps(
            {
                "permission_decision": "deny",
                "permission_decision_reason": "blocked",
                "additional_context": "keep this context",
                "suppress_output": True,
            }
        ),
        stderr="",
        exit_code=0,
        configured_order=0,
        completion_order=0,
        duration_ms=1,
    )

    reduction = reduce_hook_executions(
        HookEvent.PRE_TOOL_USE,
        [execution],
        expected_event_name="pre_tool_use",
    )

    assert reduction.stdout == ""
    assert reduction.blocked is True
    assert reduction.permission_decision == "deny"
    assert reduction.permission_decision_reason == "blocked"
    assert reduction.additional_context == "keep this context"
