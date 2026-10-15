"""Source Snapshot classifier/cache cases; transports return explicit fixtures."""
from __future__ import annotations
import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.guardian.async_config import GuardianAsyncConfig, resolve_guardian_model_policy
from backend.agent.guardian.async_progress import GuardianScoreProgress
from backend.agent.guardian.async_sampler import GuardianSamplingError, LunaSampler
from backend.agent.guardian.async_service import GuardianAsyncService, ScoreAuthorization, select_async_parent_checkpoint, _scope_for_action
from backend.agent.guardian.context import GuardianParentCheckpoint
from backend.agent.guardian.retained import GuardianEvidenceOwner
from backend.agent.guardian.service import GuardianReviewService
from backend.config import LLMSettings
from backend.config_layers import ConfigLayerStack, ConfigLayer, ConfigLayerSource
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMTurnContext, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.mcp.registry import MCPToolProxy
from backend.mcp.client import MCPToolDef
from backend.tools.registry import ToolRegistry
from backend.tools.command_tool import RunCommandTool
from backend.tests.test_codex_guardian import guardian_context, ControlledRuntime, model_definition


def configured_context(tmp_path, *, options=None, catalog=None, enabled=True):
    ctx, action = guardian_context(tmp_path)
    local = {"enabled": enabled, "review_scope": {"computer_use_only": False, "sandboxed_exec_commands": True}, **(options or {})}
    layers = ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"), {"approvals_reviewer": "auto_review", "features": {"guardianv2": local}}),))
    cfg = replace(ctx.model_execution.config, config_layer_stack=layers, feature_flags=FeatureFlags({"guardian_approval": True, "guardianv2": enabled}))
    info = replace(ctx.model_execution.model_info, extra={"guardian": catalog}) if catalog is not None else ctx.model_execution.model_info
    ctx.model_execution = replace(ctx.model_execution, config=cfg, model_info=info)
    ctx.run_context.model_execution = ctx.model_execution
    return ctx, action


class ControlledSampler:
    instances = []
    result = "low"
    hold = None
    def __init__(self, parent_snapshot, *, conversation_id):
        self.parent = parent_snapshot
        self.calls = []
        self.prewarms = []
        self.closed = False
        type(self).instances.append(self)
    def supports_parent_compaction(self, model_hash):
        return model_hash == "luna-hash"
    async def sample(self, items, **kwargs):
        self.calls.append((deepcopy(items), kwargs))
        if type(self).hold is not None:
            await type(self).hold.wait()
        result = type(self).result
        if isinstance(result, Exception):
            raise result
        return result
    async def prewarm(self, context):
        self.prewarms.append(context)
    async def aclose(self):
        self.closed = True


@pytest.fixture
def sampler():
    ControlledSampler.instances = []
    ControlledSampler.result = "low"
    ControlledSampler.hold = None
    return ControlledSampler


def service_fixture(ctx, sampler):
    owner = GuardianEvidenceOwner()
    current = [ctx]
    service = GuardianAsyncService(owner.snapshot, context_source=lambda: current[0], reviews_source=lambda: (), sampler_factory=sampler)
    registry = ToolRegistry()
    registry.register(RunCommandTool(ctx.artifact_store))
    ctx.tool_registry = registry
    ctx.run_context.guardian_async_service = service
    return owner, current, service, registry


async def settle(service):
    if service.tasks:
        await asyncio.gather(*tuple(service.tasks))
    await asyncio.sleep(0)


@pytest.mark.parametrize("catalog,feature,expected", [
    (None, False, "synchronous"), (None, True, "adaptive"),
    ({"shell": "adaptive"}, False, "adaptive"), ({"shell": "unknown-new-mode"}, True, "unknown"),
    ({"mcp": "adaptive"}, True, "disabled"),
])
def test_source_catalog_policy_precedes_legacy_flag_and_unknown_modes_stay_synchronous(tmp_path, catalog, feature, expected):
    ctx, _ = configured_context(tmp_path, catalog=catalog, enabled=feature)
    policy = resolve_guardian_model_policy(ctx.model_execution.config, ctx.model_execution.model_info)
    assert policy.mode("shell") == expected
    if catalog is not None:
        assert policy.mode("file_changes") == "disabled"


