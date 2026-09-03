import asyncio
from types import SimpleNamespace

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.config import AppConfig, LLMSettings
from backend.services.llm_config_service import (
    apply_llm_config_update,
    llm_model_updated_payload,
    refresh_llm_selection_state,
)
from backend.ws.handlers.misc import handle_llm_config_set


class _HookManager:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    async def run_config_change(self, **kwargs) -> None:
        self.calls.append(dict(kwargs))


class _Session:
    def __init__(self) -> None:
        self.config = AppConfig(llm=LLMSettings(api_key="test-key"))
        self.provider = "openai"
        self.available_models = ["gpt-5"]
        self.selected_model = "gpt-5"
        self._model_override_active = False
        self._provider_override_active = False
        self.context_builder = ContextBuilder(
            token_budget=self.config.token_budget,
            agent_settings=self.config.agent,
        )
        self.events: list[dict] = []
        self.command_results: list[dict] = []
        self.session_lifecycle = SimpleNamespace(
            send_runtime_capabilities=self.send_runtime_capabilities,
        )

    def reset_model_selection_overrides(self) -> None:
        self._model_override_active = False
        self._provider_override_active = False

    async def send_event(self, event: AgentEvent) -> None:
        self.events.append(event.to_ws_message())

    async def send_llm_state(self) -> None:
        self.events.append(
            {
                "type": "llm.model.updated",
                "provider": self.provider,
                "model": self.selected_model,
                "current_model": self.selected_model,
                "available_models": list(self.available_models),
            }
        )

    async def send_runtime_capabilities(self, *, source: str = "session") -> None:
        self.events.append(
            {
                "type": "runtime.capabilities",
                "source": source,
                "capabilities": {
                    "provider_capabilities": {
                        "provider": self.provider,
                        "model": self.selected_model,
                    }
                },
            }
        )

    async def emit_command_result(self, command: str, message: str, **kwargs) -> None:
        self.command_results.append(
            {
                "type": "command.result",
                "command": command,
                "message": message,
                **kwargs,
            }
        )


def test_selection_refresh_does_not_choose_catalog_first_model() -> None:
    state = refresh_llm_selection_state(
        previous_provider="team-provider",
        selected_model="",
        model_override_active=False,
        provider_resolver=lambda: "team-provider",
        models_resolver=lambda _provider: ["directory-first", "directory-second"],
        config_loader=lambda: AppConfig(llm=LLMSettings(api_key="", model="")),
    )

    assert state.available_models == ["directory-first", "directory-second"]
    assert state.selected_model == ""


def test_model_updated_event_distinguishes_saved_and_effective_reasoning(monkeypatch):
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setattr(
        "backend.services.llm_config_service.get_custom_settings",
        lambda: {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "wire_api": "chat",
            "reasoning_effort": "low",
            "model_metadata": {},
        },
    )

    payload = llm_model_updated_payload(
        provider="custom",
        selected_model="deepseek-v4-flash",
        available_models=["deepseek-v4-flash"],
        workspace_root="C:/repo",
    )

    assert payload["reasoning_effort"] == ""
    assert payload["configured_reasoning_effort"] == "low"
    assert payload["effective_reasoning_effort"] == ""
    assert payload["reasoning_effort_supported"] is False
    assert payload["reasoning_effort_levels"] == []
    assert payload["context_window"] == 200_000
    assert payload["context_window_source"] == "fallback"
    assert payload["context_window_verified"] is False


def test_model_updated_event_reports_provider_declared_capabilities(monkeypatch):
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setattr(
        "backend.services.llm_config_service.get_custom_settings",
        lambda: {
            "base_url": "https://gateway.example/v1",
            "model": "provider-model",
            "wire_api": "responses",
            "reasoning_effort": "high",
            "model_metadata": {
                "provider-model": {
                    "context_window": 128_000,
                    "reasoning_effort_levels": ["low", "high"],
                }
            },
        },
    )

    payload = llm_model_updated_payload(
        provider="custom",
        selected_model="provider-model",
        available_models=["provider-model"],
        workspace_root="C:/repo",
    )

    assert payload["reasoning_effort"] == "high"
    assert payload["effective_reasoning_effort"] == "high"
    assert payload["reasoning_effort_supported"] is True
    assert payload["reasoning_effort_levels"] == ["low", "high"]
    assert payload["context_window"] == 128_000
    assert payload["context_window_source"] == "provider"
    assert payload["context_window_verified"] is True

def test_provider_config_update_is_state_event_not_system_notice(monkeypatch):
    monkeypatch.setattr(
        "backend.config.load_config",
        lambda: AppConfig(llm=LLMSettings(api_key="test-key", model="gpt-5.5")),
    )
    monkeypatch.setattr(
        "backend.config.get_llm_settings_payload",
        lambda: {
            "provider": "custom",
            "custom": {
                "model": "gpt-5.5",
                "available_models": ["gpt-5.5"],
                "wire_api": "chat",
            },
            "active_model": "gpt-5.5",
        },
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: SimpleNamespace(model=model_override),
    )

    session = _Session()

    handled = asyncio.run(handle_llm_config_set(session, {"provider": "custom", "model": "gpt-5.5"}))

    assert handled is True
    assert session.provider == "custom"
    assert session.selected_model == "gpt-5.5"
    assert [event["type"] for event in session.events] == ["llm.model.updated", "runtime.capabilities"]
    assert session.events[-1]["source"] == "llm.config.set"
    assert session.command_results == []


