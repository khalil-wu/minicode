from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.message import AgentEvent
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.artifact.store import ArtifactStore
from backend.config import (
    AgentSettings,
    AppConfig,
    LLMSettings,
    PermissionSettings,
    TokenBudget,
)
from backend.llm.model_runtime import ModelDefinition
from backend.llm.provider_contracts import ReasoningPolicy
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_tools import (
    TaskTool,
    _close_subagent_llm_resolution,
    _configured_subagent_overrides,
    _resolve_subagent_llm,
)
from backend.tools.registry import ToolRegistry


class _Adapter:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        effort: str = "medium",
        levels: tuple[str, ...] = ("off", "low", "medium", "high"),
    ) -> None:
        self._settings = LLMSettings(
            api_key="test-key",
            provider=provider,
            base_url="https://example.invalid/v1",
            model=model,
            reasoning_effort=effort,
            reasoning_effort_levels=levels,
        )
        self._reasoning_policy = ReasoningPolicy(level=effort or "off")
        self.close_calls = 0

    def supported_reasoning_efforts(self) -> tuple[str, ...]:
        return tuple(self._settings.reasoning_effort_levels)

    def current_reasoning_effort(self) -> str:
        return self._reasoning_policy.level

    def apply_reasoning_policy(self, policy: ReasoningPolicy) -> None:
        self._reasoning_policy = policy
        self._settings = self._settings.__class__(
            **{
                **self._settings.__dict__,
                "reasoning_effort": policy.wire_level or policy.level,
            }
        )

    async def aclose(self) -> None:
        self.close_calls += 1


class _Runtime:
    def __init__(self, models: list[ModelDefinition]) -> None:
        self.models = models

    def get_model(self, provider: str, model: str):
        return next(
            (
                item
                for item in self.models
                if item.provider == provider and item.id == model
            ),
            None,
        )

    def get_models(self, provider: str | None = None):
        if provider is None:
            return tuple(self.models)
        return tuple(item for item in self.models if item.provider == provider)

    def get_provider(self, provider: str):
        if any(item.provider == provider for item in self.models):
            return SimpleNamespace(id=provider, name=provider)
        return None

    def get_registered_provider_config(self, provider: str):
        return {"models": []} if self.get_provider(provider) is not None else None


def _model(
    provider: str,
    model_id: str,
    *,
    reasoning: bool = True,
    default: str = "medium",
    levels: tuple[str, ...] = ("off", "low", "medium", "high"),
    context_window: int = 128_000,
) -> ModelDefinition:
    return ModelDefinition(
        provider=provider,
        id=model_id,
        name=model_id,
        api="openai-completions",
        base_url="https://example.invalid/v1",
        reasoning=reasoning,
        context_window=context_window,
        max_tokens=16_384,
        reasoning_effort_levels=levels,
        default_reasoning_effort=default,
    )


def _parent_fixture():
    parent = _Adapter(
        provider="custom",
        model="claude-sonnet-4-6",
        effort="medium",
    )
    runtime = _Runtime(
        [
            _model(
                "custom",
                "claude-sonnet-4-6",
                context_window=200_000,
            ),
            _model(
                "custom",
                "claude-opus-4-6",
                default="high",
                context_window=64_000,
            ),
            _model("custom", "org/model-with-slash", default="low"),
            _model("zai", "glm-5", default="low"),
        ]
    )
    config = AppConfig(
        llm=parent._settings,
        token_budget=TokenBudget(total=200_000, response_reserve=16_384),
        permissions=PermissionSettings(),
        agent=AgentSettings(max_iterations=3),
    )
    metadata = {
        "_subagent_parent_runtime": {
            "config": config,
            "provider": "custom",
            "model": "claude-sonnet-4-6",
            "model_runtime": runtime,
            "available_models": (
                "claude-sonnet-4-6",
                "claude-opus-4-6",
                "org/model-with-slash",
            ),
            "llm": parent,
            "thinking_level": "medium",
        }
    }
    return parent, runtime, config, metadata