@pytest.mark.parametrize("options,valid", [
    ({}, True), ({"review_threshold": 0}, True), ({"review_threshold": 1}, True),
    ({"max_action_tokens": 99}, False), ({"max_parent_compaction_tokens": 100001}, False),
    ({"review_threshold": float("nan")}, False), ({"async_classifier_conversation_token_limit": 0}, False),
    ({"transcript": {"max_message_entry_tokens": 11000, "max_message_transcript_tokens": 10000}}, False),
])
def test_source_classifier_defaults_and_configuration_errors(tmp_path, options, valid):
    ctx, _ = configured_context(tmp_path, options=options)
    if not valid:
        with pytest.raises(ValueError):
            GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
    else:
        cfg = GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
        assert cfg.classifier_mode == "snapshot" and cfg.max_tool_call_lag == 2
        assert cfg.max_message_entry_tokens == 2000 and cfg.max_tool_entry_tokens == 1000
        assert cfg.include_images and cfg.sources == ("tool_calls", "tool_outputs")


def test_custom_classifier_calibration_and_policy_substitution_follow_source(tmp_path):
    ctx, _ = configured_context(tmp_path, options={"classifier_instructions": "Owner {{ tenant_policy_config }}"})
    cfg = GuardianAsyncConfig.resolve(ctx.model_execution.config, ctx.model_execution.model_info)
    assert cfg.review_threshold == .8
    prompt = cfg.render_instructions({"guardian_policy_config": "policy contains {{ tenant_policy_config }} literally", "guardian_extra_policy": "extra"})
    assert "policy contains {{ tenant_policy_config }} literally" in prompt and "extra" in prompt
    assert prompt.endswith("nothing else.")


def test_source_score_order_failure_ties_and_wrapper_lag_have_exact_provenance():
    progress = GuardianScoreProgress()
    first = progress.observe("first")
    assert progress.publish(0., "authorization", first, 10)
    wrapper = progress.observe("wrapper", wrapper=True)
    nested = progress.observe("nested", parent_wrapper_call_id="wrapper")
    assert progress.inspect("nested").lag == 1
    assert progress.inspect("unattributed").lag == 2
    progress.fail_closed(10)
    assert progress.inspect().action_risk == 1.
    assert not progress.publish(0., "authorization", nested, 10)
    assert progress.publish(0., "new", nested, 11)
    progress.invalidate(wrapper)
    assert not progress.inspect().has_unscored_failure
    progress.mark_oversized("nested", nested + 1)
    assert progress.inspect("nested").oversized
    progress.finish("nested")
    assert not progress.inspect("nested").oversized
    assert progress.inspect().has_unscored_failure


@pytest.mark.parametrize("mode,hash_value,reuse,error,seeds", [
    ("independent", None, True, None, False), ("thread_owned", "luna-hash", True, None, True),
    ("thread_owned", "other", True, "incompatible_compaction", False),
    ("thread_owned", "luna-hash", False, "incompatible_compaction", False),
    ("legacy", "other", True, None, False), ("legacy", "luna-hash", True, None, True),
])
def test_async_checkpoint_compatibility_is_stricter_than_sync_source(mode, hash_value, reuse, error, seeds):
    owner = GuardianEvidenceOwner(context_mode=mode)
    owner.parent_compaction = GuardianParentCheckpoint({"type": "compaction", "id": "cmp", "encrypted_content": "opaque-fixture"}, "origin", hash_value)
    cfg = replace(GuardianAsyncConfig(), reuse_parent_compaction=reuse)
    sampler = ControlledSampler(None, conversation_id="fixture")
    model = replace(model_definition(), comp_hash=hash_value)
    if error:
        with pytest.raises(GuardianSamplingError) as caught:
            select_async_parent_checkpoint(owner.snapshot(), cfg, sampler, model)
        assert caught.value.kind == error
    else:
        assert (select_async_parent_checkpoint(owner.snapshot(), cfg, sampler, model) is not None) is seeds