def test_reasoning_effort_from_footer_uses_command_result_not_system_notice(monkeypatch, tmp_path):
    calls: list[dict] = []
    saved_payloads: list[dict] = []
    settings_file = tmp_path / "settings.json"
    payload = {
        "provider": "openai",
        "openai": {
            "model": "gpt-5",
            "available_models": ["gpt-5"],
            "wire_api": "responses",
            "reasoning_effort": "medium",
            "model_metadata": {
                "gpt-5": {
                    "reasoning_effort_levels": ["low", "medium", "high", "focused"],
                }
            },
        },
        "active_model": "gpt-5",
    }

    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        "backend.config.load_config",
        lambda: AppConfig(llm=LLMSettings(api_key="test-key", model="gpt-5")),
    )
    monkeypatch.setattr(
        "backend.config.get_llm_settings_payload",
        lambda: payload,
    )
    def save_settings(next_payload):
        saved_payloads.append(next_payload)
        payload["openai"].update(next_payload["openai"])
        return payload

    monkeypatch.setattr("backend.config.save_llm_settings", save_settings)
    monkeypatch.setattr(
        "backend.hooks.get_hook_manager",
        lambda: _HookManager(calls),
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: SimpleNamespace(model=model_override),
    )

    session = _Session()

    handled = asyncio.run(
        handle_llm_config_set(
            session,
            {
                "provider": "openai",
                "reasoning_effort": "focused",
                "source": "frontend.footer",
            },
        )
    )

    assert handled is True
    assert [event["type"] for event in session.events] == ["llm.model.updated", "runtime.capabilities"]
    assert session.events[-1]["source"] == "llm.config.set"
    assert session.command_results == [
        {
            "type": "command.result",
            "command": "effort",
            "message": "Reasoning effort set to 'focused'.",
            "data": {
                "reasoning_effort": "focused",
                "applied": True,
            },
        }
    ]
    assert calls == [{"source": "llm", "file_path": str(settings_file)}]
    assert len(saved_payloads) == 1
    assert set(saved_payloads[0]) == {"openai"}
    assert saved_payloads[0]["openai"]["reasoning_effort"] == "focused"


def test_reasoning_effort_validation_error_uses_effort_command_correlation(monkeypatch):
    async def reject_update(_data):
        raise ValueError("Reasoning effort is not supported by this model")

    monkeypatch.setattr(
        "backend.services.llm_config_service.apply_llm_config_update",
        reject_update,
    )

    footer_session = _Session()
    handled = asyncio.run(handle_llm_config_set(
        footer_session,
        {
            "provider": "openai",
            "reasoning_effort": "unsupported",
            "source": "frontend.footer",
        },
    ))

    assert handled is True
    assert footer_session.events[-1] == {
        "type": "command.result",
        "command": "effort",
        "message": "Reasoning effort is not supported by this model",
        "level": "error",
    }

    config_session = _Session()
    handled = asyncio.run(handle_llm_config_set(
        config_session,
        {"provider": "openai", "model": "unsupported"},
    ))

    assert handled is True
    assert config_session.events[-1]["command"] == "llm.config.set"


def test_reasoning_effort_is_not_applied_without_exact_model_declaration(monkeypatch):
    payload = {
        "provider": "openai",
        "openai": {
            "model": "provider-model",
            "wire_api": "responses",
            "model_metadata": {
                "provider-model": {
                    "reasoning_effort_levels": ["low", "high"],
                }
            },
        },
    }
    monkeypatch.setattr(
        "backend.config.load_config",
        lambda: AppConfig(llm=LLMSettings(api_key="test-key", model="provider-model")),
    )
    monkeypatch.setattr("backend.config.get_llm_settings_payload", lambda: payload)

    result = asyncio.run(
        apply_llm_config_update(
            {
                "provider": "openai",
                "reasoning_effort": "focused",
                "source": "frontend.footer",
            }
        )
    )

    assert result.reasoning_effort == ""
    assert result.notice is not None
    assert result.notice.level == "warning"
    assert result.notice.data == {"reasoning_effort": "focused", "applied": False}


def test_provider_config_update_refreshes_session_models_from_current_config(monkeypatch):
    monkeypatch.setattr(
        "backend.config.load_config",
        lambda: AppConfig(llm=LLMSettings(api_key="test-key", model="deepseek-v4-pro")),
    )
    monkeypatch.setattr(
        "backend.config.get_llm_settings_payload",
        lambda: {
            "provider": "custom",
            "custom": {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
                "available_models": ["deepseek-v4-pro", "deepseek-v4-flash"],
                "wire_api": "chat",
            },
            "active_model": "deepseek-v4-pro",
        },
    )
    monkeypatch.setattr(
        "backend.config.get_available_models",
        lambda provider: ["deepseek-v4-pro", "deepseek-v4-flash"] if provider == "custom" else ["gpt-5"],
    )
    monkeypatch.setattr(
        "backend.config_helpers.get_custom_settings",
        lambda settings_data=None: {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
            "available_models": ["deepseek-v4-pro", "deepseek-v4-flash"],
            "wire_api": "chat",
        },
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: SimpleNamespace(model=model_override),
    )

    session = _Session()

    handled = asyncio.run(handle_llm_config_set(session, {"provider": "custom"}))

    assert handled is True
    assert session.provider == "custom"
    assert session.selected_model == "deepseek-v4-pro"
    assert session.available_models == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert [event["type"] for event in session.events] == ["llm.model.updated", "runtime.capabilities"]
    assert session.events[-1]["source"] == "llm.config.set"
