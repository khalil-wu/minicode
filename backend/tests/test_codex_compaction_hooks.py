from __future__ import annotations

import json
import shlex
import sys
from types import SimpleNamespace

import pytest

from backend.agent.state import AgentState
from backend.agent.token_budget_runtime import run_context_window_compaction
from backend.hooks.dispatcher import HookExecution, compile_hook_matcher
from backend.hooks.manager import (
    HookEvent,
    HookManager,
    HookResult,
    _hook_input,
    _HookEntry,
)
from backend.hooks.reducer import reduce_hook_executions
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.context_budget import manage_context_budget
from backend.tests.test_codex_context_window import (
    WindowModel,
    run_window,
    window_builder,
)
from backend.tests.test_loop_context_budget import _BudgetCtx
from backend.tests.test_manual_compaction_model_lifetime import CompactEndpoint, prepare
from backend.tools.registry import ToolRegistry
from backend.ws.handlers.conversation import handle_context_compact


def execution(stdout, *, exit_code=0, stderr=""):
    entry = _HookEntry(matcher=compile_hook_matcher("*"), raw_matcher="*", hook_type="command",
        entry_id="compact-hook", source="project", source_path="settings.json", command="unused")
    return HookExecution(entry=entry, stdout=stdout, stderr=stderr, exit_code=exit_code,
        configured_order=0, completion_order=0, duration_ms=1)


@pytest.mark.parametrize("event", [HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT])
@pytest.mark.parametrize("output,exit_code,stopped,failed", [
    ("plain stdout must not become model instructions", 0, False, False),
    ("ignored", 2, False, True),
    ('{"continue":false,"stopReason":"actual stop","systemMessage":"actual warning"}', 0, True, False),
    ('{"continue":true,"suppressOutput":true,"systemMessage":"actual warning"}', 0, False, False),
    ('{"continue":false,"stop_reason":"old invented contract"}', 0, False, True),
    ('{"additional_context":"do more work"}', 0, False, True),
    ('{"continue":"false"}', 0, False, True),
    ('{"systemMessage":123}', 0, False, True),
    ('{"continue":', 0, False, True),
])
def test_source_compact_output_schema_only_applies_real_universal_control(event, output, exit_code, stopped, failed):
    result = reduce_hook_executions(event, [execution(output, exit_code=exit_code, stderr="actual exit-2 diagnostic" if exit_code else "")],
        expected_event_name="PreCompact" if event is HookEvent.PRE_COMPACT else "PostCompact")
    assert result.prevent_continuation is stopped and result.failed is failed
    assert result.custom_instructions == result.additional_context == ""
    assert result.run_summaries[0]["status"] == ("failed" if failed else "stopped" if stopped else "succeeded")
    if stopped:
        assert result.stop_reason == "actual stop" and result.system_message == "actual warning"
    if failed:
        assert result.errors


@pytest.mark.parametrize("event,label", [(HookEvent.PRE_COMPACT, "PreCompact"), (HookEvent.POST_COMPACT, "PostCompact")])
def test_compact_stdin_uses_exact_source_names_and_nullable_transcript(event, label, tmp_path):
    payload = _hook_input(event, {"SESSION_ID": "conversation", "TURN_ID": "actual-turn", "MODEL": "actual-model",
        "COMPACT_TRIGGER": "manual", "PERMISSION_MODE": "auto", "COMPACT_CUSTOM_INSTRUCTIONS": "must not send",
        "COMPACT_SUMMARY": "must not send"}, tmp_path)
    assert payload == {"session_id": "conversation", "turn_id": "actual-turn", "model": "actual-model",
        "cwd": str(tmp_path), "transcript_path": None, "hook_event_name": label, "trigger": "manual"}


def command_hook(tmp_path, name, *, output=None, exit_code=0):
    input_file = tmp_path / f"{name}-input.json"
    script = tmp_path / f"{name}.py"
    script.write_text("import json,sys\nfrom pathlib import Path\npayload=json.load(sys.stdin)\n"
        + f"Path({str(input_file)!r}).write_text(json.dumps(payload),encoding='utf-8')\n"
        + f"print(json.dumps({output!r}))\nsys.exit({exit_code})\n", encoding="utf-8")
    if sys.platform == "win32":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = f"& {quote(sys.executable)} {quote(script)}"
        shell = "powershell"
    else:
        command = shlex.quote(sys.executable) + " " + shlex.quote(str(script))
        shell = "bash"
    entry = _HookEntry(matcher=compile_hook_matcher("auto|manual"), raw_matcher="auto|manual", hook_type="command",
        command=command, shell=shell, entry_id=name, source="project", source_path=str(script))
    return entry, input_file


