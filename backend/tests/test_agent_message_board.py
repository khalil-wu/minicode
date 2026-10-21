from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from itertools import count
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.agent.context import ContextBuilder
from backend.agent.message_board import AgentMessageBoard, BoardActor, MessageBoards
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import FeatureFlags
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolCallSource, ToolExecutionContext
from backend.tools.agent_message_board_tools import message_board_tools
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.collaboration_tools import wait_for_coordination
from backend.tools.subagent_context import AgentExecutionProfile, subagent_toolset_policy
from backend.tests.test_source_agent_roles import RecordingModel


def make_case(tmp_path, *, backend="in_memory", model=None, run_id="main-a"):
    config = AppConfig(llm=LLMSettings(api_key="fixture", provider="custom", model="source-model"),
        feature_flags=FeatureFlags({"multi_agent_v2": True, "agent_message_board": True}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"features": {
            "multi_agent_v2": {"enabled": True, "message_board_in_memory": backend == "in_memory"}}}),)))
    config.agent = replace(config.agent, code_mode_only=False)
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id=run_id, conversation_id="tree-thread")
    runtime.configure_subagent_limit("tree-thread", 4, source_v2=True)
    registry = ToolRegistry()
    for tool in message_board_tools():
        registry.register(tool)
    snapshot = ModelExecutionSnapshot(config=config, llm=model, provider="custom", model="source-model")
    policy = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="bypass",
        tool_mode="direct", model_execution=snapshot)
    owner = RunContext(agent_runtime=runtime, model_execution=snapshot, multi_agent_version="v2")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        conversation_id="tree-thread", session_id="issuing-session", turn_id=run_id, tool_registry=registry,
        model_execution=snapshot, run_context=owner, metadata={"run_id": run_id, "turn_id": run_id,
            "_toolset_policy": policy})
    return SimpleNamespace(runtime=runtime, registry=registry, config=config, snapshot=snapshot, root=context, path=tmp_path, calls=count())


async def call(case, name, args, *, context=None, call_id=None):
    event = ToolCallEvent(call_id or f"board-{next(case.calls)}", name, args)
    result = await run_tool(event, case.registry, context or case.root)
    assert not result.is_error, result.content
    return json.loads(result.content)


def child(case, name="worker", *, active=False):
    actor_id = f"child-{name}"
    case.runtime.start_subagent(subagent_id=actor_id, parent_run_id=case.root.metadata["run_id"], agent_type="general",
        task_id=name, agent_path_segment=name, background=True)
    profile = AgentExecutionProfile(delivery="background", message_coordination=True, constrained_async_surface=True)
    policy = effective_toolset_policy(base_policy=subagent_toolset_policy(execution_profile=profile),
        tool_registry=case.registry, disabled_tools=set(), requires_explicit_workspace=False, workspace_root=case.path,
        permission_mode="bypass", tool_mode="direct", model_execution=case.snapshot)
    context = replace(case.root, turn_id=actor_id, permission=replace(case.root.permission, source=f"subagent:{actor_id}"),
        metadata={"agent_id": actor_id, "run_id": actor_id, "turn_id": actor_id,
            "_agent_execution_profile": profile, "_toolset_policy": policy},
        run_context=RunContext(agent_runtime=case.runtime, model_execution=case.snapshot, is_subagent=True, multi_agent_version="v2"))
    kernel = None
    if active:
        record = case.runtime.start_run(run_id=actor_id, conversation_id="tree-thread", parent_run_id=case.root.metadata["run_id"], role="subagent", task_id=name)
        kernel = TurnKernel(runtime=case.runtime, run_record=record, metadata=context.metadata, emit_event=None,
            initial_user_message="Owned child turn", state=AgentState(user_message="Owned child turn"), run_context=context.run_context)
    return context, kernel


