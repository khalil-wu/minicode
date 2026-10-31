from __future__ import annotations

import asyncio
import base64
import json
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from backend.agent.codex_prompts import codex_side_chat_instructions
from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.context_window import ContextWindowConfig, ContextWindowState, NEW_CONTEXT_MESSAGE
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.checkpoint import save_checkpoint, load_latest_checkpoint
from backend.agent.loop_session import AgentLoopSessionContext, populate_prompt_context
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.token_budget_runtime import run_context_window_compaction, settle_context_window
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.feature_flags import FeatureFlags, load_feature_flags
from backend.hooks.manager import HookEvent, HookManager, HookResult
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.llm.capabilities import ProviderCapabilities
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.base import ToolResult
from backend.tools.command_tool import RunCommandTool
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.context_window_tools import GetContextRemainingTool, NewContextTool
from backend.tools.file_tools import EditFileTool, ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ToolsetPolicy


class WindowModel(LLMAdapter):
    def __init__(self, steps, *, window=100_000, streamed=True, usage=UsageInfo(2_000, 500)):
        self.steps = steps
        self.streamed = streamed
        self.usage = usage
        self.capabilities = ProviderCapabilities(
            provider="local-fixture", model="fixture", streaming=True, tool_calling=True,
            context_window=window, context_window_source="declared-fixture", context_window_verified=bool(window),
            max_output_tokens=2_000, vision=True,
        )
        self.calls = 0
        self.inputs = []
        self.schemas = []

    async def simple_chat(self, messages, *, max_tokens=None):
        raise AssertionError("TokenBudget must not call a summarizer")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.inputs.append(deepcopy(messages))
        self.schemas.append(deepcopy(tools or []))
        step = self.steps[self.calls]
        self.calls += 1
        if isinstance(step, str):
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content=step, phase="final_answer")
        else:
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=step, tool_calls_committed=self.streamed)
        yield StreamEvent(StreamEventType.DONE, finish_reason="stop" if isinstance(step, str) else "tool_calls", usage=self.usage)


def window_builder(model, tmp_path, config=ContextWindowConfig()):
    builder = ContextBuilder(llm=model, conversation_id="window-conv", workspace_root=tmp_path)
    builder.configure_context_window(config)
    return builder


async def run_window(tmp_path, model, *, config=ContextWindowConfig(), builder=None,
                     extra_tools=(), code_only=False, metadata=None, state=None, observe=None, approval=None):
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    registry = ToolRegistry()
    for tool in (NewContextTool(config), GetContextRemainingTool(), ToolExecTool(), ToolWaitTool(), *extra_tools):
        registry.register(tool)
    budget = TokenBudget(total=200_000, response_reserve=4_096)
    builder = builder or ContextBuilder(llm=model, conversation_id="window-conv", workspace_root=tmp_path, token_budget=budget)
    session = AgentSession(llm=model, tool_registry=registry, artifact_store=artifacts,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(max_iterations=12, max_turn_seconds=20, stream_max_attempts=0, code_mode_only=code_only),
        token_budget=budget, context_builder=builder, approval_handler=approval)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    journal = ExecutionJournal("window-test", base_dir=tmp_path / "journals")
    owner = RunContext(agent_runtime=runtime, execution_journal=journal)
    state = state or AgentState(user_message="OLD_WINDOW_USER_REQUEST", conversation_id="window-conv", workspace_root=tmp_path)
    events = []
    try:
        async with asyncio.timeout(20), aclosing(QueryEngine().submit(QuerySubmission(
            user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="window-session", workspace_root=tmp_path,
                permission_context=PermissionContext(mode="bypass"), run_context=owner, metadata=metadata or {}),
        ))) as stream:
            async for event in stream:
                events.append(event)
                if observe is not None:
                    await observe(event, builder)
        return state, builder, journal, events
    finally:
        await session.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)


def test_context_tools_have_source_names_schema_and_exposure():
    new, remaining = NewContextTool(), GetContextRemainingTool()
    assert new.name == "new_context"
    assert new.get_spec().exposure == "direct_model_only"
    assert remaining.get_spec().exposure == "core"
    assert remaining.get_schema().output_schema["required"] == ["tokens_left"]
    for tool in (new, remaining):
        assert tool.get_schema().parameters == {"type": "object", "properties": {}, "additionalProperties": False}
        assert tool.get_schema().strict is False
        assert tool.read_only
    registry = ToolRegistry()
    registry.register(new)
    registry.register(remaining)
    policy = ToolsetPolicy(code_mode_only=True)
    direct = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)}
    nested = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=replace(policy, code_mode_only=False, nested_surface=True))}
    assert direct == {"new_context"}
    assert nested == {"get_context_remaining"}


