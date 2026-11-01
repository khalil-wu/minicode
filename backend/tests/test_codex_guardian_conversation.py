from __future__ import annotations

import asyncio
import gc
import json
import weakref
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.guardian.async_conversation import ConversationBackend, ConversationRequest
from backend.agent.guardian.async_config import GuardianAsyncConfig
from backend.agent.guardian.async_request import CollectedAsyncEvidence, prepare_retained
from backend.agent.guardian.async_sampler import GuardianSamplingError, LunaSampler
from backend.agent.guardian.async_service import GuardianAsyncService, ScoreAuthorization
from backend.agent.guardian.service import GuardianReviewService
from backend.agent.guardian.tool_approval import review_tool_request
from backend.agent.final_tool_request import FinalExecutableToolRequest
from backend.agent.guardian.prompt import GuardianPermissionEvidence
from backend.agent.guardian.retained import GuardianEvidenceOwner, GuardianTranscriptEntry
from backend.llm.base import LLMTurnContext, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.exec_policy import ExecPolicyDecision, FrozenExecCommand
from backend.sandbox.policy import SandboxPolicy
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry
from backend.tests.guardian_http_fixture import classifier_events, http_endpoint, send_sse, until
from backend.tests.test_codex_guardian_async import configured_context
from backend.tests.test_codex_guardian import controlled_guardian


def scene(tmp_path, url, *, limit=100000, comparison=False, decisions_factory=None):
    ctx, _ = configured_context(tmp_path, options={"async_classifier_mode": "conversation",
        "async_classifier_conversation_token_limit": limit})
    if comparison:
        ctx.model_execution.config.feature_flags.flags["guardianv2_decisions_comparison"] = True
    ctx.run_context.llm_turn_context = LLMTurnContext()
    owner = GuardianEvidenceOwner()
    owner.record_accepted_message("user", "REAL_RETAINED_CONSTRAINT", turn_id=ctx.turn_id,
        message_id="actual-user-input", acceptance_order=owner.reserve_order())
    current, samplers, reviews = [ctx], [], []
    def factory(execution, **kwargs):
        sampler = LunaSampler(execution, **kwargs, llm_factory=lambda cfg, **_: OpenAIAdapter(
            replace(cfg.llm, base_url=url + "/v1", wire_api="responses", proxy_mode="direct")))
        # A Source cold/cooldown HTTP lease; real WS is covered separately.
        sampler.connections.retry_after = float("inf")
        samplers.append(sampler)
        return sampler
    service = GuardianAsyncService(owner.snapshot, context_source=lambda: current[0],
        reviews_source=lambda: reviews, sampler_factory=factory, decisions_factory=decisions_factory)
    registry = ToolRegistry()
    registry.register(RunCommandTool(ctx.artifact_store))
    ctx.run_context.guardian_async_service = service
    return SimpleNamespace(ctx=ctx, owner=owner, current=current, service=service, registry=registry,
        samplers=samplers, reviews=reviews)


async def observe(fixture, call_id, command):
    ctx = replace(fixture.current[0], tool_call_id=call_id)
    fixture.current[0] = ctx
    await fixture.service.on_tool_start(ToolCallEvent(id=call_id, name="run_command", arguments={"command": command}), ctx, fixture.registry)


async def settled(fixture):
    await until(lambda: fixture.service.conversation.outstanding == 0 and not fixture.service.tasks)


def wire_text(items):
    return "\n".join(part["text"] for item in items if item.get("type") == "message"
        for part in item["content"] if part["type"] in {"input_text", "output_text"})


def captured_request(request, ctx, workspace):
    """Freeze the executable command the way final authorization does before review."""
    policy = SandboxPolicy.workspace_default(workspace)
    argv = ["sh", "-c", str(request.arguments["command"])]
    command = FrozenExecCommand.capture(request, argv=argv, policy_argv=argv, cwd=workspace, configured_shell=Path("sh"),
        permission=ctx.permission, profile=policy.permission_profile, resolved_profile=policy.resolve(cwd=workspace),
        windows_sandbox_level="disabled", sandbox_permissions="use_default", prefix_rule=None,
        environment_id=ctx.environment_id or "local", policy=policy, additional_permissions_present=False)
    return request.with_exec_policy(ExecPolicyDecision(command, "needs_approval", None, False, None, (tuple(argv),), "{}"))