@pytest.mark.asyncio
async def test_real_command_hook_input_uses_bound_model_and_operation_owner(tmp_path):
    entry, input_file = command_hook(tmp_path, "bound-pre", output={"continue": False, "stopReason": "actual stop"})
    manager = HookManager(hooks={HookEvent.PRE_COMPACT: [entry]}, workspace_root=tmp_path)
    context = ToolExecutionContext(PermissionContext(), conversation_id="owner", turn_id="captured-turn", model_execution=SimpleNamespace(model="captured-model"),
        metadata={"hook_session_id": "owner", "turn_id": "stale-turn", "model": "stale-model"})
    manager.bind_runtime(llm=None, tool_registry=ToolRegistry(), tool_context=context)
    result = await manager.run_pre_compact(trigger="manual", custom_instructions="never add this")
    captured = json.loads(input_file.read_text(encoding="utf-8"))
    assert captured["model"] == "captured-model" and captured["turn_id"] == "captured-turn"
    assert captured["session_id"] == "owner" and captured["hook_event_name"] == "PreCompact"
    assert "custom_instructions" not in captured and "event" not in captured
    assert result.prevent_continuation and result.stop_reason == "actual stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("token_budget", [False, True])
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_automatic_stop_preserves_before_or_committed_window_and_emits_actual_warning(tmp_path, token_budget, stage):
    class Hooks:
        def has_hooks(self, event): return True
        async def run_pre_compact(self, **kwargs):
            return HookResult(prevent_continuation=stage == "before", system_message="PRE_WARNING")
        async def run_post_compact(self, **kwargs):
            assert kwargs == {"trigger": "auto"}
            return HookResult(prevent_continuation=stage == "after", system_message="POST_WARNING")
    state = AgentState(user_message="existing work")
    if token_budget:
        context = window_builder(WindowModel([]), tmp_path)
        context.append_user("old window")
        context.bind_hook_manager(Hooks())
        events = [event async for event in run_context_window_compaction(context, state)]
        compacted = context._context_window_state.window_number
    else:
        context = _BudgetCtx(token_usage=900, after_compact_usage=500); context.hook_manager = Hooks()
        from backend.config import TokenBudget
        events = [event async for event in manage_context_budget(context, state, TokenBudget(total=1000), [])]
        compacted = context.compact_calls
    assert compacted == (0 if stage == "before" else 1)
    assert state.stopped_reason == "interrupted" and state.terminal_status == "cancelled"
    assert events[0].type == "system_notice" and events[0].data["content"] == "PRE_WARNING"
    if stage == "after":
        assert events[-1].type == "system_notice" and events[-1].data["content"] == "POST_WARNING"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_real_query_does_not_sample_again_after_compact_hook_stops(tmp_path, stage):
    pre, _ = command_hook(tmp_path, "query-pre", output={"continue": stage != "before", "stopReason": "before-stop" if stage == "before" else None})
    post, _ = command_hook(tmp_path, "query-post", output={"continue": stage != "after", "stopReason": "after-stop" if stage == "after" else None})
    manager = HookManager(hooks={HookEvent.PRE_COMPACT: [pre], HookEvent.POST_COMPACT: [post]}, workspace_root=tmp_path)
    from backend.llm.base import ToolCallEvent
    model = WindowModel([[ToolCallEvent(id="switch", name="new_context", arguments={})], "MUST_NOT_SAMPLE"])
    builder = window_builder(model, tmp_path)
    async def bind_owned_manager(event, context):
        if event.type == "tool_result" and event.data.get("id") == "switch":
            owned = context.hook_manager
            owned.hooks[HookEvent.PRE_COMPACT] = manager.hooks[HookEvent.PRE_COMPACT]
            owned.hooks[HookEvent.POST_COMPACT] = manager.hooks[HookEvent.POST_COMPACT]
    state, builder, _journal, events = await run_window(tmp_path, model, builder=builder, observe=bind_owned_manager)
    assert model.calls == 1 and state.stopped_reason == "interrupted" and state.terminal_status == "cancelled"
    assert builder._context_window_state.window_number == (0 if stage == "before" else 1)
    assert not any(event.type == "text_chunk" and "MUST_NOT_SAMPLE" in str(event.data) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["none", "before", "after"])