@pytest.mark.parametrize("enabled", [False, True])
def test_real_registry_respects_token_budget_flag(tmp_path, enabled):
    store = ArtifactStore(storage_dir=tmp_path / "factory-artifacts")
    try:
        registry = build_tool_registry(store, workspace_root=tmp_path,
            config=AppConfig(llm=LLMSettings(api_key=""), feature_flags=FeatureFlags({"token_budget": enabled})), mcp_manager=None)
        assert (registry.get_tool("new_context") is not None) is enabled
        assert (registry.get_tool("get_context_remaining") is not None) is enabled
        assert registry.get_tool("new_context_window") is None
    finally:
        store.shutdown()


def test_flag_table_and_managed_disable_are_real_configuration():
    settings = {"features": {"token_budget": {"enabled": True, "reminder_threshold_tokens": 600}}}
    assert load_feature_flags(settings, managed_requirements={}).enabled("token_budget")
    assert not load_feature_flags(settings, managed_requirements={"token_budget": False}).enabled("token_budget")
    assert ContextWindowConfig.from_settings(settings).reminder_threshold_tokens == 600
    assert not FeatureFlags().enabled("token_budget")


@pytest.mark.parametrize("scope,auto_limit,expected", [("total", None, 6_500), ("body_after_prefix", 7_000, 6_500)])
def test_remaining_counts_completed_input_plus_output_and_source_scope(tmp_path, scope, auto_limit, expected):
    model = WindowModel([], window=10_000)
    builder = window_builder(model, tmp_path, ContextWindowConfig(model_auto_compact_token_limit_scope=scope, model_auto_compact_token_limit=auto_limit))
    builder.append_assistant("prior answer")
    builder.record_actual_usage(UsageInfo(2_000, 500))
    assert builder.context_window_status()["tokens_left"] == expected
    # The application budget/reserve is not this Source number.
    builder.bind_budget(TokenBudget(total=1_000_000, response_reserve=500_000))
    assert builder.context_window_status()["tokens_left"] == expected


def test_remaining_counts_local_items_after_last_model_and_clamps_at_hard_limit(tmp_path):
    builder = window_builder(WindowModel([], window=10_000), tmp_path, ContextWindowConfig(model_auto_compact_token_limit_scope="body_after_prefix", model_auto_compact_token_limit=50_000))
    builder.append_assistant("prior response")
    builder.record_actual_usage(UsageInfo(2_000, 500))
    builder.append_tool_result("local", "get_context_remaining", ToolResult("中文 proof"))
    local_tokens = (len("中文 proof".encode("utf-8")) + len("local") + 3) // 4
    assert builder.context_window_status()["tokens_left"] == 9_500 - 2_500 - local_tokens
    builder.record_actual_usage(UsageInfo(9_000, 500))
    assert builder.context_window_status()["tokens_left"] == 0
    assert builder.context_window_status()["token_limit_reached"]


def test_missing_limit_is_unknown(tmp_path):
    model = WindowModel([], window=0)
    builder = window_builder(model, tmp_path)
    builder.append_assistant("old response")
    builder.record_actual_usage(UsageInfo(2_000, 500))
    assert builder.context_window_status()["tokens_left"] is None
    assert not any(message.content.startswith("<context_window>") for message in builder._render_prompt_messages(AgentState(user_message="", workspace_root=tmp_path), tmp_path))


def test_source_fallback_window_drives_context_budget_without_claiming_provider_capacity(tmp_path):
    model = WindowModel([], window=272_000)
    model.capabilities = replace(model.capabilities, context_window_source="fallback", context_window_verified=False)
    builder = window_builder(model, tmp_path)
    builder.append_assistant("old response")
    builder.record_actual_usage(UsageInfo(2_000, 500))
    assert builder.context_window_status()["tokens_left"] == 242_300


def test_explicit_context_window_is_bounded_by_the_selected_model_maximum():
    model = WindowModel([], window=272_000)
    model.capabilities = replace(model.capabilities, max_context_window=872_000, max_context_window_verified=True)
    assert ContextWindowConfig(model_context_window=1_000_000).limits(model) == (784_800, 828_400)