@pytest.mark.asyncio
async def test_two_real_http_requests_commit_native_prefix_ids_and_only_new_transcript(tmp_path):
    release = asyncio.Event()
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index), gate=release if request.index == 1 else None, reader=reader)
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        try:
            await observe(fixture, "call-1", "first-action")
            await until(lambda: fixture.service.progress.score is not None)
            backend = fixture.service.conversation
            assert backend.completed_reviews == 0 and backend.outstanding == 1
            fixture.owner.record_transcript(GuardianTranscriptEntry("tool run_command result", "ACTUAL_NEW_RESULT"))
            await observe(fixture, "call-2", "second-action")
            assert backend.outstanding == 2 and len(server.requests) == 1
            release.set()
            await settled(fixture)
            first, second = (request.body for request in server.requests)
            expected_prefix = [*first["input"], *(event["item"] for event in classifier_events(1) if event["type"] == "response.output_item.done")]
            assert second["input"][:len(expected_prefix)] == expected_prefix
            suffix = second["input"][len(expected_prefix):]
            assert "TRANSCRIPT DELTA START" in wire_text(suffix)
            assert "[2] tool run_command result: ACTUAL_NEW_RESULT" in wire_text(suffix)
            assert "REAL_RETAINED_CONSTRAINT" not in wire_text(suffix)
            assert '"command": "second-action"' in wire_text(suffix) and '"command": "first-action"' not in wire_text(suffix)
            assert all(request["include"] == ["reasoning.encrypted_content"] for request in (first, second))
            assert all("previous_response_id" not in request for request in (first, second))
            assert all(item.get("id") for request in (first, second) for item in request["input"])
            assert backend.completed_reviews == 2
            usage = fixture.ctx.run_context.llm_turn_context.usage
            assert (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens) == (30, 4, 6)
            assert all(record["transports"] == ["http"] and record["status"] == "completed"
                for record in fixture.ctx.run_context.llm_turn_context.side_call_records)
        finally:
            release.set()
            await fixture.service.aclose()


@pytest.mark.asyncio
async def test_real_conversation_early_score_reaches_frozen_approval_and_revocation_uses_current_sync_evidence(tmp_path, controlled_guardian):
    release = asyncio.Event()
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index), gate=release, reader=reader)

    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        synchronous = GuardianReviewService(fixture.owner.snapshot, runtime_factory=controlled_guardian)
        fixture.ctx.run_context.guardian_review_service = synchronous
        events = []
        async def emit(kind, data):
            events.append((kind, data))

        call = ToolCallEvent(id="actually-reviewed-call", name="run_command", arguments={"command": "echo approval-boundary"})
        request = captured_request(FinalExecutableToolRequest.freeze(call, policy_identity=fixture.registry.get_tool(call.name).policy_identity),
            fixture.ctx, tmp_path)
        ctx = replace(fixture.ctx, tool_call_id=call.id, authorization_digest=request.digest,
            authorization_policy_identity=request.policy_identity, emit_event=emit)
        fixture.current[0] = ctx
        try:
            await fixture.service.on_tool_start(call, ctx, fixture.registry)
            await until(lambda: fixture.service.progress.score is not None)
            backend = fixture.service.conversation
            assert backend.outstanding == 1 and backend.completed_reviews == 0
            approved = await review_tool_request(request, ctx, fixture.registry)
            assert approved["action"] == "approve" and approved["guardian_status"] == "approved"
            assert approved["request_digest"] == request.digest and approved["policy_identity"] == request.policy_identity
            assert not controlled_guardian.instances
            assert not events, "A cached classifier score must not fabricate a synchronous reviewer activity"

            fixture.owner.record_accepted_message("user", "NEW_REAL_REVOCATION: do not run this command",
                turn_id=ctx.turn_id, message_id="actual-steered-revocation", acceptance_order=fixture.owner.reserve_order())
            controlled_guardian.outputs = [json.dumps({"outcome": "deny", "risk_level": "high",
                "user_authorization": "low", "rationale": "The latest user instruction revokes this action."})]
            denied = await review_tool_request(request, ctx, fixture.registry)
            assert denied["action"] == "reject" and denied["guardian_status"] == "denied"
            assert denied["request_digest"] == request.digest
            assert len(controlled_guardian.instances) == 1
            assert "NEW_REAL_REVOCATION" in controlled_guardian.instances[0].prompts[0].text
            assessments = [data for kind, data in events if kind == "guardian_assessment"]
            assert [item["status"] for item in assessments] == ["in_progress", "denied"]
            assert all(item["target_item_id"] == call.id and item["turn_id"] == ctx.turn_id for item in assessments)

            release.set()
            await settled(fixture)
            assert len(server.requests) == 1 and server.requests[0].body["model"] == "gpt-5.6-luna"
            assert backend.completed_reviews == 0 and not backend.history
            assert fixture.ctx.run_context.llm_turn_context.usage.input_tokens == 10
        finally:
            release.set()
            await fixture.service.aclose()
            await synchronous.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatch", "late_error", "eof", "invalid_item", "oversized_item"])
