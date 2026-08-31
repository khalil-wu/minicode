from __future__ import annotations

import asyncio
from types import SimpleNamespace

import backend.sdk as sdk_module
import pytest
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import (
    AgentSettings,
    AppConfig,
    LLMSettings,
    PermissionSettings,
    TokenBudget,
)
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.services import tool_registry_factory
from backend.services.llm_adapter_factory import build_provider_adapter
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.registry import ToolRegistry
from backend.ws.command_handlers import SessionCommandHandlersMixin


async def _collect(stream):
    return [event async for event in stream]


def _config(**updates) -> AppConfig:
    values = {"llm": LLMSettings(api_key="")}
    values.update(updates)
    return AppConfig(**values)


def test_builtin_anthropic_provider_uses_messages_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.services.llm_adapter_factory.get_anthropic_settings",
        lambda: {
            "api_key": "test-key",
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-opus-5",
            "small_fast_model": "claude-haiku-4-5",
            "max_tokens": 8_000,
            "thinking_budget": 0,
            "proxy_mode": "direct",
            "context_window": 200_000,
            "default_headers": (),
            "auth_header": False,
        },
    )

    adapter = build_provider_adapter("anthropic")

    assert isinstance(adapter, AnthropicAdapter)
    assert adapter._provider_id == "anthropic"
    assert adapter._proxy_mode == "direct"
    assert adapter._context_window == 200_000
    assert adapter._messages_url() == "https://api.anthropic.com/v1/messages"