def test_body_baseline_starts_as_estimate_then_becomes_first_real_input(tmp_path):
    builder = window_builder(WindowModel([], window=100_000), tmp_path, ContextWindowConfig(model_auto_compact_token_limit_scope="body_after_prefix"))
    builder.begin_provider_request([LLMMessage(role="user", content="prefix")], [])
    assert not builder._context_window_state.prefill_server_observed
    builder.record_actual_usage(UsageInfo(2_000, 500))
    builder.record_actual_usage(UsageInfo(8_000, 100))
    assert builder._context_window_state.prefill_input_tokens == 2_000
    assert builder.context_window_status()["auto_compact_scope_tokens"] == 6_100


@pytest.mark.parametrize("streamed", [False, True])
def test_direct_remaining_loop_preserves_source_text_state_and_reopened_journal(tmp_path, streamed):
    async def scenario():
        model = WindowModel([[ToolCallEvent("remaining", "get_context_remaining", {})], "done"], window=100_000, streamed=streamed)
        builder = window_builder(model, tmp_path)
        builder.append_assistant("prior response")
        builder.record_actual_usage(UsageInfo(2_000, 500))
        state, builder, journal, events = await run_window(tmp_path, model, builder=builder)
        assert state.terminal_status == "completed"
        expected = "You have 87500 tokens left in this context window."
        assert state.tool_calls[0].tool_output == expected
        assert next(message for message in model.inputs[1] if message.role == "tool").content == expected
        assert next(event for event in events if event.type == "tool_result").data["summary"] == expected
        assert next(event for event in events if event.type == "tool_result").data["visibility"] == "debug"
        reloaded = ExecutionJournal("window-test", base_dir=tmp_path / "journals")
        assert next(message for message in reloaded.reconstruct_history() if message["role"] == "tool")["content"] == expected
        assert not journal.unresolved_tool_uses()
    asyncio.run(scenario())


def test_code_mode_returns_exact_tokens_left_object_and_catalog_result_schema(tmp_path):
    async def scenario():
        code = "text(ALL_TOOLS.find(tool => tool.name === 'get_context_remaining').output_schema); text(await tools.get_context_remaining({})); text(ALL_TOOLS.some(tool => tool.name === 'new_context'));"
        model = WindowModel([[ToolCallEvent("code-budget", "tool_exec", {"code": code})], "done"])
        builder = window_builder(model, tmp_path)
        builder.append_assistant("previous response")
        builder.record_actual_usage(UsageInfo(2_000, 500))
        state, builder, journal, events = await run_window(tmp_path, model, builder=builder, code_only=True)
        assert state.terminal_status == "completed"
        message = next(message for message in model.inputs[1] if message.role == "tool")
        report = json.JSONDecoder().raw_decode(message.content[message.content.index("{"):])[0]
        assert json.loads(report["output"][0]) == GetContextRemainingTool().get_schema().output_schema
        assert json.loads(report["output"][1]) == {"tokens_left": 87_500}
        assert report["output"][2] == "false"
        assert "Promise<" in next(schema for schema in model.schemas[0] if schema["function"]["name"] == "tool_exec")["function"]["description"]
        assert "new_context" in {schema["function"]["name"] for schema in model.schemas[0]}
        assert any(event.event_type == "tool_use" and event.payload["tool_call"]["name"] == "get_context_remaining" for event in journal.read_events())
    asyncio.run(scenario())