async def test_early_low_is_not_revised_and_unfinished_or_invalid_history_never_commits(tmp_path, failure):
    async def respond(request, reader, writer):
        if request.index != 1:
            return await send_sse(writer, classifier_events(request.index))
        events = classifier_events(1)
        if failure == "mismatch":
            events = classifier_events(1, final="high")
        elif failure == "late_error":
            events = [events[0], {"type": "response.failed", "response": {"error": {"message": "actual late error"}}}]
        elif failure == "eof":
            events = events[:1]
        elif failure == "invalid_item":
            events.insert(1, {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_actual", "name": "unexpected", "arguments": "{}"}})
        else:
            events[1]["item"]["encrypted_content"] = "opaque" * 10000
        await send_sse(writer, events)
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        try:
            await observe(fixture, "first", "first-action")
            await settled(fixture)
            backend = fixture.service.conversation
            assert fixture.service.progress.inspect().action_risk == 0.0 and backend.completed_reviews == 0 and not backend.history
            await observe(fixture, "second", "second-action")
            await settled(fixture)
            assert backend.completed_reviews == 1 and len(server.requests) == 2
            second = server.requests[1].body["input"]
            assert second[0]["type"] == "additional_tools" and "TRANSCRIPT DELTA START" not in wire_text(second)
            assert not any(item.get("id") in {"msg_actual_1", "rs_actual_1", "fc_actual"} for item in second)
            if failure == "late_error":
                record = fixture.ctx.run_context.llm_turn_context.side_call_records[0]
                assert record["status"] == "failed" and "actual late error" in record["error"]
            assert fixture.ctx.run_context.llm_turn_context.usage.input_tokens == (20 if failure in {"late_error", "eof"} else 30)
        finally:
            await fixture.service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,reused", [(1, False), (100000, True)])
async def test_soft_reset_limit_rebuilds_complete_current_evidence(tmp_path, limit, reused):
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index))
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url, limit=limit)
        try:
            for index in range(2):
                await observe(fixture, str(index), f"action-{index}")
                await settled(fixture)
            second = server.requests[1].body["input"]
            assert any(item.get("id") == "msg_actual_1" for item in second) is reused
            assert ("TRANSCRIPT DELTA START" in wire_text(second)) is reused
            assert "REAL_RETAINED_CONSTRAINT" in wire_text(second)
        finally:
            await fixture.service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["authorization", "history_version", "root", "instructions"])
async def test_real_authorization_history_root_and_instruction_changes_rebuild_retained_context(tmp_path, change):
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index))
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        try:
            await observe(fixture, "first", "first-action")
            await settled(fixture)
            if change == "authorization":
                fixture.owner.record_accepted_message("user", "NEW_USER_REVOCATION", turn_id="next-turn", message_id="next-user",
                    acceptance_order=fixture.owner.reserve_order())
            elif change == "history_version":
                fixture.owner.history_version += 1
            elif change == "root":
                fixture.service.evidence_source = lambda: replace(fixture.owner.snapshot(), root_versions=("root", 2, 3, 4, True), root_messages=(("user", "ROOT_NEW_CONSTRAINT"),))
            else:
                ctx = fixture.current[0]
                config = deepcopy(ctx.model_execution.config)
                config.config_layer_stack.layers[-1].config["guardian_extra_policy"] = "NEW_HOST_POLICY"
                fixture.current[0] = replace(ctx, model_execution=replace(ctx.model_execution, config=config))
            await observe(fixture, "second", "second-action")
            await settled(fixture)
            second = server.requests[1].body["input"]
            assert not any(item.get("id") == "msg_actual_1" for item in second)
            assert "TRANSCRIPT DELTA START" not in wire_text(second)
            if change == "authorization":
                assert "NEW_USER_REVOCATION" in wire_text(second)
            elif change == "root":
                assert "ROOT_NEW_CONSTRAINT" in wire_text(second)
            elif change == "instructions":
                assert "NEW_HOST_POLICY" in wire_text(second)
        finally:
            await fixture.service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_host_close_cancels_real_http_stream_and_releases_all_conversation_owners(tmp_path, published):
    release = asyncio.Event()
    async def respond(request, reader, writer):
        if published:
            await send_sse(writer, classifier_events(1), gate=release, reader=reader)
        else:
            await reader.read()
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        await observe(fixture, "cancelled", "cancelled-action")
        await until(lambda: bool(server.requests))
        if published:
            await until(lambda: fixture.service.progress.score is not None)
        backend = fixture.service.conversation
        assert backend.outstanding == 1 and not server.requests[0].closed
        await fixture.service.aclose()
        await until(lambda: server.requests[0].closed)
        assert backend.worker.done() and backend.outstanding == 0 and not backend.history
        assert not fixture.service.backends and not fixture.service.tasks and fixture.service.conversation is None
        sampler = fixture.samplers[0]
        assert sampler.connections.classifications._value == sampler.connections.sockets._value == 16
        assert not sampler.connections.active