def _parent_run_context(
    metadata: dict,
    *,
    agent_runtime: AgentRuntime | None = None,
    snapshot: dict | None = None,
) -> RunContext:
    """Build the typed owner used by the live subagent resolution boundary."""

    parent_snapshot = snapshot
    if parent_snapshot is None:
        parent_snapshot = metadata["_subagent_parent_runtime"]
    return RunContext(
        agent_runtime=agent_runtime,
        subagent_parent_runtime=dict(parent_snapshot),
    )


def _install_factory(monkeypatch: pytest.MonkeyPatch, runtime: _Runtime):
    created: list[_Adapter] = []

    def factory(_config, *, model_override, provider_override, model_runtime):
        assert model_runtime is runtime
        selected = runtime.get_model(provider_override, model_override)
        assert selected is not None
        adapter = _Adapter(
            provider=provider_override,
            model=model_override,
            effort="",
            levels=selected.reasoning_effort_levels,
        )
        created.append(adapter)
        return adapter

    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", factory)
    return created


def test_inherit_reuses_the_live_parent_adapter() -> None:
    async def run() -> None:
        parent, _runtime, config, metadata = _parent_fixture()

        resolution = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
        )

        assert resolution.llm is parent
        assert resolution.config is config
        assert resolution.provider == "custom"
        assert resolution.model == "claude-sonnet-4-6"
        assert resolution.effort == "medium"
        assert resolution.owns_llm is False
        await _close_subagent_llm_resolution(resolution)
        assert parent.close_calls == 0

    asyncio.run(run())


def test_same_model_explicit_effort_creates_and_closes_an_isolated_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        parent, runtime, _config, metadata = _parent_fixture()
        created = _install_factory(monkeypatch, runtime)

        resolution = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
            effort_override="high",
        )

        assert resolution.llm is created[0]
        assert resolution.owns_llm is True
        assert resolution.effort == "high"
        assert created[0]._settings.reasoning_effort == "high"
        await _close_subagent_llm_resolution(resolution)
        assert created[0].close_calls == 1
        assert parent.close_calls == 0

    asyncio.run(run())


def test_task_arguments_override_agent_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.tools.subagent_support.get_custom_agent",
        lambda _name, _workspace=None: SimpleNamespace(
            model="claude-opus-4-6",
            effort="low",
        ),
    )

    assert _configured_subagent_overrides(
        agent_type="reviewer",
        model_override="claude-sonnet-4-6",
        effort_override="high",
        workspace_root=None,
    ) == ("claude-sonnet-4-6", "high", True, True)
    assert _configured_subagent_overrides(
        agent_type="reviewer",
        model_override="",
        effort_override="",
        workspace_root=None,
    ) == ("claude-opus-4-6", "low", False, False)


def test_model_switch_uses_target_default_and_target_context_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        parent, runtime, _config, metadata = _parent_fixture()
        created = _install_factory(monkeypatch, runtime)

        resolution = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
            model_override="claude-opus-4-6",
        )

        assert resolution.provider == "custom"
        assert resolution.model == "claude-opus-4-6"
        assert resolution.effort == "high"
        assert resolution.config.token_budget.total == 64_000
        assert created[0]._settings.reasoning_effort == "high"
        await _close_subagent_llm_resolution(resolution)

    asyncio.run(run())


def test_unsupported_model_and_effort_fail_visibly() -> None:
    async def run() -> None:
        parent, _runtime, _config, metadata = _parent_fixture()

        with pytest.raises(ValueError, match="Unknown model"):
            await _resolve_subagent_llm(
                parent,
                parent_metadata=metadata,
                run_context=_parent_run_context(metadata),
                agent_type="general-purpose",
                model_override="missing-model",
                build_adapter=False,
            )
        with pytest.raises(ValueError, match="Reasoning effort 'max' is not supported"):
            await _resolve_subagent_llm(
                parent,
                parent_metadata=metadata,
                run_context=_parent_run_context(metadata),
                agent_type="general-purpose",
                effort_override="max",
                build_adapter=False,
            )

    asyncio.run(run())