@pytest_asyncio.fixture(params=["in_memory", "local"])
async def case(tmp_path, request):
    value = make_case(tmp_path, backend=request.param)
    try:
        yield value
    finally:
        await value.runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_source_nine_tool_discussions_subscriptions_and_unicode_read(case):
    other, _ = child(case)
    channel = await call(case, "create_channel", {"channel_name": "Design"})
    assert channel["message_count"] == 0 and channel["created_by"] == "/root"
    assert (await call(case, "subscribe", {"channel_name": "Design"}, context=other))["enabled"]
    root = await call(case, "post", {"channel_name": "Design", "text": "A shared decision. 中文🙂"})
    assert root["thread_id"] == root["message_id"] and root["author"] == "/root"
    reply = await call(case, "post", {"thread_id": root["message_id"], "text": "Worker agrees."}, context=other)
    assert reply["author"] == "/root/worker"
    assert (await call(case, "unsubscribe", {"thread_id": root["message_id"]}, context=other))["enabled"] is False
    await call(case, "post", {"thread_id": root["message_id"], "text": "Still opted out."}, context=other)
    database = case.runtime.message_boards.open("tree-thread", case.root.metadata["_toolset_policy"].coordination_options["message_board_backend"])
    with database.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM subscription_opt_outs WHERE agent='/root/worker'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM subscriptions WHERE agent='/root/worker' AND target LIKE '%thread%'").fetchone()[0] == 0
    threads = await call(case, "list_threads", {"channel_name": "Design", "sort": "activity"})
    assert threads["results"][0]["reply_count"] == 2
    discussion = await call(case, "read_thread", {"thread_id": root["message_id"], "limit": 1})
    assert discussion["root_post"]["text_preview"] == "A shared decision. 中文🙂" and discussion["has_more"]
    next_page = await call(case, "read_thread", {"thread_id": root["message_id"], "limit": 1, "cursor": discussion["next_cursor"]})
    assert next_page["root_post"] == discussion["root_post"] and next_page["has_more"] is False
    matched = await call(case, "search_posts", {"query": "AGREES", "author": "/root/worker"})
    assert [post["message_id"] for post in matched["results"]] == [reply["message_id"]]
    assert (await call(case, "get_channels", {"query": "DES"}))["results"][0]["message_count"] == 3
    read = await call(case, "read_post", {"message_id": root["message_id"], "offset_chars": 19, "limit_chars": 2})
    assert read["text"] == "中文" and read["next_offset_chars"] == 21 and read["n_chars"] == 22


@pytest.mark.asyncio
async def test_post_retry_is_one_source_logical_call_and_code_cell_identity(case):
    await call(case, "create_channel", {"channel_name": "retry"})
    args = {"channel_name": "retry", "text": "one logical direct invocation"}
    first = await call(case, "post", args, call_id="one-call")
    assert await call(case, "post", args, call_id="one-call") == first
    changed = await run_tool(ToolCallEvent("one-call", "post", {**args, "text": "different"}), case.registry, case.root)
    assert changed.is_error and "different post" in changed.content
    sources = {name: ToolCallSource(kind="code_mode", cell_id="cell-1", runtime_call_id="nested-1") for name in ("outer-a", "outer-b")}
    context = replace(case.root, call_sources=sources)
    code_args = {**args, "text": "one logical nested invocation"}
    code = await call(case, "post", code_args, context=context, call_id="outer-a")
    assert await call(case, "post", code_args, context=context, call_id="outer-b") == code
    posts = await call(case, "search_posts", {"channel_name": "retry"})
    assert posts["n_returned"] == 2


