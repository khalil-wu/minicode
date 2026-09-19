from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.lifecycle_generation import LifecycleGenerationState
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.extensions.lifecycle_observer import ExtensionLifecycleObserver
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.capabilities import capabilities_from_openai_settings
from backend.llm.model_runtime import ModelDefinition
from backend.permissions.checker import PermissionChecker
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import SessionAgentRunnerMixin


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))


class Model(LLMAdapter):
    def __init__(self, settings, behavior):
        self._settings = settings
        self.behavior = behavior
        self.inputs = []
        self.closed = False

    @property
    def capabilities(self):
        return capabilities_from_openai_settings(self._settings, provider=self._settings.provider)

    def apply_reasoning_policy(self, policy):
        super().apply_reasoning_policy(policy)
        self._settings = replace(self._settings, reasoning_effort=policy.wire_level)

    async def simple_chat(self, messages, **kwargs):
        raise AssertionError("Unexpected auxiliary model call")

    async def stream_chat(self, messages, tools=None, metadata=None):
        assert not self.closed
        self.inputs.append(messages)
        call = await self.behavior(self, messages)
        if call is not None:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[call], tool_calls_committed=True)
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Verified the requested model ownership.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def aclose(self):
        self.closed = True


class ModelCatalog:
    def get_model(self, provider, name):
        if provider != "custom" or name not in {"model-a", "model-b"}:
            return None
        return ModelDefinition(provider=provider, id=name, name=name, api="openai-completions",
            base_url="https://example.invalid/v1", reasoning=True,
            context_window=96000 if name == "model-a" else 32768, max_tokens=4096,
            reasoning_effort_levels=("low", "high"), default_reasoning_effort="low")

    def get_models(self, provider):
        return tuple(self.get_model(provider, name) for name in ("model-a", "model-b"))

    async def refresh_oauth_credentials(self, provider):
        pass

    async def refresh_provider_auth(self, provider):
        pass


class Runner:
    def __init__(self):
        self.events = []

    def bind_actions(self, actions):
        self.actions = actions

    def bind_context_actions(self, actions):
        self.context_actions = actions

    async def emit(self, event):
        self.events.append(event)


class Host(SessionAgentRunnerMixin):
    run_manager = None

    def _model_registry_for_conversation(self, conversation_id):
        return None


class InspectModel(BaseTool):
    name = "inspect_model"
    read_only = True

    def __init__(self):
        self.observations = []

    def get_schema(self):
        return ToolSchema(self.name, "Inspect the model that issued this call", {"type": "object", "properties": {}})

    async def execute(self, args, context=None):
        snapshot = context.model_execution
        observation = (context.llm.model_id(), snapshot.model, snapshot.thinking_level, snapshot.config.token_budget.total)
        self.observations.append(observation)
        return ToolResult(json.dumps(observation))


def setup(tmp_path, monkeypatch, behavior, extra_tools=()):
    settings = LLMSettings(api_key="fixture", provider="custom", base_url="https://example.invalid/v1",
        model="model-a", reasoning_effort="low", reasoning_effort_levels=("low", "high"), model_instructions="INSTRUCTION-model-a")
    config = AppConfig(llm=settings, token_budget=TokenBudget(total=96000, response_reserve=4096),
        agent=AgentSettings(max_iterations=6, max_turn_seconds=20, stream_max_attempts=0))
    model = Model(settings, behavior)
    catalog = ModelCatalog()
    created = []

    def build(config, *, provider_override, model_override, model_runtime):
        assert model_runtime is catalog
        adapter = Model(replace(config.llm, provider=provider_override, model=model_override,
            model_instructions=f"INSTRUCTION-{model_override}"), behavior)
        created.append(adapter)
        return adapter

    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", build)
    inspector = InspectModel()
    registry = ToolRegistry()
    for tool in [inspector, ToolExecTool(), ToolWaitTool(), *extra_tools]:
        registry.register(tool)
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    owner = RunContext(agent_runtime=runtime, execution_journal=ExecutionJournal("model", base_dir=tmp_path / "journal"),
        model_execution=ModelExecutionSnapshot(config=config, llm=model, provider="custom", model="model-a", thinking_level="low", model_runtime=catalog))
    builder = ContextBuilder(llm=model, token_budget=config.token_budget)
    session = AgentSession(llm=model, tool_registry=registry, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=config.agent,
        token_budget=config.token_budget, context_builder=builder)
    runner, host = Runner(), Host()
    host.conversation_repo = SimpleNamespace(update_model_selection=Mock())
    generation = LifecycleGenerationState("model-conv")
    generation.update(runtime=runner, model_runtime=catalog)
    host._extension_runtime_states = {"model-conv": generation}
    host._bind_lifecycle_runtime_host_actions(runner, conversation=SimpleNamespace(id="model-conv"), tool_registry=registry,
        run_metadata={}, run_context_builder=builder, run_llm=model, cancel_event=None,
        model_runtime=catalog, agent_session=session, run_context=owner)
    return SimpleNamespace(config=config, model=model, created=created, inspector=inspector, runtime=runtime,
        owner=owner, session=session, runner=runner, host=host, builder=builder)