def test_claude_bare_aliases_keep_same_tier_or_resolve_current_provider_catalog() -> None:
    async def run() -> None:
        parent, _runtime, _config, metadata = _parent_fixture()

        same_tier = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
            model_override="sonnet",
            build_adapter=False,
        )
        different_tier = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
            model_override="opus",
            build_adapter=False,
        )

        assert same_tier.model == "claude-sonnet-4-6"
        assert same_tier.llm is parent
        assert different_tier.model == "claude-opus-4-6"
        assert different_tier.effort == "high"

    asyncio.run(run())


def test_claude_alias_resolves_from_parent_snapshot_without_model_runtime() -> None:
    async def run() -> None:
        parent, _runtime, config, metadata = _parent_fixture()
        snapshot = dict(metadata["_subagent_parent_runtime"])
        snapshot["model_runtime"] = None
        snapshot["available_models"] = (
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        )

        resolution = await _resolve_subagent_llm(
            parent,
            parent_metadata={"_subagent_parent_runtime": snapshot},
            run_context=_parent_run_context(
                metadata,
                snapshot=snapshot,
            ),
            agent_type="general-purpose",
            model_override="opus",
            build_adapter=False,
        )

        assert resolution.config.llm.model == "claude-opus-4-6"
        assert resolution.config.llm.provider == "custom"
        assert resolution.model == "claude-opus-4-6"

    asyncio.run(run())


def test_model_id_with_slash_is_checked_exactly_before_provider_split() -> None:
    async def run() -> None:
        parent, _runtime, _config, metadata = _parent_fixture()

        exact = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
            model_override="org/model-with-slash",
            build_adapter=False,
        )
        qualified = await _resolve_subagent_llm(
            parent,
            parent_metadata=metadata,
            run_context=_parent_run_context(metadata),
            agent_type="general-purpose",
            model_override="zai/glm-5",
            build_adapter=False,
        )

        assert exact.provider == "custom"
        assert exact.model == "org/model-with-slash"
        assert qualified.provider == "zai"
        assert qualified.model == "glm-5"

    asyncio.run(run())


def _task_tool(parent: _Adapter, tmp_path) -> TaskTool:
    return TaskTool(
        llm_provider=parent,
        tool_registry_provider=ToolRegistry(),
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=lambda: PermissionChecker(PermissionSettings()),
        agent_settings_provider=lambda: AgentSettings(max_iterations=1),
        token_budget_provider=lambda: TokenBudget(),
    )


def test_background_factory_failure_never_returns_running_or_publishes_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def run() -> None:
        parent, _runtime, _config, metadata = _parent_fixture()
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")

        def fail_factory(*_args, **_kwargs):
            raise RuntimeError("adapter factory failed")

        monkeypatch.setattr("backend.llm.model_registry.create_session_llm", fail_factory)
        result = await _task_tool(parent, tmp_path).execute(
            {
                "description": "background model override",
                "prompt": "Inspect the repository.",
                "model": "claude-opus-4-6",
                "run_in_background": True,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(),
                metadata={
                    **metadata,
                    "agent_runtime": runtime,
                    "run_id": "parent-run",
                },
                run_context=_parent_run_context(
                    metadata,
                    agent_runtime=runtime,
                ),
            ),
        )

        assert result.status == "failed"
        assert "adapter factory failed" in result.content
        assert runtime.list_runs(include_subagents=True)["subagents"] == []
        assert parent.close_calls == 0

    asyncio.run(run())