@pytest.mark.asyncio
async def test_replaced_generation_releases_old_history_and_collected_backend_after_worker_exit(tmp_path):
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index))
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        try:
            await observe(fixture, "first", "first-action")
            await settled(fixture)
            backend = fixture.service.conversation
            assert backend.history
            old = weakref.ref(backend)
            ctx = fixture.current[0]
            fixture.current[0] = replace(ctx, model_execution=replace(ctx.model_execution,
                thinking_level="medium"))
            await observe(fixture, "second", "second-action")
            await settled(fixture)
            await until(lambda: backend.worker.done())
            assert not backend.history and backend not in fixture.service.backends
            del backend
            gc.collect()
            assert old() is None
            assert len(fixture.service.backends) == 1
        finally:
            await fixture.service.aclose()


@pytest.mark.asyncio
async def test_reservations_preserve_preparation_order_and_bound_active_plus_queued_requests(tmp_path):
    async def respond(request, reader, writer):
        await send_sse(writer, classifier_events(request.index))
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        sampler = fixture.service.sampler_factory(fixture.ctx.model_execution, conversation_id=fixture.ctx.conversation_id)
        backend = ConversationBackend(sampler, fixture.service._is_current)
        reservations = [backend.reserve() for _ in range(16)]
        try:
            with pytest.raises(GuardianSamplingError, match="queue is full"):
                backend.reserve()
            evidence = fixture.owner.snapshot()
            config = GuardianAsyncConfig.resolve(fixture.ctx.model_execution.config, fixture.ctx.model_execution.model_info)
            permissions = GuardianPermissionEvidence.from_policy(fixture.ctx.sandbox_policy, cwd=fixture.ctx.workspace_root, environment_id=fixture.ctx.environment_id)
            scores = []
            for index in (2, 1, 0):
                ready = asyncio.get_running_loop().create_future()
                scores.append(ready)
                context = replace(fixture.ctx, turn_id=f"turn-{index}")
                reservations[index].submit(ConversationRequest(CollectedAsyncEvidence(evidence,
                    json.dumps({"tool": "exec_command", "label": index}), permissions, config), "actual policy", None,
                    context, ScoreAuthorization.capture(evidence, fixture.ctx), config.conversation_token_limit, ready))
            for reservation in reservations[3:]:
                reservation.abandon()
            assert await asyncio.gather(*scores) == ["low", "low", "low"]
            await until(lambda: backend.outstanding == 0)
            assert [request.body["client_metadata"]["parent_turn_id"] for request in server.requests] == ["turn-0", "turn-1", "turn-2"]
            assert backend.completed_reviews == 3
        finally:
            for reservation in reservations:
                reservation.abandon()
            await backend.aclose()
            await sampler.aclose()
            await fixture.service.aclose()


@pytest.mark.asyncio
async def test_queued_stale_authorization_is_rejected_without_any_http_request(tmp_path):
    async def respond(request, reader, writer):
        raise AssertionError("stale queued request must not reach HTTP")
    async with http_endpoint(respond) as server:
        fixture = scene(tmp_path, server.url)
        sampler = fixture.service.sampler_factory(fixture.ctx.model_execution, conversation_id=fixture.ctx.conversation_id)
        backend = ConversationBackend(sampler, fixture.service._is_current)
        blocker, reservation = backend.reserve(), backend.reserve()
        evidence = fixture.owner.snapshot()
        config = GuardianAsyncConfig.resolve(fixture.ctx.model_execution.config, fixture.ctx.model_execution.model_info)
        permissions = GuardianPermissionEvidence.from_policy(fixture.ctx.sandbox_policy, cwd=fixture.ctx.workspace_root, environment_id=fixture.ctx.environment_id)
        ready = asyncio.get_running_loop().create_future()
        reservation.submit(ConversationRequest(CollectedAsyncEvidence(evidence, "{}", permissions, config), "policy", None,
            fixture.ctx, ScoreAuthorization.capture(evidence, fixture.ctx), config.conversation_token_limit, ready))
        fixture.owner.user_message_revision += 1
        blocker.abandon()
        try:
            with pytest.raises(GuardianSamplingError, match="superseded"):
                await ready
            assert not server.requests and not backend.history
        finally:
            await backend.aclose()
            await sampler.aclose()
            await fixture.service.aclose()