@pytest.mark.asyncio
async def test_default_false_has_no_network_or_fabricated_cua_from_same_name_mcp(tmp_path, sampler):
    ctx, action = configured_context(tmp_path, enabled=False)
    owner, current, service, registry = service_fixture(ctx, sampler)
    proxy = MCPToolProxy("cua_repl", MCPToolDef(name="js", description="custom user server", input_schema={"type": "object"},
        meta={"_codex_apps": {"connector_id": "connector_openai_browser"}}), None)
    registry.register(proxy)
    try:
        await service.on_tool_start(ToolCallEvent(id="named-js", name=proxy.name, arguments={"code": "any program"}), ctx, registry)
        await settle(service)
        assert not sampler.instances and service.progress.js_executions == 0
        from backend.agent.guardian.action import GuardianAction
        mcp_action = GuardianAction.freeze({"tool": "mcp_tool_call", "server": "cua_repl", "tool_name": "js", "arguments": {}},
            call_id=action.call_id, turn_id=action.turn_id, request_digest=action.request_digest, policy_identity=action.policy_identity, environment_id=None)
        assert _scope_for_action(mcp_action) == "mcp"
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result,approved", [("low", True), ("high", False), (" LOW", False), ("lo", False), ("low now", False)])
async def test_only_exact_source_low_can_reuse_and_later_output_cannot_redefine_it(tmp_path, sampler, result, approved):
    ctx, action = configured_context(tmp_path)
    owner, current, service, registry = service_fixture(ctx, sampler)
    sampler.result = result
    try:
        await service.on_tool_start(ToolCallEvent(id=action.call_id, name="run_command", arguments={"command": "echo changed"}), ctx, registry)
        await settle(service)
        receipt = await service.cached_receipt(action, ctx, required=True)
        assert (receipt is not None) is approved
        assert len(sampler.instances) == 1
        assert "echo changed" in json.dumps(sampler.instances[0].calls[0][0])
        if receipt is not None:
            assert receipt.request_digest == action.request_digest and receipt.assessment is None
        else:
            assert service.progress.inspect().action_risk == 1.
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["local_authorization", "review_context", "root_authorization", "permissions", "model", "luna_catalog", "lag", "failure", "fresh", "cancel"])
async def test_cached_score_rejects_actual_changed_evidence_and_strict_requests(tmp_path, sampler, change):
    ctx, action = configured_context(tmp_path)
    owner, current, service, registry = service_fixture(ctx, sampler)
    luna = [replace(model_definition("gpt-5.6-luna"), comp_hash="luna-old")]
    if change == "luna_catalog":
        runtime = SimpleNamespace(get_model=lambda _provider, _model: luna[0])
        ctx.model_execution = replace(ctx.model_execution, model_runtime=runtime)
    try:
        await service.on_tool_start(ToolCallEvent(id=action.call_id, name="run_command", arguments={"command": "echo allowed"}), ctx, registry)
        await settle(service)
        assert await service.cached_receipt(action, ctx, required=True) is not None
        strict = False
        if change == "local_authorization":
            owner.user_message_revision += 1
        elif change == "review_context":
            owner.review_context_revision += 1
        elif change == "root_authorization":
            service.evidence_source = lambda: owner.snapshot(root_versions=("root", 1, 99, 3, True))
        elif change == "permissions":
            ctx.permission = replace(ctx.permission, mode="bypass")
        elif change == "model":
            ctx.model_execution = replace(ctx.model_execution, model="changed")
        elif change == "luna_catalog":
            luna[0] = replace(luna[0], comp_hash="luna-new")
        elif change == "lag":
            for number in range(3): service.progress.observe(str(number))
        elif change == "failure":
            service.progress.invalidate(service.progress.observe("failed"))
        elif change == "fresh":
            strict = True
        else:
            ctx.cancel_event.set()
        assert await service.cached_receipt(action, ctx, required=True, require_fresh_review=strict) is None
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_newer_authorization_supersedes_inflight_score_and_close_settles_tasks(tmp_path, sampler):
    ctx, action = configured_context(tmp_path)
    owner, current, service, registry = service_fixture(ctx, sampler)
    sampler.hold = asyncio.Event()
    try:
        call = ToolCallEvent(id=action.call_id, name="run_command", arguments={"command": "echo captured"})
        await service.on_tool_start(call, ctx, registry)
        await service.on_tool_start(call, ctx, registry)
        owner.user_message_revision += 1
        sampler.hold.set()
        await settle(service)
        assert service.progress.latest_tool_call == 1
        assert service.progress.inspect().action_risk is None
        assert service.audit[-1]["status"] == "superseded"
        sampler.hold = asyncio.Event()
        await service.on_tool_start(replace(call, id="pending"), ctx, registry)
        await asyncio.sleep(0)
        await service.aclose()
        assert not service.tasks and sampler.instances[0].closed
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exec_command", "request_permissions", "write_stdin"])
async def test_direct_consumers_share_cache_strict_requires_guardian_and_explicit_fresh_returns_sync(tmp_path, sampler, kind):
    ctx, action = configured_context(tmp_path)
    owner, current, asynchronous, registry = service_fixture(ctx, sampler)
    ControlledRuntime.outputs = ['{"outcome":"allow"}']
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    synchronous = GuardianReviewService(owner.snapshot, runtime_factory=ControlledRuntime)
    if kind == "request_permissions":
        action = replace(action, json_payload=json.dumps({"tool": kind, "turn_id": ctx.turn_id, "permissions": {"network": {"enabled": True}}}))
    elif kind == "write_stdin":
        action = replace(action, json_payload=json.dumps({"tool": kind, "session_id": "real-registered-terminal", "chars": "echo next", "cwd": str(tmp_path), "tty": False}))
    try:
        await asynchronous.on_tool_start(ToolCallEvent(id=action.call_id, name="run_command", arguments={"command": "echo small"}), ctx, registry)
        await settle(asynchronous)
        assert (await synchronous.decide(action, ctx, required=True)).status == "approved"
        assert not ControlledRuntime.instances
        assert (await synchronous.decide(action, ctx, required=True, strict_auto_review=True)).status == "approved"
        assert not ControlledRuntime.instances
        assert (await synchronous.decide(action, ctx, required=True, require_synchronous_review=True)).status == "approved"
        assert len(ControlledRuntime.instances) == 1
    finally:
        await asynchronous.aclose()
        await synchronous.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("first,late_error", [("low", False), ("low", True), (" LOW", False)])