@pytest.mark.parametrize("token_budget", [False, True])
async def test_real_manual_hooks_frame_persisted_operation_and_keep_model_inputs_clean(tmp_path, monkeypatch, stage, token_budget):
    from dataclasses import replace

    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.feature_flags import FeatureFlags
    pre, pre_file = command_hook(tmp_path, "manual-pre", output={"continue": stage != "before",
        "systemMessage": "PRE_MANUAL_WARNING", "stopReason": "before-stop" if stage == "before" else None})
    post, post_file = command_hook(tmp_path, "manual-post", output={"continue": stage != "after",
        "systemMessage": "POST_MANUAL_WARNING", "stopReason": "after-stop" if stage == "after" else None})
    manager = HookManager(hooks={HookEvent.PRE_COMPACT: [pre], HookEvent.POST_COMPACT: [post]}, workspace_root=tmp_path)
    monkeypatch.setattr("backend.hooks.manager.load_hook_manager_for_workspace", lambda *_args, **_kwargs: manager)
    with CompactEndpoint() as endpoint:
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint)
        if token_budget:
            model = WindowModel([])
            config = replace(session.config, feature_flags=FeatureFlags({"token_budget": True}))
            captured = ModelExecutionSnapshot(config=config, llm=model, provider="openai", model="captured-window-model")
            monkeypatch.setattr("backend.ws.handlers.conversation._capture_manual_compaction_model", lambda *_args: (captured, tmp_path, []))
        before_live = session.context_builder.export_snapshot()
        assert await handle_context_compact(session, {"conversation_id": before.id})
        assert all(adapter._closed for adapter in adapters)
        events = [call.args[0] for call in session.send_event.await_args_list]
        after = session.conversation_repo.get_conversation(before.id)
        pre_input = json.loads(pre_file.read_text(encoding="utf-8"))
        assert pre_input["session_id"] == before.id and pre_input["turn_id"].startswith("compact-turn-")
        assert pre_input["model"] == ("captured-window-model" if token_budget else "alpha")
        assert events[0].type == "system_notice" and events[0].data["content"] == "PRE_MANUAL_WARNING"
        results = [event for event in events if event.type == "command.result"]
        assert len(results) == 1 and results[0].data["level"] == ("success" if stage == "none" else "error")
        assert "PRE_MANUAL_WARNING" not in json.dumps(after.context_snapshot) and "POST_MANUAL_WARNING" not in json.dumps(after.context_snapshot)
        if stage == "before":
            assert after.revision == before.revision and after.context_snapshot == before.context_snapshot
            assert session.context_builder.export_snapshot() == before_live
            assert not endpoint.requests and not post_file.exists()
            assert not any(event.type == "item.started" for event in events)
        else:
            assert after.revision > before.revision and after.compaction_state == "compacted"
            assert after.transcript[:len(before.transcript)] == before.transcript
            assert after.transcript[-1]["blocks"][0]["status"] == "completed"
            post_input = json.loads(post_file.read_text(encoding="utf-8"))
            assert post_input["turn_id"] == pre_input["turn_id"] and post_input["model"] == pre_input["model"]
            completed_index = next(index for index, event in enumerate(events) if event.type == "item.completed")
            post_index = next(index for index, event in enumerate(events) if event.type == "system_notice" and event.data["content"] == "POST_MANUAL_WARNING")
            assert completed_index < post_index
            if token_budget:
                assert model.calls == 0 and not endpoint.requests
                assert after.context_snapshot["context_window_state"]["window_number"] == 1
            else:
                assert len(endpoint.requests) == 1 and endpoint.requests[0][0].endswith("/compact")
        from backend.hooks.manager import _release_hook_manager_for_owner
        _release_hook_manager_for_owner(session.session_id, manager)