@pytest.mark.asyncio
async def test_notification_is_current_turn_assistant_fragment_without_idle_queue_or_self_notice(case):
    running, kernel = child(case, active=True)
    idle, _ = child(case, "idle")
    await call(case, "create_channel", {"channel_name": "updates"})
    await call(case, "subscribe", {"channel_name": "updates", "target_agent": "/root/worker"})
    cursor = running.run_context.turn_input_queue.activity_cursor()
    waiter = asyncio.create_task(running.run_context.turn_input_queue.wait_for_activity(after_seq=cursor, timeout=2))
    posted = await call(case, "post", {"channel_name": "updates", "text": "decision " + "x" * 250,
        "agents_to_notify": ["/root", "/root/worker", "/root/worker", "/root/idle"]})
    assert await waiter
    assert await wait_for_coordination(running, timeout_seconds=120) == {"kind": "message"}
    notices = running.run_context.turn_input_queue.take_message_board_notifications()
    assert len(notices) == 1 and notices[0].startswith("Message Type: CHANNEL_POST\nSender: /root\nChannel: updates\n")
    assert "[Use read_post for the rest.]" in notices[0] and posted["message_id"] in notices[0]
    builder = ContextBuilder()
    builder.append_message_board_notification(notices[0])
    exported = builder.export_snapshot()
    restored = ContextBuilder.deserialize_snapshot_history(exported["history"])
    assert restored[0].role == "assistant" and restored[0].content_kind == "agent_message_board.notification"
    assert not restored[0].is_user_input and not builder.guardian_evidence.snapshot().transcript
    kernel.defer_mailbox_to_next_turn()
    await call(case, "post", {"channel_name": "updates", "text": "late", "agents_to_notify": ["/root/worker", "/root/idle"]})
    assert not running.run_context.turn_input_queue.has_message_board_notifications()
    running.run_context.turn_input_queue.begin_turn("next-real-turn")
    assert not running.run_context.turn_input_queue.take_message_board_notifications()
    assert case.runtime.list_swarm_messages(participant_id="child-idle", conversation_id="tree-thread") == []
    assert "child-idle" not in case.runtime._subagent_mailbox_turns