@pytest.mark.parametrize("streamed", [False, True])
def test_new_context_waits_for_real_command_and_preserves_all_old_facts(tmp_path, streamed):
    async def scenario():
        proof = tmp_path / "proof.txt"
        proof.write_text("OLD_FILE_EVIDENCE", encoding="utf-8")
        store = ArtifactStore(storage_dir=tmp_path / "file-tools-artifacts")
        command = "Start-Sleep -Milliseconds 200; Write-Output 'WINDOW_COMMAND_EVIDENCE'"
        model = WindowModel([
            [ToolCallEvent("read-proof", "run_command", {"command": "Get-Content -LiteralPath '" + str(proof).replace("'", "''") + "'", "shell": "powershell", "login": False})],
            [ToolCallEvent("edit-proof", "apply_patch", {"patch": "*** Begin Patch\n*** Update File: " + proof.as_posix() + "\n@@\n-OLD_FILE_EVIDENCE\n+NEW_FILE_EVIDENCE\n*** End Patch"})],
            [ToolCallEvent("new-window", "new_context", {}), ToolCallEvent("real-command", "run_command", {"command": command, "shell": "powershell"})],
            "WINDOW_FOLLOWUP_FINISHED",
        ], streamed=streamed)
        skill_file = tmp_path / "SKILL.md"
        skill_body = "# Original Skill\n" + "Preserve the exact selected body. " * 900
        skill_file.write_text(skill_body, encoding="utf-8")
        state = AgentState(user_message="OLD_WINDOW_USER_REQUEST", conversation_id="window-conv", workspace_root=tmp_path,
            prompt_context={"skill_injections": [{"name": "real-skill", "path": str(skill_file), "content": skill_file.read_text(encoding="utf-8")} ]})
        def checkpoint_on_event(event, builder):
            async def observe():
                if event.type == "tool_result" and event.data["id"] == "new-window":
                    assert builder._context_window_state.window_number == 0
                    assert builder.new_context_window_requested
                    assert "OLD_WINDOW_USER_REQUEST" in json.dumps(builder.export_snapshot()["history"])
            return observe()
        approvals = []
        async def approve_tool(call_id):
            approvals.append(call_id)
            return {"action": "approve"}
        try:
            state, builder, journal, events = await run_window(tmp_path, model, state=state,
                extra_tools=(ApplyPatchTool(), RunCommandTool(store)), observe=checkpoint_on_event, approval=approve_tool)
            assert state.terminal_status == "completed", state.stopped_reason
            assert proof.read_text(encoding="utf-8") == "NEW_FILE_EVIDENCE\n", [
                (record.tool_name, record.status, record.tool_output) for record in state.tool_calls
            ]
            assert "edit-proof" in approvals
            assert not any(event.type == "error" for event in events)
            compact_index = next(index for index, event in enumerate(events) if event.type == "context_compacted")
            command_index = next(index for index, event in enumerate(events) if event.type == "tool_result" and event.data["id"] == "real-command")
            assert command_index < compact_index
            assert events[compact_index].data["summary"] == ""
            assert next(record for record in state.tool_calls if record.tool_call_id == "new-window").tool_output == NEW_CONTEXT_MESSAGE
            active_text = "\n".join(message.content for message in model.inputs[3])
            assert "OLD_WINDOW_USER_REQUEST" not in active_text
            assert "WINDOW_COMMAND_EVIDENCE" not in active_text
            assert skill_body.strip() in active_text
            snapshot = builder.export_snapshot()
            old = snapshot["inactive_context_windows"][0]
            old_text = json.dumps(old["history"], ensure_ascii=False)
            assert "OLD_WINDOW_USER_REQUEST" in old_text
            assert "OLD_FILE_EVIDENCE" in old_text
            assert "WINDOW_COMMAND_EVIDENCE" in old_text
            assert NEW_CONTEXT_MESSAGE in old_text
            first = old["context_window_state"]
            current = snapshot["context_window_state"]
            assert current["first_window_id"] == first["first_window_id"]
            assert current["previous_window_id"] == first["window_id"]
            assert current["window_id"] != first["window_id"]
            assert UUID(current["window_id"]).version == 7
            assert current["window_number"] == 1
            restored = ContextBuilder(llm=model, workspace_root=tmp_path)
            restored.load_snapshot(snapshot)
            restored.configure_context_window(ContextWindowConfig())
            assert restored.export_snapshot()["inactive_context_windows"] == snapshot["inactive_context_windows"]
            assert restored.export_snapshot()["context_window_state"] == current
            reopened = ExecutionJournal("window-test", base_dir=tmp_path / "journals")
            committed = next(event for event in reopened.read_events() if event.event_type == "system" and event.payload.get("lifecycle") == "compaction_committed")
            assert committed.payload["context_snapshot"]["inactive_context_windows"][0] == old
            assert not journal.unresolved_tool_uses()
        finally:
            store.shutdown()
    asyncio.run(scenario())


def test_new_window_archives_image_bytes_read_provenance_and_independent_fork_state(tmp_path):
    model = WindowModel([])
    builder = window_builder(model, tmp_path)
    image_data = base64.b64encode(b"original raw image bytes").decode()
    builder._history_store.append(LLMMessage(role="user", content="old image user", is_user_input=True,
        images=[{"media_type": "image/png", "data": image_data}]))
    builder.read_file_hashes()[str(tmp_path / "proof.txt")] = "exact-content-hash"
    cloned = clone_context_builder(builder)
    builder.start_new_context_window(AgentState(user_message="old image user", workspace_root=tmp_path))
    assert builder.export_snapshot()["inactive_context_windows"][0]["history"][0]["images"][0]["data"] == image_data
    assert builder.read_file_hashes() == {}
    assert builder.export_snapshot()["inactive_context_windows"][0]["read_file_hashes"][str(tmp_path / "proof.txt")] == "exact-content-hash"
    assert cloned._context_window_state.window_number == 0
    assert cloned.export_snapshot().get("inactive_context_windows", []) == []


