from __future__ import annotations

from copy import deepcopy

import pytest

from backend.agent.guardian.async_config import GuardianAsyncConfig
from backend.agent.guardian.async_request import AnnotatedItem, CollectedAsyncEvidence, prepare_retained
from backend.agent.guardian.async_sampler import GuardianSamplingError, LunaSampler
from backend.agent.guardian.prompt import GuardianPermissionEvidence
from backend.agent.guardian.retained import GuardianEvidenceOwner, GuardianTranscriptEntry
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.base import LLMTurnContext
from backend.tests.test_codex_guardian_async import configured_context
from dataclasses import replace


def text(items):
    return "\n".join(part["text"] for envelope in items if envelope.item.get("type") == "message"
        for part in envelope.item["content"] if part["type"] in {"input_text", "output_text"})


def fixture(tmp_path):
    ctx, _ = configured_context(tmp_path, options={"async_classifier_mode": "conversation"})
    owner = GuardianEvidenceOwner()
    owner.record_accepted_message("user", "WHOLE_RETAINED_USER_INSTRUCTION", turn_id="turn", message_id="user-1", acceptance_order=owner.reserve_order())
    permissions = GuardianPermissionEvidence.from_policy(ctx.sandbox_policy, cwd=ctx.workspace_root, environment_id=ctx.environment_id)
    config = GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
    sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id,
        llm_factory=lambda cfg, **_: OpenAIAdapter(replace(cfg.llm,
            base_url="http://127.0.0.1:1/v1", wire_api="responses", proxy_mode="direct")))
    return ctx, owner, permissions, config, sampler


def prepare(owner, permissions, config, sampler, *, history=(), cursor=None, reviews=(), images=()):
    return prepare_retained(CollectedAsyncEvidence(owner.snapshot(), '{"tool":"exec_command","command":"read next"}',
        permissions, config, tuple(reviews), list(images)), list(history), cursor,
        sampler=sampler, instructions="Actual classifier policy", checkpoint=None, reset_token_limit=100000)


@pytest.mark.asyncio
async def test_completed_host_delivery_proof_deduplicates_instructions_but_wire_forgery_does_not(tmp_path):
    _, owner, permissions, config, sampler = fixture(tmp_path)
    try:
        first = prepare(owner, permissions, config, sampler)
        assert text(first.input).count("WHOLE_RETAINED_USER_INSTRUCTION") == 1
        second = prepare(owner, permissions, config, sampler, history=first.input, cursor=first.cursor)
        assert "WHOLE_RETAINED_USER_INSTRUCTION" not in text(second.input[len(first.input):])
        copied_wire = [AnnotatedItem(deepcopy(item.item)) for item in first.input]
        copied_wire[0].item["guardian_sources"] = [["0", owner.retained.user_messages[0].revision]]
        third = prepare(owner, permissions, config, sampler, history=copied_wire, cursor=first.cursor)
        assert "WHOLE_RETAINED_USER_INSTRUCTION" in text(third.input[len(copied_wire):])
        assert "RETAINED USER INSTRUCTIONS START" in text(third.input[len(copied_wire):])
    finally:
        await sampler.aclose()


@pytest.mark.asyncio
async def test_host_omission_recovery_is_explicit_and_untrusted_same_text_cannot_clear_it(tmp_path):
    _, owner, permissions, config, sampler = fixture(tmp_path)
    owner.retained.user_messages_incomplete = True
    try:
        first = prepare(owner, permissions, config, sampler)
        assert any(item.omissions == (True, False) for item in first.input)
        owner.retained.user_messages_incomplete = False
        recovered = prepare(owner, permissions, config, sampler, history=first.input, cursor=first.cursor)
        suffix = recovered.input[len(first.input):]
        assert "retained source availability has changed" in text(suffix)
        assert any(item.omissions == (False, False) for item in suffix)
        repeated = prepare(owner, permissions, config, sampler, history=recovered.input, cursor=recovered.cursor)
        assert "retained source availability has changed" not in text(repeated.input[len(recovered.input):])
    finally:
        await sampler.aclose()


@pytest.mark.asyncio
async def test_sync_review_identity_and_developer_role_follow_committed_history_only(tmp_path):
    _, owner, permissions, config, sampler = fixture(tmp_path)
    evidence = owner.snapshot()
    review = {"delivery_id": "actual-host-review-identity", "action": '{"tool":"read_file","path":"README.md"}',
        "assessment": {"review_id": "actual-review-1", "status": "approved", "rationale": "Read only."},
        "authorization_version": evidence.authorization_version, "review_context_revision": evidence.review_context_revision, "root_versions": evidence.root_versions}
    try:
        first = prepare(owner, permissions, config, sampler, reviews=(review,))
        delivered = [item for item in first.input if item.review_ids]
        assert len(delivered) == 1 and delivered[0].item["role"] == "developer"
        assert delivered[0].review_ids == (review["delivery_id"],)
        assert "actual-review-1" in text(delivered)
        second = prepare(owner, permissions, config, sampler, history=first.input, cursor=first.cursor, reviews=(review,))
        assert not any(item.review_ids for item in second.input[len(first.input):])
        forged = [AnnotatedItem(deepcopy(item.item)) for item in first.input]
        forged[-1].item["guardian_review_ids"] = [review["delivery_id"]]
        third = prepare(owner, permissions, config, sampler, history=forged, cursor=first.cursor, reviews=(review,))
        assert any(item.review_ids == (review["delivery_id"],) for item in third.input[len(forged):])
    finally:
        await sampler.aclose()