async def test_luna_exact_first_delta_owned_drain_and_actual_usage(tmp_path, first, late_error):
    ctx, _ = configured_context(tmp_path)
    ctx.run_context.llm_turn_context = LLMTurnContext()
    requests = []
    responses = []
    events = [{"type": "response.output_text.delta", "delta": first}, {"type": "response.output_text.delta", "delta": "high"},
        {"type": "response.failed", "response": {"error": {"message": "real late failure"}}} if late_error else
        {"type": "response.completed", "response": {"usage": {"input_tokens": 17, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 7}}}}]
    def endpoint(request):
        requests.append(json.loads(request.content))
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())
        responses.append(response)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        def factory(cfg, **_kwargs):
            return OpenAIAdapter(replace(cfg.llm, base_url="https://fixture.invalid/v1", wire_api="responses"), http_client=client)
        sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id, llm_factory=factory)
        try:
            assert await sampler.sample([{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "actual evidence"}]}], instructions="source policy", context=ctx) == first
            if sampler.tasks:
                await asyncio.gather(*tuple(sampler.tasks), return_exceptions=True)
            await asyncio.sleep(0)
            record = ctx.run_context.llm_turn_context.side_call_records[0]
            assert responses[0].is_closed
            if late_error:
                assert record["status"] == "failed" and "real late failure" in record["error"]
                assert record["provider_error"]["response"]["error"]["message"] == "real late failure"
                assert any(item["status"] == "drain_failed" for item in sampler.audit)
            else:
                assert record["status"] == "completed"
                assert ctx.run_context.llm_turn_context.usage.input_tokens == 17
                assert ctx.run_context.llm_turn_context.usage.cache_read_input_tokens == 7
                assert ctx.run_context.llm_turn_context.usage.output_tokens == 3
            assert requests[0]["model"] == "gpt-5.6-luna" and requests[0]["input"][0]["type"] == "additional_tools"
            assert "max_output_tokens" not in requests[0] and "text" not in requests[0]
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_trigger", ["retry", "escalated", "model_requires_without_async"])
async def test_source_retry_and_escalated_exec_force_fresh_even_with_matching_low_score(tmp_path, sampler, fresh_trigger):
    ctx, action = configured_context(tmp_path)
    owner, current, asynchronous, registry = service_fixture(ctx, sampler)
    ControlledRuntime.outputs = ['{"outcome":"allow"}']
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    synchronous = GuardianReviewService(owner.snapshot, runtime_factory=ControlledRuntime)
    try:
        await asynchronous.on_tool_start(ToolCallEvent(id=action.call_id, name="run_command", arguments={"command": "echo regular"}), ctx, registry)
        await settle(asynchronous)
        arguments = {"required": True}
        if fresh_trigger == "retry":
            arguments["retry_reason"] = "Actual sandbox retry"
        elif fresh_trigger == "escalated":
            payload = action.payload
            payload["sandbox_permissions"] = "require_escalated"
            action = replace(action, json_payload=json.dumps(payload))
        else:
            cfg = replace(ctx.model_execution.config, feature_flags=FeatureFlags({"guardian_approval": True, "guardianv2": False}))
            ctx.model_execution = replace(ctx.model_execution, config=cfg)
            arguments["model_requires_review"] = True
        assert (await synchronous.decide(action, ctx, **arguments)).status == "approved"
        assert len(ControlledRuntime.instances) == 1
    finally:
        await asynchronous.aclose()
        await synchronous.aclose()