def test_tool_registry_freezes_workspace_and_config_snapshot(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    config = _config(
        permissions=PermissionSettings(auto_allow=["read_file"]),
        agent=AgentSettings(max_iterations=17),
        token_budget=TokenBudget(total=91_000),
    )
    monkeypatch.chdir(foreign_cwd)

    registry = build_tool_registry(
        ArtifactStore(storage_dir=tmp_path / "artifacts"),
        workspace_root=workspace,
        config=config,
    )

    task_tool = registry.get_tool("task")
    assert task_tool._resolve_permission_checker()._workspace_root == workspace.resolve()
    assert task_tool._resolve_agent_settings() is config.agent
    assert task_tool._resolve_token_budget() is config.token_budget
    assert registry.get_tool("fuzzy_search").workspace_root == workspace.resolve()
    assert registry.get_tool("git_status")._workspace_root == workspace.resolve()
    assert registry.get_tool("preview_server")._workspace_root == str(workspace.resolve())
    assert registry.get_tool("enter_plan_mode")._workspace_root == workspace.resolve()


def test_tool_registry_loads_config_once_for_the_composed_workspace(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    loaded: list[object] = []

    def load_snapshot(*, cwd=None):
        loaded.append(cwd)
        return config

    monkeypatch.setattr(tool_registry_factory, "load_config", load_snapshot)

    registry = build_tool_registry(
        ArtifactStore(storage_dir=tmp_path / "artifacts"),
        workspace_root=workspace,
    )
    task_tool = registry.get_tool("task")
    task_tool._resolve_permission_checker()
    task_tool._resolve_agent_settings()
    task_tool._resolve_token_budget()

    assert loaded == [workspace.resolve()]


def test_sdk_query_composes_conversation_workspace_and_config(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    config = _config()

    def build_registry(artifact_store, **kwargs):
        captured["artifact_store"] = artifact_store
        captured["registry_kwargs"] = kwargs
        return ToolRegistry()

    class CapturingQueryEngine:
        async def submit(self, submission):
            captured["submission"] = submission
            yield AgentEvent.done()

    monkeypatch.setattr(
        "backend.services.tool_registry_factory.build_tool_registry",
        build_registry,
    )
    monkeypatch.setattr(sdk_module, "QueryEngine", CapturingQueryEngine)

    events = asyncio.run(
        _collect(
            sdk_module.query(
                "hello",
                llm=object(),
                config=config,
                workspace_root=tmp_path,
                metadata={"source": "sdk", "conversation_id": "conv-sdk"},
            )
        )
    )

    assert events[-1].type == "done"
    registry_kwargs = captured["registry_kwargs"]
    assert registry_kwargs["workspace_root"] == tmp_path.resolve()
    assert registry_kwargs["config"] is config
    submission = captured["submission"]
    assert submission.state.conversation_id == "conv-sdk"
    assert submission.state.workspace_root == tmp_path.resolve()
    assert submission.runtime.workspace_root == tmp_path.resolve()
    assert submission.runtime.metadata["conversation_id"] == "conv-sdk"
    assert submission.runtime.metadata["workspace_root"] == str(tmp_path.resolve())
    assert submission.session.permission_checker._workspace_root == tmp_path.resolve()


def test_sdk_query_without_metadata_has_declared_conversation_state(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class CapturingQueryEngine:
        async def submit(self, submission):
            captured["state"] = submission.state
            yield AgentEvent.done()

    monkeypatch.setattr(sdk_module, "QueryEngine", CapturingQueryEngine)

    events = asyncio.run(
        _collect(
            sdk_module.query(
                "hello",
                llm=object(),
                tool_registry=ToolRegistry(),
                config=_config(),
                workspace_root=tmp_path,
            )
        )
    )

    assert events[-1].type == "done"
    assert captured["state"].conversation_id == ""
    assert captured["state"].workspace_root == tmp_path.resolve()


def test_sdk_rejects_state_owner_rebinding(tmp_path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    state = AgentState(
        user_message="hello",
        conversation_id="conv-state",
        workspace_root=tmp_path,
    )

    with pytest.raises(ValueError, match="conversation_id conflicts"):
        asyncio.run(
            _collect(
                sdk_module.query(
                    "hello",
                    llm=object(),
                    tool_registry=ToolRegistry(),
                    config=_config(),
                    state=state,
                    metadata={"conversation_id": "conv-other"},
                )
            )
        )

    with pytest.raises(ValueError, match="workspace_root conflicts"):
        asyncio.run(
            _collect(
                sdk_module.query(
                    "hello",
                    llm=object(),
                    tool_registry=ToolRegistry(),
                    config=_config(),
                    state=state,
                    workspace_root=other_workspace,
                )
            )
        )


def test_ws_model_selection_uses_active_conversation_config(monkeypatch, tmp_path) -> None:
    settings_data = {
        "provider": "custom",
        "models": ["workspace-model"],
        "models_source": "workspace",
    }
    config = _config(
        llm=LLMSettings(
            api_key="",
            provider="custom",
            model="workspace-model",
        ),
        config_layer_stack=SimpleNamespace(
            effective_config=lambda: settings_data,
        ),
    )
    loaded: list[object] = []

    class Session(SessionCommandHandlersMixin):
        active_conversation_id = "conv-workspace"
        provider = "openai"
        selected_model = "global-model"
        available_models: list[str] = []
        models_source = ""
        _model_override_active = False
        _provider_override_active = False

        def __init__(self) -> None:
            self.session_lifecycle = SimpleNamespace(
                workspace_root_for_conversation=lambda: tmp_path,
            )

        def _model_runtime_for_conversation(self, _conversation_id):
            return None

        @staticmethod
        def _resolve_llm_provider(settings):
            return settings["provider"]

        @staticmethod
        def _resolve_available_models(_provider, settings):
            return settings["models"]

        @staticmethod
        def _resolve_models_source(_provider, settings):
            return settings["models_source"]

    monkeypatch.setattr(
        "backend.config.load_config",
        lambda *, cwd=None: loaded.append(cwd) or config,
    )

    session = Session()
    session.refresh_llm_selection(prefer_config=True)

    assert loaded == [tmp_path]
    assert session.config is config
    assert session.provider == "custom"
    assert session.available_models == ["workspace-model"]
    assert session.selected_model == "workspace-model"
    assert session.models_source == "workspace"