def test_parallel_factory_failure_closes_prepared_children_without_partial_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def run() -> None:
        parent, runtime_models, _config, metadata = _parent_fixture()
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        first_child = _Adapter(
            provider="custom",
            model="claude-opus-4-6",
            effort="high",
        )
        calls = 0

        def factory(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first_child
            raise RuntimeError("second child failed")

        monkeypatch.setattr("backend.llm.model_registry.create_session_llm", factory)
        result = await _task_tool(parent, tmp_path).execute(
            {
                "parallel_tasks": [
                    {
                        "description": "first",
                        "prompt": "Inspect first.",
                        "model": "claude-opus-4-6",
                        "read_only": True,
                    },
                    {
                        "description": "second",
                        "prompt": "Inspect second.",
                        "model": "zai/glm-5",
                        "read_only": True,
                    },
                ],
                "run_in_background": True,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(),
                metadata={
                    **metadata,
                    "agent_runtime": runtime,
                    "run_id": "parent-run",
                },
                run_context=_parent_run_context(
                    metadata,
                    agent_runtime=runtime,
                ),
            ),
        )

        assert runtime_models.get_model("zai", "glm-5") is not None
        assert result.status == "failed"
        assert "second child failed" in result.content
        assert first_child.close_calls == 1
        assert parent.close_calls == 0
        assert runtime.list_runs(include_subagents=True)["subagents"] == []

    asyncio.run(run())


def test_registration_failure_cancels_unpublished_worker_and_closes_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def run() -> None:
        parent, runtime_models, _config, metadata = _parent_fixture()
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
        children = _install_factory(monkeypatch, runtime_models)
        child_loop_started = False

        async def should_not_start(**_kwargs):
            nonlocal child_loop_started
            child_loop_started = True
            yield AgentEvent.agent_message_completed("unexpected")

        def fail_registration(*_args, **_kwargs):
            raise RuntimeError("registration failed")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", should_not_start)
        monkeypatch.setattr(runtime, "register_subagent_task", fail_registration)

        result = await _task_tool(parent, tmp_path).execute(
            {
                "description": "must not publish",
                "prompt": "Inspect the repository.",
                "model": "claude-opus-4-6",
                "run_in_background": True,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(),
                metadata={
                    **metadata,
                    "agent_runtime": runtime,
                    "run_id": "parent-run",
                },
                run_context=_parent_run_context(
                    metadata,
                    agent_runtime=runtime,
                ),
            ),
        )
        await asyncio.sleep(0)

        assert result.is_error is True
        assert result.status == "failed"
        assert "registration failed" in result.content
        assert len(children) == 1
        assert children[0].close_calls == 1
        assert parent.close_calls == 0
        assert child_loop_started is False
        assert runtime.list_runs(include_subagents=True)["subagents"] == []

    asyncio.run(run())


def test_successful_foreground_child_closes_only_its_fresh_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def run() -> None:
        parent, runtime_models, _config, metadata = _parent_fixture()
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
        children = _install_factory(monkeypatch, runtime_models)

        async def child_loop(**kwargs):
            context = kwargs["context_builder"]
            history_start = context.history_length
            context.append_user(kwargs["user_message"])
            await kwargs["metadata"]["commit_turn_admission"](
                boundary_input=SimpleNamespace(consumed_steer=None),
                history_start=history_start,
                history_end=context.history_length,
            )
            yield AgentEvent.agent_message_completed("child completed")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", child_loop)
        result = await _task_tool(parent, tmp_path).execute(
            {
                "description": "foreground override",
                "prompt": "Inspect the repository.",
                "model": "claude-opus-4-6",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(),
                metadata={
                    **metadata,
                    "agent_runtime": runtime,
                    "run_id": "parent-run",
                },
                run_context=_parent_run_context(
                    metadata,
                    agent_runtime=runtime,
                ),
            ),
        )

        assert result.status == "completed"
        assert "child completed" in result.content
        assert len(children) == 1
        assert children[0].close_calls == 1
        assert parent.close_calls == 0
        subagents = runtime.list_runs(include_subagents=True)["subagents"]
        assert len(subagents) == 1
        transcript = runtime.load_agent_transcript(str(subagents[0]["subagent_id"]))
        user_prompt = next(
            event for event in transcript["events"]
            if event["event_type"] == "user_prompt"
        )
        assert user_prompt["payload"]["provider"] == "custom"
        assert user_prompt["payload"]["model"] == "claude-opus-4-6"
        assert user_prompt["payload"]["reasoning_effort"] == "high"

    asyncio.run(run())