@pytest.mark.asyncio
async def test_pages_fit_source_eight_kilobyte_budget_and_keep_real_continuations(case):
    body = "决策🙂e" * 2500
    roots = [await call(case, "post", {"new_channel_name": "pages", "text": body})]
    for index in range(1, 9):
        roots.append(await call(case, "post", {"channel_name": "pages", "text": f"{index}:" + body}))
    found, cursor = [], None
    while True:
        args = {"channel_name": "pages", "limit": 50, "max_chars_per_post": 20_000}
        if cursor:
            args["cursor"] = cursor
        page = await call(case, "search_posts", args)
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 8000
        found.extend(post["message_id"] for post in page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert set(found) == {root["message_id"] for root in roots} and len(found) == len(roots)
    offset, contents = 0, ""
    while offset < len(body):
        part = await call(case, "read_post", {"message_id": roots[0]["message_id"], "offset_chars": offset, "limit_chars": 20_000})
        contents += part["text"]
        assert part["next_offset_chars"] > offset
        offset = part["next_offset_chars"]
    assert contents == body


@pytest.mark.asyncio
async def test_same_root_thread_shares_board_across_main_turns_and_rejects_forged_tree(case):
    original = await call(case, "post", {"new_channel_name": "stable", "text": "persist across root turns"})
    old_child, _ = child(case)
    case.runtime.commit_terminal(case.root.metadata["run_id"], "completed")
    case.runtime.start_run(run_id="main-b", conversation_id="tree-thread")
    newer = replace(case.root, turn_id="main-b", metadata={**case.root.metadata, "run_id": "main-b", "turn_id": "main-b"})
    assert (await call(case, "read_post", {"message_id": original["message_id"]}, context=newer))["text"] == "persist across root turns"
    assert (await call(case, "read_post", {"message_id": original["message_id"]}, context=old_child))["author"] == "/root"
    case.runtime.start_run(run_id="foreign", conversation_id="other-tree")
    forged = replace(case.root, metadata={**case.root.metadata, "run_id": "foreign"})
    denied = await run_tool(ToolCallEvent("forged", "read_post", {"message_id": original["message_id"]}), case.registry, forged)
    assert denied.is_error and "another message board" in denied.content
    record = case.runtime.get_subagent("child-worker")
    record.agent_path = "main-a/false_parent/worker"
    denied = await run_tool(ToolCallEvent("bad-path", "get_channels", {}), case.registry, old_child)
    assert denied.is_error and "own its tree path" in denied.content


@pytest.mark.asyncio
async def test_local_storage_independent_handles_share_transactions_and_permanent_deletion_tombstone(tmp_path):
    case = make_case(tmp_path, backend="local")
    independent = MessageBoards(case.runtime.state_root)
    try:
        source = await call(case, "post", {"new_channel_name": "durable", "text": "accepted once"}, call_id="durable-call")
        actor = BoardActor(replace(case.root, tool_call_id="durable-call"))
        board = AgentMessageBoard(independent.open(actor.tree, "local"), actor)
        retried, recipients, notice = await board.run("post", {"new_channel_name": "durable", "text": "accepted once"}, request_id="main-a:durable-call")
        assert retried == source and recipients == [] and notice is None
        original = AgentMessageBoard(case.runtime.message_boards.open(actor.tree, "local"), actor)
        concurrent_args = {"channel_name": "durable", "text": "Concurrent handles accept one logical invocation."}
        writes = await asyncio.gather(*(asyncio.to_thread(asyncio.run,
            handle.run("post", concurrent_args, request_id="main-a:concurrent-call")) for handle in (original, board)))
        assert writes[0][0] == writes[1][0]
        queried, _, _ = await board.run("search_posts", {"channel_name": "durable"})
        assert queried["n_returned"] == 2
        case.runtime.message_boards.close()
        read, _, _ = await board.run("read_post", {"message_id": source["message_id"]})
        assert read["text"] == "accepted once"
        case.runtime.commit_terminal("main-a", "completed")
        case.runtime.purge_conversation("tree-thread")
        with pytest.raises(ValueError, match="permanently deleted"):
            await board.run("get_channels", {})
    finally:
        independent.close()
        await case.runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_source_input_errors_and_small_mutation_budget_leave_no_write(case):
    invalid = [
        ("create_channel", {"channel_name": " edge "}),
        ("post", {"channel_name": "first", "new_channel_name": "second", "text": "bad"}),
        ("post", {"new_channel_name": "oversized", "text": "x" * (64 * 1024 + 1)}),
        ("post", {"new_channel_name": "too_many", "text": "bad", "agents_to_notify": ["/root"] * 257}),
        ("get_channels", {"cursor": "AAA="}),
        ("get_channels", {"caller": "forged"}),
    ]
    for index, (name, args) in enumerate(invalid):
        result = await run_tool(ToolCallEvent(f"invalid-{index}", name, args), case.registry, case.root)
        assert result.is_error, result.content
    definition = ModelDefinition(provider="custom", id="source-model", name="Budget fixture", api="chat", base_url="http://fixture.invalid",
        truncation_policy={"mode": "bytes", "limit": 100})
    narrow = replace(case.root, model_execution=replace(case.snapshot, model_info=definition))
    denied = await run_tool(ToolCallEvent("too-small", "create_channel", {"channel_name": "unacknowledged"}), case.registry, narrow)
    assert denied.is_error and "no change was made" in denied.content
    assert (await call(case, "get_channels", {}))["n_returned"] == 0


@pytest.mark.asyncio
async def test_clock_failure_does_not_post_or_break_accepted_retry_and_activity_uses_time_values(case, monkeypatch):
    from backend.agent import message_board as module

    values = iter([datetime(2026, 9, 18, 12, tzinfo=timezone.utc),
        datetime(2026, 9, 18, 12, 0, 0, 500_000, tzinfo=timezone.utc)])
    class Clock:
        @classmethod
        def now(cls, _tz):
            try:
                return next(values)
            except StopIteration:
                raise RuntimeError("caller clock unavailable")
    monkeypatch.setattr(module, "datetime", Clock)
    args = {"new_channel_name": "clock", "text": "Whole second root"}
    first = await call(case, "post", args, call_id="clock-root")
    reply = await call(case, "post", {"thread_id": first["message_id"], "text": "Half second later"})
    threads = await call(case, "list_threads", {"channel_name": "clock"})
    assert threads["results"][0]["last_activity_at"] == reply["created_at"]
    failed = await run_tool(ToolCallEvent("clock-failed", "post", {"channel_name": "clock", "text": "Cannot date this post"}), case.registry, case.root)
    assert failed.is_error and "caller clock unavailable" in failed.content
    assert await call(case, "post", args, call_id="clock-root") == first
    assert (await call(case, "search_posts", {"channel_name": "clock"}))["n_returned"] == 2


@pytest.mark.asyncio
async def test_old_remote_turn_notice_and_unbind_cannot_target_the_new_child_turn(case):
    context, kernel = child(case, active=True)
    old = kernel.run_record
    replacement = case.runtime.start_run(run_id="later-child-turn", conversation_id="tree-thread",
        parent_run_id="main-a", role="subagent", task_id="child-worker")
    queue = context.run_context.turn_input_queue
    queue.begin_turn(replacement.run_id)
    manager = case.runtime.message_boards
    manager.bind_turn(replacement, {"agent_id": "child-worker", "run_id": replacement.run_id}, queue)
    notice = {"author": "/root", "channel_name": "events", "message_id": "message", "thread_id": "thread",
        "text_preview": "current remote preview", "truncated": False}
    receiver = asyncio.create_task(asyncio.Event().wait())
    manager.remote_receivers[("tree-thread", "/root/worker")] = (replacement.run_id, receiver, context.run_context)
    try:
        assert not manager.notify("tree-thread", "/root/worker", "child-worker", notice, expected_turn_id=old.run_id)
        assert manager.notify("tree-thread", "/root/worker", "child-worker", notice, expected_turn_id=replacement.run_id)
        assert receiver not in context.run_context.lifecycle_cleanup_tasks
        manager.unbind_turn(old)
        assert not receiver.cancelling() and ("tree-thread", "/root/worker") in manager.active
        manager.unbind_turn(replacement)
        assert receiver.cancelling()
        assert receiver in context.run_context.lifecycle_cleanup_tasks
        assert not manager.notify("tree-thread", "/root/worker", "child-worker", notice, expected_turn_id=replacement.run_id)
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)