def test_manual_core_compact_replaces_window_without_any_summarizer(tmp_path):
    async def scenario():
        model = WindowModel([])
        builder = window_builder(model, tmp_path)
        builder.append_user("old user source")
        assert await builder.compact(restore_state=AgentState(user_message="old user source", workspace_root=tmp_path)) == ""
        assert builder._context_window_state.window_number == 1
        assert "old user source" in json.dumps(builder.export_snapshot()["inactive_context_windows"])
        assert "old user source" not in "\n".join(message.content for message in builder._history)
    asyncio.run(scenario())


def test_exhausted_previous_window_rolls_before_admitting_new_user_input(tmp_path):
    async def scenario():
        model = WindowModel(["NEW_TASK_FINISHED"])
        builder = window_builder(model, tmp_path)
        builder.append_user("OLD_COMPLETED_TASK")
        builder.append_assistant("old final reply")
        builder.record_actual_usage(UsageInfo(89_000, 1_000))
        state = AgentState(user_message="NEW_TASK_AFTER_EXHAUSTED_WINDOW", conversation_id="window-conv", workspace_root=tmp_path)
        state, builder, _, events = await run_window(tmp_path, model, builder=builder, state=state)
        assert state.terminal_status == "completed"
        sent = "\n".join(message.content for message in model.inputs[0])
        assert "NEW_TASK_AFTER_EXHAUSTED_WINDOW" in sent
        assert "OLD_COMPLETED_TASK" not in sent
        assert builder._context_window_state.window_number == 1
        assert sum(event.type == "context_compacted" for event in events) == 1
    asyncio.run(scenario())


def test_automatic_source_limit_rollover_waits_until_tool_settlement(tmp_path):
    async def scenario():
        model = WindowModel([[ToolCallEvent("remaining-auto", "get_context_remaining", {})], "AUTO_WINDOW_FINISHED"], usage=UsageInfo(90_000, 1_000))
        state, builder, _, events = await run_window(tmp_path, model)
        assert state.terminal_status == "completed"
        assert builder._context_window_state.window_number == 1
        tool_index = next(index for index, event in enumerate(events) if event.type == "tool_result")
        compact_index = next(index for index, event in enumerate(events) if event.type == "context_compacted")
        assert tool_index < compact_index
        assert "OLD_WINDOW_USER_REQUEST" not in "\n".join(message.content for message in model.inputs[1])
    asyncio.run(scenario())


def test_durable_checkpoint_and_context_delta_keep_complete_inactive_windows(tmp_path):
    from backend.conversations.context_delta import apply_context_snapshot_delta
    model = WindowModel([])
    builder = window_builder(model, tmp_path)
    builder.append_user("Actual source user text " * 500)
    builder.extension_state = {"saved_fact": "Persistent extension fact", "pending_messages": ["Real queued input"],
        "_history_rewind": {"floor": 0, "changes": [{"history_end": 1, "undo": {}}]}}
    builder.extension_cursor = {"consumed_receipt": "Keep current receipt"}
    before = builder.export_snapshot()
    builder.start_new_context_window(AgentState(user_message="old", workspace_root=tmp_path))
    delta, _revision = builder.export_snapshot_delta(before, since_revision=None)
    snapshot = apply_context_snapshot_delta(before, delta)
    assert snapshot["inactive_context_windows"][0]["history"] == before["history"]
    assert snapshot["extension_state"]["pending_messages"] == ["Real queued input"]
    assert snapshot["extension_cursor"] == {"consumed_receipt": "Keep current receipt"}
    save_checkpoint("window-checkpoint", "old", 1, "", [], [], [], set(), "interrupted", -1,
        base_dir=tmp_path / "checkpoints", conversation_id="window-conv", context_snapshot=snapshot)
    checkpoint = load_latest_checkpoint("window-checkpoint", base_dir=tmp_path / "checkpoints")
    assert checkpoint.context_snapshot["inactive_context_windows"] == snapshot["inactive_context_windows"]
    restored = ContextBuilder(llm=model)
    restored.load_snapshot(checkpoint.context_snapshot)
    restored.configure_context_window(ContextWindowConfig())
    assert restored._context_window_state.first_window_id == builder._context_window_state.first_window_id
    assert restored._context_window_state.previous_window_id == builder._context_window_state.previous_window_id
    assert restored.extension_cursor == builder.extension_cursor


