"""Real accepted inputs, durable cold restore and actual Guardian HTTP requests."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.compaction import retain_user_inputs
from backend.agent.guardian.heartbeat import selected_transcript
from backend.agent.guardian.async_config import GuardianAsyncConfig
from backend.agent.guardian.async_request import CollectedAsyncEvidence, prepare_retained
from backend.agent.guardian.async_sampler import LunaSampler
from backend.agent.guardian.async_service import build_async_evidence
from backend.agent.guardian.host import GuardianHostState, live_user_messages, parent_window_transcript
from backend.agent.guardian.prompt import GuardianPermissionEvidence, build_guardian_prompt
from backend.agent.guardian.retained import GuardianTranscriptEntry
from backend.agent.guardian.runtime import GuardianRuntime
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import QueryEngine
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings, TokenBudget
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.services.turn_notifications import heartbeat_user_message
from backend.services.llm_adapter_factory import create_session_llm
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.guardian_http_fixture import classifier_events, http_endpoint, send_json, send_sse
from backend.tests.test_codex_guardian import guardian_context
from backend.tests.test_codex_guardian_async import configured_context
from backend.tests.test_codex_guardian_checkpoint_chain import raw_checkpoint
from backend.tests.test_harness_turn_boundaries import query_factory


def heartbeat(index, instructions="Monitor only."):
    return heartbeat_user_message("monitor", f"2026-09-23T00:{index:02}:00Z", instructions)


class Replies(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Actual heartbeat result", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "Explicit fixture summary"


@pytest.mark.asyncio
async def test_actual_query_admission_35_inputs_preserve_public_text_order_latest_skill_and_cold_facts(tmp_path, monkeypatch, query_factory):
    monkeypatch.setattr("backend.agent.context.build_git_status_context_async", AsyncMock(return_value=""))
    configuration = tmp_path / "configuration"
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(configuration))
    skill = configuration / "skills" / "checks" / "SKILL.md"
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(workspace_root=str(tmp_path))
    create_session, make_submission = query_factory
    session = create_session(Replies())
    manager = SkillManager(SkillLoader(tmp_path))
    originals = []
    cold_host = None
    try:
        for index in range(35):
            if index == 34:
                skill.parent.mkdir(parents=True)
                skill.write_text("---\nname: checks\ndescription: Actual user-owned workflow\n---\nRead the local files.\n", encoding="utf-8")
            origin = "user" if index in {1, 33} else "heartbeat"
            text = "Create a worktree." if index == 1 else heartbeat(index,
                "Stop monitoring." if index == 31 else "Monitor only.\n$checks")
            originals.append(text)
            request = make_submission(session, text)
            request.runtime.skill_manager = manager
            request.runtime.metadata.update({"conversation_id": conversation.id, "turn_id": f"actual-turn-{index}",
                "user_message_id": f"input-{index}", "input_origin": origin})
            events = [event async for event in QueryEngine().submit(request)]
            assert next(event for event in events if event.type == "done").data["status"] == "completed", events
            repository.append_transcript_message(conversation.id, {"id": f"input-{index}", "role": "user", "content": text})
        builder = session.context_builder
        owner = builder.guardian_evidence
        assert [record.value.message_id for record in owner.retained.user_messages] == [f"input-{index}" for index in (0, 1, 31, 32, 33)]
        accepted = [message for message in builder._history if message.is_user_input]
        assert len(accepted) == 35 and [builder.admitted_user_text(message) for message in accepted] == originals
        assert [message.accepted_input["message_id"] for message in accepted] == [f"input-{index}" for index in range(35)]
        root_snapshot = session.guardian_host.root_snapshot()
        assert root_snapshot.trusted_skill_paths == (str(skill.resolve()),)
        assert owner.reconciled_context().latest_user_turn_id == "actual-turn-34"
        record = repository.get_conversation(conversation.id)
        record.context_snapshot = builder.export_snapshot()
        repository.save_conversation(record)
        reloaded = ConversationRepository(repository.storage_root).get_conversation(conversation.id)
        assert [message["content"] for message in reloaded.transcript] == originals
        assert "unchanged from transcript entry" not in json.dumps(reloaded.transcript)
        cold = ContextBuilder(llm=Replies())
        cold.load_snapshot(reloaded.context_snapshot)
        cold_host = GuardianHostState(conversation.id)
        cold_host.bind(cold)
        before = deepcopy(cold.export_snapshot())
        assert cold_host.root_snapshot().trusted_skill_paths == ()
        assert cold.guardian_evidence.reconciled_context().latest_user_turn_id == "actual-turn-34"
        assert cold.export_snapshot() == before
        assert [message.accepted_input for message in cold._history if message.is_user_input] == [message.accepted_input for message in accepted]
        async def respond(request, reader, writer):
            await send_sse(writer, classifier_events(request.index))
        async with http_endpoint(respond) as endpoint:
            ctx, _ = configured_context(tmp_path)
            ctx.conversation_id, ctx.turn_id = conversation.id, "actual-turn-34"
            config = GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
            permissions = GuardianPermissionEvidence.from_policy(ctx.sandbox_policy, cwd=ctx.workspace_root, environment_id=ctx.environment_id)
            sampler = LunaSampler(ctx.model_execution, conversation_id=conversation.id,
                llm_factory=lambda cfg, **_: OpenAIAdapter(replace(cfg.llm, base_url=endpoint.url + "/v1", wire_api="responses", proxy_mode="direct")))
            sampler.connections.retry_after = float("inf")
            try:
                assert await sampler.sample(build_async_evidence(session.guardian_host.snapshot(), '{"tool":"read_file"}', permissions, config),
                    instructions="Actual classifier policy", context=ctx) == "low"
                skill_evidence = [item for item in endpoint.requests[0].body["input"] if item.get("role") == "developer"
                    and "Codex-verified invoked user-owned skill paths:\n" in wire_text([item])]
                assert len(skill_evidence) == 1
                body = wire_text(skill_evidence).split("\n", 1)[1]
                assert json.loads(body) == [str(skill.resolve())]
            finally:
                await sampler.aclose()
        request = make_submission(session, "Only report status; no Skill invocation this turn.")
        request.runtime.skill_manager = manager
        request.runtime.metadata.update({"conversation_id": conversation.id, "turn_id": "actual-turn-35", "user_message_id": "input-35"})
        assert next(event for event in [event async for event in QueryEngine().submit(request)] if event.type == "done").data["status"] == "completed"
        assert str(skill.resolve()) in builder._invoked_skill_payloads
        assert session.guardian_host.root_snapshot().trusted_skill_paths == ()
    finally:
        if cold_host is not None:
            await cold_host.aclose()
        await session.aclose()


async def admitted_builder(texts, *, llm=None):
    builder = ContextBuilder(llm=llm or Replies())
    for index, text in enumerate(texts):
        message = await builder.start_turn(text, AgentState(user_message=text))
        message.input_origin = "heartbeat"
        builder.record_admitted_user_input(message, text=text, turn_id=f"turn-{index}", message_id=f"input-{index}",
            acceptance_order=builder.guardian_evidence.reserve_order())
        builder.append_assistant("Actual completed answer", phase="final_answer")
    return builder


def wire_text(items):
    return "\n".join(part["text"] for item in items if item.get("type", "message") == "message" and item.get("role") in {"user", "assistant", "developer"}
        for part in (item["content"] if isinstance(item["content"], list) else [{"type": "input_text", "text": item["content"]}])
        if part["type"] in {"input_text", "output_text", "text"})


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["independent", "thread_owned"])
@pytest.mark.parametrize("mode", ["snapshot", "conversation"])
async def test_actual_async_http_both_windows_keep_references_and_committed_delivery_proof(tmp_path, window, mode):
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index))
    async with http_endpoint(respond) as endpoint:
        builder = await admitted_builder([heartbeat(index) for index in range(3)])
        builder.guardian_evidence.context_mode = window
        cold = ContextBuilder(llm=Replies())
        cold.load_snapshot(json.loads(json.dumps(builder.export_snapshot())))
        host = GuardianHostState("heartbeat-parent")
        host.bind(cold)
        ctx, _ = configured_context(tmp_path)
        permissions = GuardianPermissionEvidence.from_policy(ctx.sandbox_policy, cwd=ctx.workspace_root, environment_id=ctx.environment_id)
        config = GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
        sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_: OpenAIAdapter(replace(cfg.llm, base_url=endpoint.url + "/v1", wire_api="responses", proxy_mode="direct")))
        sampler.connections.retry_after = float("inf")
        history, cursor = [], None
        try:
            for index in range(2):
                evidence = host.snapshot()
                if mode == "conversation":
                    prepared = prepare_retained(CollectedAsyncEvidence(evidence, '{"tool":"read_file","path":"actual.txt"}', permissions, config),
                        history, cursor, sampler=sampler, instructions="Actual classifier policy", checkpoint=None, reset_token_limit=100000)
                    ready = asyncio.get_running_loop().create_future()
                    completion = asyncio.create_task(sampler.sample_retained(prepared, ready=ready, instructions="Actual classifier policy", context=ctx))
                    assert await ready == "low"
                    history = await completion
                    assert history is not None
                    cursor = prepared.cursor
                else:
                    assert await sampler.sample(build_async_evidence(evidence, '{"tool":"read_file","path":"actual.txt"}', permissions, config),
                        instructions="Actual classifier policy", context=ctx) == "low"
                if index == 0:
                    cold._history_store.append(LLMMessage(role="tool", name="read_file", tool_call_id="actual-read", content="ACTUAL_NEW_TOOL_RESULT"))
                    cold.guardian_evidence.record_transcript(GuardianTranscriptEntry("tool read_file result", "ACTUAL_NEW_TOOL_RESULT"))
            first, second = [request.body for request in endpoint.requests]
            assert wire_text(first["input"]).count("unchanged from transcript entry [1]") == 2
            if mode == "conversation":
                prefix = [*first["input"], *(event["item"] for event in classifier_events(1) if event["type"] == "response.output_item.done")]
                assert second["input"][:len(prefix)] == prefix
                suffix = wire_text(second["input"][len(prefix):])
                assert "TRANSCRIPT DELTA START" in suffix and "ACTUAL_NEW_TOOL_RESULT" in suffix
                assert "Monitor only." not in suffix and "unchanged from transcript entry" not in suffix
            else:
                assert wire_text(second["input"]).count("unchanged from transcript entry [1]") == 2
                assert "ACTUAL_NEW_TOOL_RESULT" in wire_text(second["input"])
        finally:
            await sampler.aclose()
            await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_parent_compaction", [False, True])
@pytest.mark.parametrize("content_item_kinds", [True, False])
async def test_actual_sync_http_after_native_compaction_and_cold_restore_starts_window_with_full_body(tmp_path, reuse_parent_compaction, content_item_kinds):
    checkpoint = raw_checkpoint(id="cmp-heartbeat")
    async def respond(request, reader, writer):
        if request.path.endswith("/compact"):
            accepted = [item for item in request.body["input"] if item.get("type") == "message" and item.get("role") == "user"]
            assert len(accepted) == 3
            assert len({item["id"] for item in accepted}) == 3
            for index, item in enumerate(accepted):
                assert item["id"].startswith("msg_") and item["id"] != f"input-{index}"
                assert item["content"] == [{"type": "input_text", "text": heartbeat(index)}]
                metadata = {"turn_id": f"turn-{index}"}
                if content_item_kinds:
                    metadata["content_item_kinds"] = ["user.heartbeat"]
                assert item["internal_chat_message_metadata_passthrough"] == metadata
            # The service only echoes IDs/turns/markers actually sent to it.
            await send_json(writer, {"id": "compact-fixture", "object": "response.compaction",
                "output": [checkpoint, *accepted[1:]], "usage": {"input_tokens": 100, "output_tokens": 10}})
        else:
            answer = '{"outcome":"allow","rationale":"Explicit local protocol fixture."}'
            await send_sse(writer, classifier_events(request.index, choice=answer, final=answer))
    async with http_endpoint(respond) as endpoint:
        url = endpoint.url + "/v1"
        settings = LLMSettings(provider="custom", api_key="explicit-local-fixture", base_url=url, model="parent", wire_api="responses",
            native_compaction=True, comp_hash="actual-heartbeat-producer", max_tokens=512)
        info = ModelDefinition(provider="custom", id="parent", name="parent", api="openai-responses", base_url=url,
            comp_hash="actual-heartbeat-producer", context_window=128000, extra={"auto_review_model_override": "explicit-reviewer"})
        cfg = AppConfig(llm=settings, token_budget=TokenBudget(total=128000), feature_flags=FeatureFlags({
            "guardian_approval": True, "guardian_reuse_parent_compaction": reuse_parent_compaction,
            "content_item_kinds": content_item_kinds}))
        adapter = create_session_llm(cfg)
        model = replace(ModelExecutionSnapshot.capture(cfg, adapter), model_info=info)
        builder = await admitted_builder([heartbeat(index) for index in range(3)], llm=adapter)
        original_ids = [message.accepted_input["model_item_id"] for message in builder._history if message.is_user_input]
        builder.bind_guardian_model(model)
        await builder.compact()
        assert [entry[1].message_id for entry in live_user_messages(builder)] == ["input-1", "input-2"]
        assert [item["id"] for item in endpoint.requests[0].body["input"] if item.get("type") == "message"] == original_ids
        assert builder.guardian_evidence.reconciled_context(live_user_messages(builder)).latest_user_turn_id == "turn-2"
        cold = ContextBuilder(llm=adapter)
        cold.load_snapshot(json.loads(json.dumps(builder.export_snapshot())))
        assert cold.guardian_evidence.reconciled_context(live_user_messages(cold)).latest_user_turn_id == "turn-2"
        cold.bind_guardian_model(model, restored=True)
        host = GuardianHostState("heartbeat-parent")
        host.bind(cold)
        ctx, action = guardian_context(tmp_path)
        ctx.model_execution = model
        host.service.pool.runtime_factory = lambda **arguments: GuardianRuntime(**arguments,
            llm_factory=lambda config, **_: OpenAIAdapter(config.llm))
        try:
            assert (await host.service.decide(action, ctx, required=True)).status == "approved"
            assert endpoint.requests[0].path == "/v1/responses/compact"
            review = next(request.body for request in endpoint.requests if request.path == "/v1/responses")
            text = wire_text(review["input"])
            assert text.count("unchanged from transcript entry [1]") == (int(content_item_kinds) if reuse_parent_compaction else 2)
            assert f"<current_time_iso>2026-09-23T00:{1 if reuse_parent_compaction else 0:02}:00Z</current_time_iso>" in text
            assert "Codex-verified invoked user-owned skill paths" not in text
            assert [item for item in review["input"] if item.get("type") == "compaction"] == ([checkpoint] if reuse_parent_compaction else [])
            assert len(cold.guardian_evidence.transcript) == 3
        finally:
            await host.aclose()
            await adapter.aclose()


@pytest.mark.asyncio
async def test_shortened_live_window_cannot_resurrect_original_body_or_complete_delivery():
    original = heartbeat(0, "Monitor only. " * 120)
    builder = await admitted_builder([original])
    admitted = [message for message in builder._history if message.is_user_input]
    shortened = retain_user_inputs([replace(admitted[0], content=builder._strip_trusted_runtime_context(admitted[0]))], 30)
    assert shortened and len(shortened[0].content) < len(original)
    snapshot = builder.export_snapshot()
    snapshot["history"] = [{**snapshot["history"][0], "content": shortened[0].content}]
    cold = ContextBuilder(llm=Replies())
    cold.load_snapshot(snapshot)
    entries = parent_window_transcript(cold)
    assert len(entries) == 1 and entries[0].text != original
    assert entries[0].source_order is None and entries[0].source_revision is None


@pytest.mark.asyncio
async def test_rebuilt_window_reference_does_not_claim_original_instruction_delivery(tmp_path):
    builder = await admitted_builder([heartbeat(index) for index in range(3)])
    host = GuardianHostState("heartbeat-parent")
    host.bind(builder)
    ctx, _ = configured_context(tmp_path)
    permissions = GuardianPermissionEvidence.from_policy(ctx.sandbox_policy, cwd=ctx.workspace_root, environment_id=ctx.environment_id)
    config = GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
    # A new physical window starts at the second invocation; its complete
    # body has a different source identity from the first retained instruction.
    raw = parent_window_transcript(builder)
    user_rows = tuple(entry for entry in raw if entry.kind == "user")
    evidence = replace(host.snapshot(), parent_window_transcript=user_rows[1:])
    selected = selected_transcript(evidence)
    assert selected[0].text == heartbeat(1)
    assert "unchanged from transcript entry [1]" in selected[1].text
    assert selected[1].source_order is None and selected[1].source_revision is None
    items, _ = CollectedAsyncEvidence(evidence, '{"tool":"read_file"}', permissions, config).compose(None, [])
    text = wire_text([item.item for item in items])
    assert "Retained source order: 0" in text
    assert "<current_time_iso>2026-09-23T00:00:00Z</current_time_iso>" in text
    assert "<current_time_iso>2026-09-23T00:01:00Z</current_time_iso>" in text
    await host.aclose()