async def execute(fixture, tmp_path, *, conversation_id="model-conv"):
    state = AgentState(user_message="Verify model ownership", workspace_root=tmp_path, conversation_id=conversation_id)
    try:
        events = [event async for event in QueryEngine().submit(QuerySubmission(session=fixture.session, state=state,
            user_message=state.user_message, runtime=AgentLoopSessionContext(workspace_root=tmp_path,
                session_id="model-session", run_context=fixture.owner)))]
        assert state.terminal_status == "completed", (state.stopped_reason, events)
    finally:
        await fixture.session.aclose()
        fixture.runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_live_selection_changes_next_request_and_keeps_issued_tools_bound(tmp_path, monkeypatch):
    fixture = None

    async def behavior(model, messages):
        if model.model_id() == "model-a":
            assert fixture.runner.actions["set_thinking_level"]("high") == "high"
            assert await fixture.runner.actions["set_model"]({"provider": "custom", "id": "model-b"})
            assert fixture.runner.context_actions["model"]().id == "model-a"
            observer = ExtensionLifecycleObserver(None, "", metadata={"model": "stale"}, run_context=fixture.owner)
            assert observer._assistant_defaults()["model"] == "model-a"
            return ToolCallEvent(id="from-a", name="inspect_model", arguments={})
        assert any("INSTRUCTION-model-b" in message.content for message in messages)
        if len(model.inputs) == 1:
            return ToolCallEvent(id="from-b", name="inspect_model", arguments={})
        return None

    fixture = setup(tmp_path, monkeypatch, behavior)
    await execute(fixture, tmp_path)
    assert fixture.inspector.observations == [("model-a", "model-a", "low", 96000), ("model-b", "model-b", "high", 32768)]
    assert fixture.model._settings.reasoning_effort == "low"
    assert len(fixture.model.inputs) == 1
    assert [len(model.inputs) for model in fixture.created] == [0, 2]
    fixture.host.conversation_repo.update_model_selection.assert_called_with("model-conv", provider="custom", model="model-b", reasoning_effort="high")


@pytest.mark.asyncio
async def test_yielded_cell_keeps_its_model_when_later_request_switches(tmp_path, monkeypatch):
    ready = asyncio.Event()

    class Gate(InspectModel):
        name = "gate"

        async def execute(self, args, context=None):
            await ready.wait()
            return ToolResult("ready")

    fixture = None

    async def behavior(model, messages):
        if model.model_id() == "model-a":
            await fixture.runner.actions["set_model"]({"provider": "custom", "id": "model-b"})
            return ToolCallEvent(id="cell-from-a", name="tool_exec", arguments={
                "code": "await tools.gate({}); text((await tools.inspect_model({})).content);", "yield_time_ms": 0})
        if len(model.inputs) == 1:
            content = next(message for message in reversed(messages) if message.role == "tool").content
            report = json.JSONDecoder().raw_decode(content[content.index("{"):])[0]
            assert report["status"] == "running"
            ready.set()
            return ToolCallEvent(id="join-from-b", name="tool_wait", arguments={"cell_id": report["cell_id"], "yield_time_ms": 10000})
        return None

    fixture = setup(tmp_path, monkeypatch, behavior, [Gate()])
    await execute(fixture, tmp_path)
    assert fixture.inspector.observations == [("model-a", "model-a", "low", 96000)]
    assert fixture.owner.active_model_execution.model == "model-b"