@pytest.mark.asyncio
async def test_actual_sampler_retry_count_stable_input_identity_and_image_budget(tmp_path):
    ctx, _ = configured_context(tmp_path)
    requests = []
    def endpoint(request):
        requests.append(json.loads(request.content))
        if len(requests) < 3:
            return httpx.Response(429, json={"error": {"message": "controlled retry fixture"}})
        events = [{"type": "response.output_text.delta", "delta": "low"}, {"type": "response.completed", "response": {"usage": {"input_tokens": 12, "output_tokens": 1}}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_kwargs: OpenAIAdapter(replace(cfg.llm, base_url="https://fixture.invalid/v1", wire_api="responses"), http_client=client))
        sampler.max_input_tokens = 5000
        image = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 400000, "detail": "original"}
        try:
            assert await sampler.sample([{"type": "message", "role": "user", "content": [image]}], instructions="policy", context=ctx) == "low"
            if sampler.tasks:
                await asyncio.gather(*tuple(sampler.tasks))
            assert len(requests) == 3
            assert all(request["input"] == requests[0]["input"] for request in requests)
            assert len({request["client_metadata"]["turn_id"] for request in requests}) == 1
            assert "detail" not in requests[0]["input"][-1]["content"][0]
            assert image["detail"] == "original"
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
async def test_source_required_model_requirement_is_produced_by_actual_layer_stack(tmp_path, sampler):
    from backend.config_requirements import ConfigRequirements

    ctx, action = configured_context(tmp_path)
    requirements = ConfigRequirements(raw={"auto_review": {"required_on_models": ["parent"]}})
    stack = replace(ctx.model_execution.config.config_layer_stack, requirements=requirements)
    cfg = replace(ctx.model_execution.config, config_layer_stack=stack)
    ctx.model_execution = replace(ctx.model_execution, config=cfg)
    owner, current, asynchronous, registry = service_fixture(ctx, sampler)
    ControlledRuntime.outputs = ['{"outcome":"allow"}']
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    synchronous = GuardianReviewService(owner.snapshot, runtime_factory=ControlledRuntime)
    try:
        assert requirements.auto_review_required_for_model("parent")
        assert resolve_guardian_model_policy(cfg, ctx.model_execution.model_info).mode("shell") == "synchronous"
        await asynchronous.on_tool_start(ToolCallEvent(id=action.call_id, name="run_command", arguments={"command": "echo model protected"}), ctx, registry)
        await settle(asynchronous)
        assert not sampler.instances
        assert (await synchronous.decide(action, ctx)).status == "approved"
        assert len(ControlledRuntime.instances) == 1
    finally:
        await asynchronous.aclose()
        await synchronous.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,expected", [("default", None), ("flex", "flex"), ("priority", "priority"), ("not-in-catalog", None)])