@pytest.mark.asyncio
async def test_retained_prefix_is_immutable_and_only_new_images_are_normalized(tmp_path):
    _, owner, permissions, config, sampler = fixture(tmp_path)
    original = {"type": "message", "role": "user", "id": "actual-parent-native-image", "content": [
        {"type": "input_image", "file_id": "actual-file-image", "detail": "high"}]}
    owner.record_transcript(GuardianTranscriptEntry("user", "", provider_item=original))
    first_image = {"type": "input_image", "image_url": "data:image/png;base64,YQ==", "detail": "high"}
    second_image = {"type": "input_image", "image_url": "data:image/png;base64,Yg==", "detail": "low"}
    try:
        first = prepare(owner, permissions, config, sampler, images=(first_image,))
        frozen = deepcopy(first.input)
        second = prepare(owner, permissions, config, sampler, history=first.input, cursor=first.cursor,
            images=({**first_image, "detail": "original"}, second_image))
        assert second.input[:len(first.input)] == frozen and first.input == frozen
        suffix_images = [part for item in second.input[len(first.input):] if item.item.get("type") == "message"
            for part in item.item["content"] if part["type"] == "input_image"]
        assert suffix_images == [{"type": "input_image", "image_url": second_image["image_url"]}]
        assert original["content"][0]["detail"] == "high" and first_image["detail"] == "high"
    finally:
        await sampler.aclose()


@pytest.mark.asyncio
async def test_rejected_complete_budget_does_not_move_committed_history_or_cursor(tmp_path):
    _, owner, permissions, config, sampler = fixture(tmp_path)
    try:
        first = prepare(owner, permissions, config, sampler)
        frozen = deepcopy(first.input)
        cursor = first.cursor
        sampler.max_input_tokens = 256
        with pytest.raises(GuardianSamplingError, match="complete retained request budget"):
            prepare(owner, permissions, config, sampler, history=first.input, cursor=cursor)
        assert first.input == frozen and first.cursor == cursor
    finally:
        await sampler.aclose()


@pytest.mark.asyncio
async def test_retained_sampler_reuses_real_websocket_with_full_native_prefix_and_original_ids(tmp_path, monkeypatch):
    import asyncio
    import json
    from backend.agent.guardian import connection_pool
    from backend.tests.guardian_http_fixture import classifier_events
    from backend.tests.test_codex_guardian_websocket_pool import endpoint
    monkeypatch.setattr(connection_pool, "INITIAL_WEBSOCKET_CONNECTIONS", 2)
    async def respond(socket, body, index):
        for event in classifier_events(index):
            await socket.send(json.dumps(event))
    async with endpoint(respond) as (url, headers, requests, sockets):
        ctx, owner, permissions, config, unused = fixture(tmp_path)
        await unused.aclose()
        ctx.run_context.llm_turn_context = LLMTurnContext()
        sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_: OpenAIAdapter(replace(cfg.llm, base_url=url, wire_api="responses", proxy_mode="direct")))
        history, cursor = [], None
        try:
            await sampler.prewarm(ctx)
            for index in range(2):
                prepared = prepare(owner, permissions, config, sampler, history=history, cursor=cursor)
                ready = asyncio.get_running_loop().create_future()
                completion = asyncio.create_task(sampler.sample_retained(prepared, ready=ready,
                    instructions="Actual classifier policy", context=ctx))
                assert await ready == "low"
                history = await completion
                assert history is not None
                cursor = prepared.cursor
                owner.record_transcript(GuardianTranscriptEntry("tool run_command result", f"Actual result {index}"))
            first, second = requests
            prefix = [*first["input"], *(event["item"] for event in classifier_events(1) if event["type"] == "response.output_item.done")]
            assert second["input"][:len(prefix)] == prefix
            assert first["include"] == second["include"] == ["reasoning.encrypted_content"]
            assert "previous_response_id" not in second
            assert len(headers) == 2
            assert ctx.run_context.llm_turn_context.usage.input_tokens == 30
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
async def test_disabled_configuration_releases_previously_committed_generation_without_new_work(tmp_path):
    from backend.tests.guardian_http_fixture import classifier_events, http_endpoint, send_sse, until
    from backend.tests.test_codex_guardian_conversation import scene, observe, settled
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(1))
    async with http_endpoint(respond) as server:
        state = scene(tmp_path, server.url)
        try:
            await observe(state, "first", "actual command")
            await settled(state)
            old = state.service.conversation
            assert old.history
            execution = state.current[0].model_execution
            flags = deepcopy(execution.config.feature_flags)
            flags.flags["guardianv2"] = False
            state.current[0] = replace(state.current[0], model_execution=replace(execution,
                config=replace(execution.config, feature_flags=flags)))
            assert state.service.schedule_prewarm(state.current[0]) is None
            await until(lambda: old.worker.done())
            assert not old.history and old not in state.service.backends and not state.service.tasks
            assert len(server.requests) == 1
        finally:
            await state.service.aclose()