@pytest.mark.asyncio
async def test_sdk_tools_receive_supplied_config_without_global_reload(tmp_path, monkeypatch):
    from backend.sdk import query

    async def behavior(model, messages):
        if len(model.inputs) == 1:
            config.token_budget = TokenBudget(total=8192, response_reserve=4096)
            return ToolCallEvent(id="sdk-model", name="inspect_model", arguments={})
        return None

    settings = LLMSettings(api_key="fixture", provider="custom", model="sdk-model", reasoning_effort="high", reasoning_effort_levels=("low", "high"))
    config = AppConfig(llm=settings, token_budget=TokenBudget(total=65536, response_reserve=4096), agent=AgentSettings(max_iterations=3))
    model, inspector = Model(settings, behavior), InspectModel()
    monkeypatch.setattr("backend.sdk.load_config", Mock(side_effect=AssertionError("Supplied config must be used")))
    events = [event async for event in query("Check configured model", config=config, llm=model, tools=[inspector], tool_registry=ToolRegistry(),
        workspace_root=tmp_path, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))]
    assert inspector.observations == [("sdk-model", "sdk-model", "high", 65536)]
    assert any(event.type == "done" for event in events)


def test_explicit_provider_does_not_borrow_stale_image_credentials():
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.tools.image_generation_tool import GenerateImageTool

    model = Model(LLMSettings(api_key="fixture", provider="custom", model="old"), None)
    snapshot = ModelExecutionSnapshot(config=AppConfig(llm=model._settings), llm=model, provider="extension-provider", model="new")
    context = ToolExecutionContext(permission=PermissionContext(), model_execution=snapshot,
        llm=model, metadata={"provider": "custom"})
    assert GenerateImageTool._provider_for_context(context) is None


@pytest.mark.asyncio
async def test_inherited_snapshot_survives_model_catalog_refresh():
    from backend.tools.subagent_support import _resolve_subagent_llm

    model = Model(LLMSettings(api_key="fixture", provider="custom", model="captured", reasoning_effort="high"), None)
    catalog = SimpleNamespace(get_model=Mock(side_effect=AssertionError("An inherited selection does not consult the refreshed catalog")))
    snapshot = ModelExecutionSnapshot(config=AppConfig(llm=model._settings), llm=model, provider="custom", model="captured",
        thinking_level="high", model_runtime=catalog)
    result = await _resolve_subagent_llm(None, parent_metadata={}, run_context=RunContext(model_execution=snapshot), agent_type="general-purpose")
    assert result.llm is model and result.model == "captured" and result.effort == "high"
    assert result.config is snapshot.config


def test_sdk_capture_preserves_canonical_effort_mapping():
    from backend.llm.provider_contracts import ReasoningPolicy

    model = Model(LLMSettings(api_key="fixture", provider="custom", model="mapped"), None)
    model.apply_reasoning_policy(ReasoningPolicy(level="xhigh", wire_level="high"))
    snapshot = ModelExecutionSnapshot.capture(AppConfig(llm=model._settings), model)
    assert snapshot.thinking_level == "xhigh"


@pytest.mark.asyncio
async def test_responses_wire_keeps_inflight_effort_and_uses_new_model_next(tmp_path, monkeypatch):
    from backend.llm.openai_adapter import OpenAIAdapter

    fixture = setup(tmp_path, monkeypatch, None)
    requests = []

    async def endpoint(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            fixture.runner.actions["set_thinking_level"]("high")
            await fixture.runner.actions["set_model"]({"id": "model-b"})
            output = [{"type": "function_call", "id": "native-item", "call_id": "native-tool",
                "name": "inspect_model", "arguments": "{}", "status": "completed"}]
            text = ""
        else:
            output, text = [], "Verified native model selection."
        event = {"type": "response.completed", "response": {"id": f"response-{len(requests)}", "status": "completed",
            "output": output, "output_text": text, "usage": {"input_tokens": 100, "output_tokens": 20}}}
        return httpx.Response(200, content=("data: " + json.dumps(event) + "\n\n").encode(), headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        settings = replace(fixture.config.llm, wire_api="responses")
        original = OpenAIAdapter(settings, http_client=client)
        config = replace(fixture.config, llm=settings)
        fixture.owner.model_execution = replace(fixture.owner.model_execution, llm=original, config=config)
        fixture.session.llm = original
        fixture.builder.bind_llm(original)

        def build(config, *, model_override, provider_override, model_runtime):
            return OpenAIAdapter(replace(config.llm, model=model_override, provider=provider_override,
                model_instructions=f"INSTRUCTION-{model_override}"), http_client=client)

        monkeypatch.setattr("backend.llm.model_registry.create_session_llm", build)
        await execute(fixture, tmp_path)
        assert [(request["model"], request["reasoning"]["effort"]) for request in requests] == [("model-a", "low"), ("model-b", "high")]
        assert "INSTRUCTION-model-b" in requests[1]["instructions"]
        assert original._settings.reasoning_effort == "low"
        assert fixture.inspector.observations == [("model-a", "model-a", "low", 96000)]