async def test_classifier_regular_api_tier_uses_actual_model_request_policy(tmp_path, selected, expected):
    from backend.llm.model_request_policy import ModelRequestPolicy

    ctx, _ = configured_context(tmp_path)
    policy = ModelRequestPolicy(service_tier=selected, service_tiers=("priority",))
    ctx.model_execution = replace(ctx.model_execution, config=replace(ctx.model_execution.config, model_request_policy=policy))
    requests = []
    def endpoint(request):
        requests.append(json.loads(request.content))
        events = [{"type": "response.output_text.delta", "delta": "low"}, {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_kwargs: OpenAIAdapter(replace(cfg.llm, base_url="https://fixture.invalid/v1", wire_api="responses"), http_client=client))
        try:
            assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
            if sampler.tasks:
                await asyncio.gather(*tuple(sampler.tasks))
            assert requests[0].get("service_tier") == expected
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,auth_mode,expected", [
    ({}, "chatgpt", True), ({"supports_codex_backend_routes": False}, "chatgpt", False),
    ({"requires_openai_auth": False}, "chatgpt", False), ({"env_key": "EXPLICIT_SOURCE_ENV_KEY"}, "chatgpt", False),
    ({"experimental_bearer_token": "explicit-fixture"}, "chatgpt", False),
    ({"auth": {}}, "chatgpt", False), ({"aws": {}}, "chatgpt", False),
    ({}, None, False), ({}, "headers", True), ({}, "agentIdentity", True),
])
async def test_actual_registered_classifier_route_requires_trusted_auth_and_all_source_capabilities(tmp_path, overrides, auth_mode, expected):
    from backend.tests.test_history_notes_service import notes_runtime
    from backend.agent.guardian.async_sampler import classifier_backend_route
    from backend.llm.model_request_policy import ModelRequestPolicy

    ctx, _ = configured_context(tmp_path)
    runtime, model, storage = notes_runtime(tmp_path, "http://127.0.0.1", auth_mode=auth_mode)
    profile = runtime._extension_providers[model.provider]
    profile.update({"supports_codex_backend_routes": True, "requires_openai_auth": True, **overrides})
    config = replace(ctx.model_execution.config, model_request_policy=ModelRequestPolicy(service_tier="priority", service_tiers=("priority",)))
    execution = replace(ctx.model_execution, provider=model.provider, model=model.id, model_info=model, model_runtime=runtime, config=config)
    assert classifier_backend_route(execution) is expected
    requests = []
    def endpoint(request):
        requests.append((json.loads(request.content), dict(request.headers)))
        events = [{"type": "response.output_text.delta", "delta": "low"}, {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        sampler = LunaSampler(execution, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_kwargs: OpenAIAdapter(replace(cfg.llm, base_url="https://fixture.invalid/v1", wire_api="responses"), http_client=client))
        try:
            ctx.metadata["parent_response_id"] = "actual-parent-response"
            assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
            if sampler.tasks:
                await asyncio.gather(*tuple(sampler.tasks))
            body, headers = requests[0]
            assert (headers.get("x-codex-guardian") == "classifier") is expected
            assert body.get("service_tier") == (None if expected else "priority")
            assert body["client_metadata"].get("parent_response_id") == ("actual-parent-response" if expected else None)
        finally:
            await sampler.aclose()
