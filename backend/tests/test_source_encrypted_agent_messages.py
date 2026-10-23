"""Source V2 peer input across real child turns, mailboxes and cold history."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.guardian.host import parent_window_transcript
from backend.agent.mailbox_delivery import inject_subagent_mailbox_updates
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool_with_timeout
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tests.test_source_v2_configuration_chain import v2_case
from backend.tools.swarm_tools import SourceSendMessageTool
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY


def _native_item(author, recipient, message, *, trigger=False, encrypted=True):
    header = (f"Message Type: {'NEW_TASK' if trigger else 'MESSAGE'}\n"
        f"Task name: {recipient}\nSender: {author}\nPayload:\n")
    content = ([{"type": "input_text", "text": header},
        {"type": "encrypted_content", "encrypted_content": message}] if encrypted else
        [{"type": "input_text", "text": header + message}])
    return {"type": "agent_message", "author": author, "recipient": recipient, "content": content}


def _peer_items(messages):
    return [item for message in messages for item in message.provider_items if item["type"] == "agent_message"]


def test_native_peer_history_and_encryption_origin_survive_snapshot_and_guardian():
    opaque = "  opaque:source/" + "x" * 140 + "\n"
    native = _native_item("/root", "/root/worker", opaque, trigger=True)
    builder = ContextBuilder()
    builder.append_agent_input_messages([{"content": "", "provider_items": [native]}])
    calls = [ToolCallEvent(f"call-{index}", "send_message", {"target": "/root/worker", "message": opaque},
        native_type="function_call", native_namespace="collaboration", native_name="send_message",
        encrypted_function_args=marker) for index, marker in enumerate((None, [], ["message"]))]
    builder._history = [*builder._history, LLMMessage(role="assistant", content="", tool_calls=calls),
        *[LLMMessage(role="tool", content="", name=call.name, tool_call_id=call.id) for call in calls]]
    snapshot = json.loads(json.dumps(builder.export_snapshot()))
    assert "encrypted_function_args" not in snapshot["history"][1]["tool_calls"][0]
    assert snapshot["history"][1]["tool_calls"][1]["encrypted_function_args"] == []
    restored = ContextBuilder()
    restored.load_snapshot(snapshot)
    peer = restored._get_history_within_budget()[0]
    assert peer.content == "" and peer.input_origin == "agent" and not peer.is_user_input
    assert peer.provider_items == [native]
    assert [call.encrypted_function_args for call in restored._history[1].tool_calls] == [None, [], ["message"]]
    retained = parent_window_transcript(restored)
    assert any(entry.provider_item == native and entry.text == "" for entry in retained)
    assert restored.export_snapshot()["history"][0]["provider_items"] == [native]


@pytest.mark.asyncio
async def test_native_queue_messages_reach_child_and_virtual_parent_once(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    parent = runtime.start_run(run_id="root-turn", conversation_id="conversation")
    child = runtime.start_subagent(subagent_id="worker", parent_run_id=parent.run_id,
        agent_type="default", agent_path_segment="worker")
    root = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="conversation", metadata={"run_id": parent.run_id,
        "source_agent_message_encrypted": True}, run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    child_context = replace(root, metadata={"agent_id": child.subagent_id, "run_id": "child-turn",
        "mailbox_epoch": child.mailbox_epoch, "agent_mode": "subagent", "source_agent_message_encrypted": True})
    try:
        down = "  opaque-child-message\n"
        up = "  opaque-parent-message\n"
        assert not (await SourceSendMessageTool().execute({"target": "/root/worker", "message": down}, root)).is_error
        assert not (await SourceSendMessageTool().execute({"target": "/root", "message": up}, child_context)).is_error
        for context, expected in ((child_context, _native_item("/root", "/root/worker", down)),
            (root, _native_item("/root/worker", "/root", up))):
            builder = ContextBuilder()
            state = AgentState(user_message="")
            count = await inject_subagent_mailbox_updates(ctx=builder, state=state, metadata=context.metadata,
                conversation_id="conversation", run_context=context.run_context)
            assert count == 1
            assert _peer_items(builder._history) == [expected]
            assert builder._history[0].content == "" and not builder._history[0].is_user_input
            runtime.ack_swarm_message_claims(state.prompt_context["delivered_swarm_message_claims"])
            assert await inject_subagent_mailbox_updates(ctx=builder, state=state, metadata=context.metadata,
                conversation_id="conversation", run_context=context.run_context) == 0
        down_record, = [message for message in runtime.list_swarm_messages(participant_id=child.subagent_id)
            if message.recipient_id == child.subagent_id]
        up_record, = runtime.list_swarm_messages(participant_id="parent")
        assert down_record.content == up_record.content == ""
        assert down_record.encrypted_content == down and up_record.encrypted_content == up
        assert (up_record.author_agent_path, up_record.recipient_agent_path) == ("/root/worker", "/root")
        assert "encrypted_content" not in down_record.public_dict()
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker,cold", [(None, False), (["message"], True), ([], True)])
async def test_actual_spawn_send_followup_preserve_native_input_and_cold_checkpoint(tmp_path, marker, cold):
    config, registry, snapshot, policy = v2_case(tmp_path)
    config = replace(config, llm=replace(config.llm, reasoning_effort="off", reasoning_effort_levels=("off",)))

    class Model(LLMAdapter):
        _settings = config.llm

        def __init__(self):
            self.calls = []

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.calls.append(deepcopy(messages))
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Child completed.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages, *, max_tokens=None):
            return ""

        def model_id(self):
            return self._settings.model

        def supported_reasoning_efforts(self):
            return self._settings.reasoning_effort_levels

        def current_reasoning_effort(self):
            return self._settings.reasoning_effort

    model = Model()
    snapshot = replace(snapshot, config=config, llm=model, thinking_level="off")
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", session_id="session", conversation_id="conversation")
    checker = PermissionChecker(config.permissions, workspace_root=tmp_path)
    context = ToolExecutionContext(permission=checker.build_context(mode="bypass"), permission_checker=checker,
        workspace_root=tmp_path, session_id="session", conversation_id="conversation", llm=model,
        model_execution=snapshot, tool_registry=registry, run_context=RunContext(agent_runtime=runtime,
            model_execution=snapshot, multi_agent_version="v2"),
        metadata={"run_id": "parent", ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
    encrypted = marker != []
    inherited = _native_item("/root/earlier", "/root", "opaque parent history")
    parent_builder = ContextBuilder()
    parent_builder.append_agent_input_messages([{"content": "", "provider_items": [inherited]}])
    context.run_context.context_builder = parent_builder

    async def call(name, args, call_id):
        result = await run_tool_with_timeout(ToolCallEvent(call_id, name, args, native_type="function_call",
            native_namespace="collaboration", native_name=name, encrypted_function_args=deepcopy(marker)), registry, context)
        assert not result.is_error, result.content
        return result

    initial, queued, followup = "  initial opaque task\n", "  queued opaque detail\n", "  next opaque task\n"
    try:
        started = await call("spawn_agent", {"task_name": "worker", "message": initial, "fork_turns": "all"}, "spawn")
        child_id = started.runtime_metadata["subagent_id"]
        await runtime.wait_for_subagent_execution(child_id)
        assert runtime.get_subagent(child_id).status == "completed"
        first = _native_item("/root", "/root/worker", initial, trigger=True, encrypted=encrypted)
        assert _peer_items(model.calls[0]) == [inherited, first]
        record = runtime.get_subagent(child_id)
        assert record.input_prompt == ""
        assert record.resume_config["agent_input_messages"] == [{"content": "", "provider_items": [first]}]
        original_epoch = record.mailbox_epoch
        await call("send_message", {"target": "/root/worker", "message": queued}, "queue")
        assert len(model.calls) == 1
        if cold:
            assert await runtime.aclose(release_lease=True)
            runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
                enable_lease_heartbeat=False)
            runtime.start_run(run_id="parent-restored", session_id="session", conversation_id="conversation")
            context.run_context.agent_runtime = runtime
            context.metadata["run_id"] = "parent-restored"
            assert runtime.subagent_session(child_id) is None
        await call("followup_task", {"target": "/root/worker", "message": followup}, "followup")
        scheduler = runtime._subagent_mailbox_turns.get(child_id)
        if scheduler is not None:
            await scheduler[0]
        await runtime.wait_for_subagent_execution(child_id)
        assert len(model.calls) == 2
        expected = [inherited, first, _native_item("/root", "/root/worker", queued, encrypted=encrypted),
            _native_item("/root", "/root/worker", followup, trigger=True, encrypted=encrypted)]
        assert _peer_items(model.calls[1]) == expected
        assert runtime.get_subagent(child_id).mailbox_epoch == original_epoch + 1
        builder = runtime.subagent_session(child_id).context_builder
        carriers = [message for message in builder._history if message.provider_items and message.input_origin == "agent"]
        assert len(carriers) == 4 and all(message.content == "" and not message.is_user_input for message in carriers)
        assert not any(value in message.content for value in (initial, queued, followup) for message in model.calls[1])
        assert not any(entry.kind == "user" and entry.text in (initial, queued, followup)
            for entry in builder.guardian_evidence.transcript)
        assert runtime.claim_swarm_messages(participant_id=child_id, mailbox_epoch=original_epoch + 1,
            conversation_id="conversation") == []
    finally:
        await runtime.aclose(release_lease=True)