def test_reminder_and_fallback_are_once_per_window_and_explicit_new_skips_fallback(tmp_path):
    async def scenario():
        config = ContextWindowConfig(reminder_threshold_tokens=1_000, reminder_message_template="Source reminder {n_remaining}",
            auto_compact_fallback_prompt="Write window notes before reset", auto_compact_fallback_buffer_tokens=4_000)
        builder = window_builder(WindowModel([], window=10_000), tmp_path, config)
        builder.append_assistant("sampled")
        builder.record_actual_usage(UsageInfo(8_000, 1_000))
        state = AgentState(user_message="continue", workspace_root=tmp_path)
        assert [event async for event in settle_context_window(builder, state, needs_follow_up=True)] == []
        assert sum(message.content == "Source reminder 0" for message in builder._history) == 1
        assert sum(message.content == "Write window notes before reset" for message in builder._history) == 1
        assert [event async for event in settle_context_window(builder, state, needs_follow_up=True)] == []
        assert sum(message.content == "Write window notes before reset" for message in builder._history) == 1
        builder.request_new_context_window()
        events = [event async for event in settle_context_window(builder, state, needs_follow_up=True)]
        assert any(event.type == "context_compacted" for event in events)
        assert not any(message.content == "Write window notes before reset" for message in builder._history)
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_stage", ["before", "after", "neither"])
def test_window_hooks_use_real_stop_semantics_and_no_summary(tmp_path, stop_stage):
    class Hooks(HookManager):
        def __init__(self):
            super().__init__()
            self.calls = []
        def has_hooks(self, event):
            return event in {HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT}
        async def run_pre_compact(self, **kwargs):
            self.calls.append(("before", kwargs))
            return HookResult(prevent_continuation=stop_stage == "before")
        async def run_post_compact(self, **kwargs):
            self.calls.append(("after", kwargs))
            return HookResult(prevent_continuation=stop_stage == "after")
    async def scenario():
        builder = window_builder(WindowModel([]), tmp_path)
        builder.append_user("old work")
        hooks = Hooks()
        builder.bind_hook_manager(hooks)
        state = AgentState(user_message="old work", workspace_root=tmp_path)
        events = [event async for event in run_context_window_compaction(builder, state)]
        assert builder._context_window_state.window_number == (0 if stop_stage == "before" else 1)
        assert bool(events) is (stop_stage != "before")
        if stop_stage == "neither":
            assert state.stopped_reason is None
        else:
            assert state.stopped_reason == "interrupted"
            assert state.terminal_status == "cancelled"
        if stop_stage != "before":
            assert hooks.calls[1] == ("after", {"trigger": "auto"})
    asyncio.run(scenario())


def test_source_sidechat_developer_boundary_survives_rollover_and_new_user_turn(tmp_path):
    async def scenario():
        model = WindowModel([[ToolCallEvent("side-reset", "new_context", {})], "SIDE_FOLLOWUP_FINISHED"])
        builder = window_builder(model, tmp_path)
        builder.append_user("PARENT_TASK_REFERENCE_ONLY")
        state, builder, _, _ = await run_window(tmp_path, model, builder=builder,
            metadata={"conversation_type": "side_chat", "parent_conversation_id": "parent-conv"})
        assert state.terminal_status == "completed"
        for request in model.inputs:
            boundary = [message for message in request if message.content == codex_side_chat_instructions()]
            assert len(boundary) == 1
            assert boundary[0].role == "developer"
        assert "PARENT_TASK_REFERENCE_ONLY" in "\n".join(message.content for message in model.inputs[0])
        assert "PARENT_TASK_REFERENCE_ONLY" not in "\n".join(message.content for message in model.inputs[1])
        restored = ContextBuilder(llm=model)
        restored.load_snapshot(builder.export_snapshot())
        restored.configure_context_window(ContextWindowConfig())
        await restored.start_turn("NEW_SIDE_USER_REQUEST", state)
        request = await restored.build(state)
        assert sum(message.content == codex_side_chat_instructions() for message in request) == 1
        assert "NEW_SIDE_USER_REQUEST" in "\n".join(message.content for message in request)
        state.prompt_context["conversation_type"] = "main"
        assert not any(message.content == codex_side_chat_instructions() for message in restored._render_prompt_messages(state, tmp_path))
    asyncio.run(scenario())