def test_legacy_snapshot_only_restore_keeps_board_context_without_manufacturing_a_visible_answer(tmp_path):
    builder = ContextBuilder()
    literal = "Message Type: CHANNEL_POST\nUser-authored literal"
    builder.append_user(literal)
    builder.append_message_board_notification("Message Type: CHANNEL_POST\nInternal contextual notice")
    builder.append_assistant("Actual final answer", phase="final_answer", answer_source="model_final")
    directory = tmp_path / "conversations"
    directory.mkdir()
    (directory / "conv_legacy_board.meta.json").write_text(json.dumps({"id": "conv_legacy_board", "title": "Legacy board context"}), encoding="utf-8")
    (directory / "conv_legacy_board.snapshot.json").write_text(json.dumps(builder.export_snapshot(), ensure_ascii=False), encoding="utf-8")
    reloaded = ConversationRepository(base_dir=directory).get_conversation("conv_legacy_board")
    assert [(message["role"], message["content"]) for message in reloaded.transcript] == [
        ("user", literal), ("assistant", "Actual final answer")]
    assert any(message.get("content_kind") == "agent_message_board.notification" for message in reloaded.context_snapshot["history"])


class ChildBoardModel(RecordingModel):
    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls.append((messages, tools))
        outputs = [message for message in messages if message.role == "tool" and message.name == "post"]
        if not outputs:
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent("child-post", "post", {"new_channel_name": "real_child", "text": "Actual child QueryEngine used the board."})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            assert not outputs[-1].is_error, outputs[-1].content
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Child board complete", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop")


@pytest.mark.asyncio
async def test_real_source_v2_background_child_passes_receiving_profile_and_board_admission(tmp_path):
    model = ChildBoardModel()
    case = make_case(tmp_path, model=model)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    case.registry.register(TaskTool(artifact_store=artifacts, workspace_root=tmp_path, config_snapshot=case.config))
    policy = effective_toolset_policy(base_policy=None, tool_registry=case.registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="bypass", tool_mode="direct", model_execution=case.snapshot)
    parent = replace(case.root, permission_checker=checker, permission=checker.build_context(mode="bypass"), llm=model,
        metadata={**case.root.metadata, "_toolset_policy": policy})
    try:
        spawned = await run_tool(ToolCallEvent("real-spawn", "spawn_agent", {"task_name": "writer", "message": "Use post to record the actual child result.", "fork_turns": "none"}), case.registry, parent)
        assert not spawned.is_error, spawned.content
        agent_id = spawned.runtime_metadata["subagent_id"]
        async with asyncio.timeout(20):
            await case.runtime.wait_for_subagent_execution(agent_id)
        assert model.calls and all(any(schema["function"]["name"] == "post" for schema in tools) for _, tools in model.calls)
        posted = await call(case, "search_posts", {"channel_name": "real_child"})
        assert posted["n_returned"] == 1 and posted["results"][0]["author"] == "/root/writer"
    finally:
        await case.runtime.aclose(release_lease=True)
        await artifacts.aclose()
