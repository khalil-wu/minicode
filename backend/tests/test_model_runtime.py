from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import time
from pathlib import Path

import pytest
from dataclasses import replace

import backend.llm.model_runtime as model_runtime_module
import backend.llm.model_runtime_definitions as definitions_module
import backend.llm.provider_auth as provider_auth_module
from backend.config import LLMSettings
from backend.llm.model_runtime import (
    ModelDefinition,
    ModelRuntime,
    ProviderRegistrationError,
    apply_model_thinking_level,
    clamp_model_thinking_level,
    default_model_thinking_level,
    model_thinking_levels,
)
from backend.llm.provider_auth import ProviderAuthStorage
from backend.llm.provider_contracts import ReasoningPolicy
from backend.llm.provider_models import ProviderModelsStorage


def _model(
    *,
    reasoning: bool = True,
    thinking_level_map=None,
    default_reasoning_effort: str = "",
) -> ModelDefinition:
    return ModelDefinition(
        provider="test-provider",
        id="test-model",
        name="Test Model",
        api="openai-completions",
        base_url="https://example.invalid/v1",
        reasoning=reasoning,
        thinking_level_map=thinking_level_map,
        context_window=128_000,
        max_tokens=16_384,
        default_reasoning_effort=default_reasoning_effort,
    )


def test_base_provider_runtime_projects_saved_proxy_mode(monkeypatch) -> None:
    def section(provider: str, proxy_mode: str) -> dict[str, object]:
        wire_api = "anthropic" if provider == "anthropic" else "chat"
        return {
            "display_name": provider,
            "api_key": "test-key",
            "base_url": f"https://{provider}.example/v1",
            "model": f"{provider}-model",
            "available_models": [f"{provider}-model"],
            "wire_api": wire_api,
            "proxy_mode": proxy_mode,
            "max_tokens": 8_000 if provider == "anthropic" else 0,
            "model_metadata": {},
            "reasoning_effort_levels": [],
        }

    monkeypatch.setattr(
        model_runtime_module,
        "get_openai_settings",
        lambda: section("openai", "inherit"),
    )
    monkeypatch.setattr(
        model_runtime_module,
        "get_anthropic_settings",
        lambda: section("anthropic", "inherit"),
    )
    monkeypatch.setattr(
        model_runtime_module,
        "get_custom_settings",
        lambda: section("custom", "direct"),
    )

    providers = ModelRuntime._load_base_providers()

    assert providers["custom"]["proxy_mode"] == "direct"
    assert providers["openai"]["proxy_mode"] == "inherit"


class _SettingsAdapter:
    def __init__(self) -> None:
        self._settings = LLMSettings(
            api_key="test-key",
            provider="test-provider",
            base_url="https://example.invalid/v1",
            model="test-model",
            reasoning_effort="",
            reasoning_effort_levels=(),
        )
        self._reasoning_policy = ReasoningPolicy(level="off")

    def supported_reasoning_efforts(self) -> tuple[str, ...]:
        return ("off", "minimal", "low", "medium", "high", "xhigh", "max")

    def apply_reasoning_policy(self, policy: ReasoningPolicy) -> None:
        self._reasoning_policy = policy
        self._settings = replace(
            self._settings,
            reasoning_effort=policy.wire_level,
            reasoning_effort_levels=policy.wire_levels,
        )

    def current_reasoning_effort(self) -> str:
        return self._reasoning_policy.level


class _BudgetAdapter:
    def __init__(self) -> None:
        self._thinking_budget = 4096
        self._configured_thinking_budget = 4096
        self._reasoning_policy = ReasoningPolicy(level="high")

    def supported_reasoning_efforts(self) -> tuple[str, ...]:
        return ("off", "high")

    def apply_reasoning_policy(self, policy: ReasoningPolicy) -> None:
        self._reasoning_policy = policy
        self._thinking_budget = (
            self._configured_thinking_budget if policy.level != "off" else None
        )


class _FakeAuthStorage:
    def __init__(self) -> None:
        self.values: dict[str, dict] = {}
        self.locks: dict[tuple[str, int], asyncio.Lock] = {}

    def get(self, provider: str):
        value = self.values.get(provider)
        return dict(value) if value is not None else None

    def set(self, provider: str, value) -> None:
        self.values[provider] = dict(value)

    def delete(self, provider: str) -> bool:
        return self.values.pop(provider, None) is not None

    async def modify(self, provider: str, fn):
        key = (provider, id(asyncio.get_running_loop()))
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self.get(provider)
            value = fn(current)
            if asyncio.iscoroutine(value):
                value = await value
            if value is None:
                return current
            self.set(provider, value)
            return self.get(provider)

    async def delete_serialized(self, provider: str) -> bool:
        return self.delete(provider)


class _ProcessJsonVault:
    """Tiny process-shared vault used to exercise ProviderAuthStorage locks."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, values: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(values), encoding="utf-8")

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def set(self, name: str, value: str, **_kwargs) -> None:
        values = self._read()
        values[name] = value
        self._write(values)

    def delete(self, name: str) -> bool:
        values = self._read()
        removed = values.pop(name, None) is not None
        if removed:
            self._write(values)
        return removed


def test_provider_auth_storage_uses_only_minicode_vault_namespace() -> None:
    class MemoryVault:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def get(self, name: str) -> str | None:
            return self.values.get(name)

        def set(self, name: str, value: str, **_kwargs) -> None:
            self.values[name] = value

        def delete(self, name: str) -> bool:
            return self.values.pop(name, None) is not None

    vault = MemoryVault()
    provider_id = "provider"
    digest = hashlib.sha256(provider_id.encode("utf-8")).hexdigest()[:24].upper()
    legacy_name = f"PROVIDER_OAUTH_{digest}"
    vault.values[legacy_name] = json.dumps({"type": "api_key", "key": "legacy"})
    storage = ProviderAuthStorage(vault)

    assert storage.get(provider_id) is None
    storage.set(provider_id, {"type": "api_key", "key": "canonical"})

    canonical_name = provider_auth_module._credential_name(provider_id)
    assert canonical_name == f"MINICODE_PROVIDER_CREDENTIAL_{digest}"
    assert json.loads(vault.values[canonical_name])["key"] == "canonical"
    assert vault.values[legacy_name]


def test_provider_models_storage_rejects_legacy_time_fields(tmp_path: Path) -> None:
    storage = ProviderModelsStorage(tmp_path / "models-store.json")

    with pytest.raises(ValueError, match="unsupported legacy fields: checkedAt"):
        asyncio.run(storage.write("provider", {"models": [], "checkedAt": 1}))

    asyncio.run(
        storage.write("provider", {"models": [], "checked_at": 1})
    )
    assert asyncio.run(storage.read("provider")) == {
        "models": [],
        "checked_at": 1,
    }


def test_only_minicode_offline_controls_provider_network(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_OFFLINE", raising=False)
    monkeypatch.setenv("PI_OFFLINE", "1")
    assert definitions_module._minicode_network_allowed() is True

    monkeypatch.setenv("MINICODE_OFFLINE", "1")
    assert definitions_module._minicode_network_allowed() is False

    monkeypatch.setenv("MINICODE_OFFLINE", "false")
    assert definitions_module._minicode_network_allowed() is True


def _provider_auth_rotate_process(
    path: str,
    entered,
    release,
    result_queue,
) -> None:
    async def run() -> None:
        storage = ProviderAuthStorage(_ProcessJsonVault(path))

        async def rotate(current):
            entered.set()
            release.wait(15)
            return {
                "type": "oauth",
                "access": "rotated",
                "refresh": "rotated-refresh",
                "expires": 4_102_444_800_000,
            }

        result_queue.put(await storage.modify("provider", rotate))

    asyncio.run(run())


def _provider_auth_delete_process(
    path: str,
    entered,
    finished,
    result_queue,
) -> None:
    async def run() -> None:
        storage = ProviderAuthStorage(_ProcessJsonVault(path))
        entered.set()
        result_queue.put(await storage.delete_serialized("provider"))
        finished.set()

    asyncio.run(run())


class _OAuthCallbacks:
    def __init__(self, *, drain_error: Exception | None = None) -> None:
        self.drain_error = drain_error
        self.drain_count = 0

    async def drain(self) -> None:
        self.drain_count += 1
        if self.drain_error is not None:
            raise self.drain_error


class _ApiKeyCallbacks:
    def __init__(self, answer: str = "interactive-key") -> None:
        self.answer = answer
        self.prompts: list[dict[str, object]] = []
        self.drain_count = 0

    async def prompt(self, payload) -> str:
        self.prompts.append(dict(payload))
        return self.answer

    async def drain(self) -> None:
        self.drain_count += 1


def _isolated_model_runtime() -> ModelRuntime:
    """Create a runtime with no developer machine provider/model projection."""

    runtime = ModelRuntime(provider_configs={})
    runtime._base_providers = {}
    runtime._load_base_providers = lambda: {}  # type: ignore[method-assign]
    runtime._refresh_available_snapshot(apply_filters=False)
    return runtime


def _oauth_runtime(oauth, *, provider_id: str = "modern-oauth") -> tuple[ModelRuntime, _FakeAuthStorage]:
    runtime = _isolated_model_runtime()
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    runtime.register_provider(provider_id, {
        "name": "Modern OAuth",
        "auth": {"oauth": oauth},
        "models": [{
            "id": "model-1",
            "name": "Model One",
            "api": "openai-completions",
            "base_url": "https://models.example.test/v1",
            "context_window": 128_000,
            "max_tokens": 8_192,
        }],
    })
    return runtime, storage


def _modern_api_key_runtime(
    api_key_auth,
    *,
    provider_id: str = "modern-auth",
    headers: dict[str, str] | None = None,
    auth_header: bool = False,
    refresh_models=None,
) -> tuple[ModelRuntime, _FakeAuthStorage]:
    runtime = _isolated_model_runtime()
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    config = {
        "name": "Modern Auth",
        "auth": {"api_key": api_key_auth},
        "auth_header": auth_header,
        "headers": headers or {},
        "models": [
            {
                "id": "model-1",
                "name": "Model One",
                "api": "openai-completions",
                "base_url": "https://models.example.test/v1",
                "context_window": 128_000,
                "max_tokens": 8_192,
            }
        ],
    }
    if refresh_models is not None:
        config["refresh_models"] = refresh_models
    runtime.register_provider(provider_id, config)
    return runtime, storage


def _native_filter_runtime(
    filter_models,
    *,
    models: list[dict] | None = None,
    refresh_models=None,
) -> tuple[ModelRuntime, _FakeAuthStorage, list[dict]]:
    catalog = list(models or [
        {
            "id": "model-1",
            "name": "Model One",
            "api": "openai-completions",
            "base_url": "https://models.example.test/v1",
        }
    ])

    async def resolve(input_value):
        credential = input_value.get("credential") or {}
        return {"auth": {"api_key": credential.get("key")}}

    # filter_models is a base-provider capability on purpose: a registered
    # extension config must not be able to cull the composed catalog. So this
    # provider is installed natively rather than through register_provider.
    provider = {
        "name": "Native Filter",
        "auth": {"api_key": {"resolve": resolve}},
        "models": tuple(
            ModelDefinition(
                provider="native-filter",
                id=str(model["id"]),
                name=str(model.get("name") or model["id"]),
                api=str(model.get("api") or "openai-completions"),
                base_url=str(model.get("base_url") or "https://models.example.test/v1"),
                context_window=128_000,
                max_context_window=128_000,
                max_tokens=16_384,
                max_output_tokens=16_384,
            )
            for model in catalog
        ),
        "filter_models": filter_models,
    }
    if refresh_models is not None:
        provider["refresh_models"] = refresh_models
    runtime = _isolated_model_runtime()
    runtime._base_providers = {"native-filter": provider}
    runtime._load_base_providers = lambda: {"native-filter": provider}
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    return runtime, storage, catalog


def test_pi_reasoning_model_without_map_supports_only_base_levels() -> None:
    assert model_thinking_levels(_model()) == (
        "off",
        "minimal",
        "low",
        "medium",
        "high",
    )


def test_pi_non_reasoning_model_supports_only_off() -> None:
    assert model_thinking_levels(_model(reasoning=False)) == ("off",)


def test_config_command_output_is_bounded_before_becoming_auth_material(
    monkeypatch,
) -> None:
    class Completed:
        returncode = 0

    def fake_run(*_args, stdout, **_kwargs):
        stdout.write(
            b"x" * (definitions_module._MAX_CONFIG_COMMAND_OUTPUT_BYTES + 1)
        )
        return Completed()

    monkeypatch.setattr(definitions_module.subprocess, "run", fake_run)
    monkeypatch.setattr(definitions_module.shutil, "which", lambda _name: "bash")

    assert (
        definitions_module._execute_config_command(
            "!synthetic-command",
            use_cache=False,
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_window", True),
        ("context_window", 1.5),
        ("context_window", "128000"),
        ("max_context_window", float("inf")),
        ("max_tokens", 0),
    ],
)
def test_pi_provider_rejects_lossy_or_invalid_numeric_model_declarations(
    field: str,
    value,
) -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}

    with pytest.raises(ProviderRegistrationError, match=field):
        runtime.register_provider(
            "strict-provider",
            {
                "api": "openai-completions",
                "models": [
                    {
                        "id": "strict-model",
                        "base_url": "https://example.invalid/v1",
                        field: value,
                    }
                ],
            },
        )


def test_pi_provider_rejects_string_reasoning_flag() -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}

    with pytest.raises(ProviderRegistrationError, match="reasoning must be a boolean"):
        runtime.register_provider(
            "strict-provider",
            {
                "api": "openai-completions",
                "models": [
                    {
                        "id": "strict-model",
                        "base_url": "https://example.invalid/v1",
                        "reasoning": "false",
                    }
                ],
            },
        )


def test_pi_provider_rejects_output_limit_larger_than_declared_window() -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}

    with pytest.raises(ProviderRegistrationError, match="max_tokens must not exceed"):
        runtime.register_provider(
            "strict-provider",
            {
                "api": "openai-completions",
                "models": [
                    {
                        "id": "strict-model",
                        "base_url": "https://example.invalid/v1",
                        "context_window": 4_096,
                        "max_tokens": 8_192,
                    }
                ],
            },
        )


def test_pi_provider_omitted_output_limit_is_bounded_by_small_context_window() -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}
    runtime.register_provider(
        "strict-provider",
        {
            "api": "openai-completions",
            "models": [
                {
                    "id": "strict-model",
                    "base_url": "https://example.invalid/v1",
                    "context_window": 4_096,
                }
            ],
        },
    )

    model = runtime.get_model("strict-provider", "strict-model")

    assert model is not None
    assert model.context_window == 4_096
    assert model.max_tokens == 4_096
    assert model.max_output_tokens_source == "context_window_clamp"
    assert model.max_output_tokens_verified is True


@pytest.mark.parametrize("api", ["openai-responses", "openai-completions"])
def test_builtin_openai_auto_output_limit_remains_zero(api: str) -> None:
    model = ModelRuntime._base_model(
        "custom",
        "gateway-model",
        api=api,
        base_url="https://gateway.example/v1",
        max_tokens=0,
        settings={},
    )

    assert model.max_tokens == 0


def test_builtin_anthropic_auto_output_limit_becomes_required_default() -> None:
    model = ModelRuntime._base_model(
        "anthropic",
        "claude-test",
        api="anthropic-messages",
        base_url="https://api.anthropic.com/v1",
        max_tokens=0,
        settings={},
    )

    assert model.max_tokens == 8_000


def test_pi_thinking_map_null_disables_levels_and_extended_levels_are_opt_in() -> None:
    model = _model(
        thinking_level_map={
            "off": None,
            "minimal": None,
            "xhigh": "max",
            "max": None,
        }
    )

    assert model_thinking_levels(model) == ("low", "medium", "high", "xhigh")


def test_pi_clamp_searches_upward_before_downward() -> None:
    assert clamp_model_thinking_level("xhigh", ("high", "max")) == "max"


def test_pi_default_medium_is_clamped_to_target_model() -> None:
    model = _model(thinking_level_map={"medium": None})
    available = model_thinking_levels(model)

    assert default_model_thinking_level(model, available) == "high"
    assert default_model_thinking_level(_model(reasoning=False), ("off",)) == ""


def test_canonical_pi_level_is_kept_separate_from_provider_wire_mapping() -> None:
    model = _model(
        thinking_level_map={
            "off": "none",
            "minimal": "low",
            "xhigh": "max",
        }
    )
    adapter = _SettingsAdapter()

    effective = apply_model_thinking_level(adapter, model, "xhigh")

    assert effective == "xhigh"
    assert adapter.current_reasoning_effort() == "xhigh"
    assert adapter._settings.reasoning_effort == "max"
    assert adapter._settings.reasoning_effort_levels == (
        "none",
        "low",
        "medium",
        "high",
        "max",
    )


def test_explicit_off_mapping_is_put_on_wire_but_implicit_off_is_omitted() -> None:
    explicit = _SettingsAdapter()
    implicit = _SettingsAdapter()

    apply_model_thinking_level(
        explicit,
        _model(thinking_level_map={"off": "none"}),
        "off",
    )
    apply_model_thinking_level(implicit, _model(), "off")

    assert explicit.current_reasoning_effort() == "off"
    assert explicit._settings.reasoning_effort == "none"
    assert implicit.current_reasoning_effort() == "off"
    assert implicit._settings.reasoning_effort == ""


def test_budget_adapter_exposes_only_off_high_and_restores_its_budget() -> None:
    adapter = _BudgetAdapter()
    model = _model()

    assert model_thinking_levels(model, adapter) == ("off", "high")
    apply_model_thinking_level(adapter, model, "off")
    assert adapter._thinking_budget is None
    apply_model_thinking_level(adapter, model, "high")
    assert adapter._thinking_budget == 4096


def test_modern_pi_api_key_login_persists_and_resolves_without_exposing_secret() -> None:
    seen: dict[str, object] = {}

    async def login(interaction):
        seen["interaction"] = interaction
        return {
            "type": "api_key",
            "key": "stored-interactive-key",
            "env": {"ACCOUNT_ID": "account-one"},
        }

    async def check(input_value):
        credential = input_value.get("credential")
        return (
            {"type": "api_key", "source": "stored credential"}
            if credential and credential.get("key")
            else None
        )

    async def resolve(input_value):
        credential = input_value.get("credential") or {}
        return {
            "auth": {
                "api_key": credential.get("key"),
                "headers": {"X-Account": credential.get("env", {}).get("ACCOUNT_ID", "")},
            },
            "env": credential.get("env", {}),
            "source": "stored credential",
        }

    runtime, storage = _modern_api_key_runtime(
        {"name": "Interactive key", "login": login, "check": check, "resolve": resolve}
    )
    callbacks = _ApiKeyCallbacks()

    result = asyncio.run(
        runtime.login_provider(
            "modern-auth",
            callbacks,
            auth_type="api_key",
        )
    )

    assert result == {"type": "api_key"}
    assert callbacks.drain_count == 1
    assert callbacks.prompts == []
    assert seen["interaction"] is callbacks
    assert storage.values["modern-auth"] == {
        "type": "api_key",
        "key": "stored-interactive-key",
        "env": {"ACCOUNT_ID": "account-one"},
    }
    assert "stored-interactive-key" not in json.dumps(result)
    spec = runtime.resolve_adapter_spec("modern-auth", "model-1")
    assert spec.api_key == "stored-interactive-key"
    assert spec.headers == {"X-Account": "account-one"}


def test_pi_default_api_key_login_is_available_for_non_oauth_provider() -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    runtime.register_provider(
        "interactive-default",
        {
            "name": "Interactive Default",
            "models": [
                {
                    "id": "model-1",
                    "api": "openai-completions",
                    "base_url": "https://models.example.test/v1",
                }
            ],
        },
    )
    callbacks = _ApiKeyCallbacks("default-login-key")

    result = asyncio.run(
        runtime.login_provider(
            "interactive-default",
            callbacks,
            auth_type="api_key",
        )
    )

    assert result == {"type": "api_key"}
    assert callbacks.prompts == [
        {"type": "secret", "message": "Enter API key"}
    ]
    assert storage.values["interactive-default"] == {
        "type": "api_key",
        "key": "default-login-key",
    }
    assert runtime.resolve_adapter_spec(
        "interactive-default",
        "model-1",
    ).api_key == "default-login-key"
    assert asyncio.run(runtime.logout_provider("interactive-default")) is True
    assert "interactive-default" not in storage.values


def test_modern_pi_oauth_login_refresh_and_header_only_auth_are_projected_exactly() -> None:
    seen: dict[str, object] = {}

    async def login(callbacks):
        seen["callbacks"] = callbacks
        return {
            "type": "oauth",
            "access": "access-one",
            "refresh": "refresh-one",
            "expires": 1,
            "accountId": "account-one",
        }

    async def refresh(credentials):
        seen["refresh_credentials"] = dict(credentials)
        return {
            "type": "oauth",
            "access": "access-two",
            "refresh": "refresh-two",
            "expires": 4_102_444_800_000,
            "accountId": "account-two",
        }

    async def to_auth(credentials):
        seen.setdefault("to_auth_credentials", []).append(dict(credentials))
        return {
            "headers": {"Authorization": f"Bearer {credentials['access']}"},
            "base_url": "https://credential.example.test/v2",
        }

    runtime, storage = _oauth_runtime({
        "name": "Modern OAuth",
        "login": login,
        "refresh": refresh,
        "to_auth": to_auth,
    })
    callbacks = _OAuthCallbacks()

    result = asyncio.run(runtime.login_provider("modern-oauth", callbacks))

    assert result == {"type": "oauth", "expires": 1}
    assert callbacks.drain_count == 1
    assert storage.values["modern-oauth"] == {
        "type": "oauth",
        "access": "access-two",
        "refresh": "refresh-two",
        "expires": 4_102_444_800_000,
        "accountId": "account-two",
    }
    json.dumps(storage.values["modern-oauth"])

    spec = runtime.resolve_adapter_spec("modern-oauth", "model-1")
    assert spec.api_key == ""
    assert spec.headers == {"Authorization": "Bearer access-two"}
    assert spec.base_url == "https://credential.example.test/v2"

    # Pi follows login with a provider-model refresh, so an already-expired
    # login credential is rotated before login returns. A second refresh is a
    # no-op against the now-valid credential.
    assert asyncio.run(runtime.refresh_oauth_credentials("modern-oauth")) is False
    assert seen["refresh_credentials"] == {
        "type": "oauth",
        "access": "access-one",
        "refresh": "refresh-one",
        "expires": 1,
        "accountId": "account-one",
    }
    assert all(
        "_minicode_auth" not in credentials
        for credentials in seen["to_auth_credentials"]
    )
    assert runtime.resolve_adapter_spec("modern-oauth", "model-1").headers == {
        "Authorization": "Bearer access-two",
    }


def test_oauth_refresh_callback_receives_the_same_optional_signal() -> None:
    class Signal:
        aborted = False

    signal = Signal()
    seen: dict[str, object] = {}

    async def refresh(credentials, received_signal):
        seen["credentials"] = dict(credentials)
        seen["signal"] = received_signal
        return {
            "type": "oauth",
            "access": "access-two",
            "refresh": "refresh-two",
            "expires": 4_102_444_800_000,
        }

    async def to_auth(credentials):
        return {"headers": {"Authorization": f"Bearer {credentials['access']}"}}

    runtime, storage = _oauth_runtime({
        "name": "Signal OAuth",
        "login": lambda _callbacks: None,
        "refresh": refresh,
        "to_auth": to_auth,
    })
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "access-one",
        "refresh": "refresh-one",
        "expires": 1,
    }

    assert (
        asyncio.run(
            runtime.refresh_oauth_credentials("modern-oauth", signal=signal)
        )
        is True
    )
    assert seen["credentials"] == {
        "type": "oauth",
        "access": "access-one",
        "refresh": "refresh-one",
        "expires": 1,
    }
    assert seen["signal"] is signal


def test_oauth_refresh_single_argument_callback_remains_signal_compatible() -> None:
    class Signal:
        aborted = False

    signal = Signal()
    calls = 0

    async def refresh(credentials):
        nonlocal calls
        calls += 1
        assert credentials["access"] == "legacy-access"
        return {
            "type": "oauth",
            "access": "legacy-access-two",
            "refresh": "legacy-refresh-two",
            "expires": 4_102_444_800_000,
        }

    async def to_auth(credentials):
        return {"api_key": credentials["access"]}

    runtime, storage = _oauth_runtime({
        "name": "Single Argument OAuth",
        "login": lambda _callbacks: None,
        "refresh": refresh,
        "to_auth": to_auth,
    })
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "legacy-access",
        "refresh": "legacy-refresh",
        "expires": 1,
    }

    assert (
        asyncio.run(
            runtime.refresh_oauth_credentials("modern-oauth", signal=signal)
        )
        is True
    )
    assert calls == 1
    assert runtime.resolve_adapter_spec("modern-oauth", "model-1").api_key == (
        "legacy-access-two"
    )


def test_oauth_abort_during_refresh_keeps_last_good_availability_snapshot() -> None:
    class Signal:
        aborted = False

    signal = Signal()

    async def refresh(_credentials, received_signal):
        assert received_signal is signal
        signal.aborted = True
        return {
            "type": "oauth",
            "access": "rotated-after-abort",
            "refresh": "refresh-two",
            "expires": 4_102_444_800_000,
        }

    async def to_auth(credentials):
        return {"api_key": credentials["access"]}

    runtime, storage = _oauth_runtime({
        "name": "Abort OAuth",
        "login": lambda _callbacks: None,
        "refresh": refresh,
        "to_auth": to_auth,
    })
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "baseline-access",
        "refresh": "baseline-refresh",
        "expires": 4_102_444_800_000,
    }
    asyncio.run(runtime.refresh_provider_auth("modern-oauth"))
    baseline = runtime.get_available_snapshot()
    assert [model.id for model in baseline] == ["model-1"]
    publish_calls = 0
    publish_snapshot = runtime._refresh_available_snapshot

    def track_publish(*, apply_filters):
        nonlocal publish_calls
        publish_calls += 1
        publish_snapshot(apply_filters=apply_filters)

    runtime._refresh_available_snapshot = track_publish  # type: ignore[method-assign]

    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "expired-access",
        "refresh": "expired-refresh",
        "expires": 1,
    }
    assert (
        asyncio.run(
            runtime.refresh_oauth_credentials("modern-oauth", signal=signal)
        )
        is True
    )

    # Pi's CredentialStore transaction may persist a rotated token returned by
    # the provider, but cancellation must stop auth derivation and publication.
    assert storage.values["modern-oauth"]["access"] == "rotated-after-abort"
    assert runtime.get_available_snapshot() == baseline
    assert publish_calls == 0


def test_dynamic_model_callback_is_not_invoked_after_auth_signal_aborts() -> None:
    class Signal:
        aborted = False

    signal = Signal()
    refresh_model_calls = 0

    async def refresh_oauth(_credentials, received_signal):
        assert received_signal is signal
        signal.aborted = True
        return {
            "type": "oauth",
            "access": "rotated-access",
            "refresh": "rotated-refresh",
            "expires": 4_102_444_800_000,
        }

    async def to_auth(credentials):
        return {"api_key": credentials["access"]}

    async def refresh_models(_context):
        nonlocal refresh_model_calls
        refresh_model_calls += 1
        return []

    runtime = ModelRuntime()
    runtime._base_providers = {}
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    runtime.register_provider("oauth-dynamic", {
        "name": "OAuth Dynamic",
        "auth": {
            "oauth": {
                "name": "OAuth Dynamic",
                "login": lambda _callbacks: None,
                "refresh": refresh_oauth,
                "to_auth": to_auth,
            }
        },
        "models": [{
            "id": "model-1",
            "api": "openai-completions",
            "base_url": "https://models.example.test/v1",
        }],
        "refresh_models": refresh_models,
    })
    storage.values["oauth-dynamic"] = {
        "type": "oauth",
        "access": "expired-access",
        "refresh": "expired-refresh",
        "expires": 1,
    }

    asyncio.run(runtime.refresh_dynamic_models(signal=signal))

    assert signal.aborted is True
    assert refresh_model_calls == 0


def test_legacy_pi_oauth_login_refresh_and_get_api_key_remain_compatible() -> None:
    seen: dict[str, object] = {}

    async def login(callbacks):
        seen["callbacks"] = callbacks
        return {"access": "legacy-one", "refresh": "legacy-refresh", "expires": 1}

    async def refresh_token(credentials):
        seen["refresh_credentials"] = dict(credentials)
        return {
            "access": "legacy-two",
            "refresh": "legacy-refresh-two",
            "expires": 4_102_444_800_000,
        }

    def get_api_key(credentials):
        return credentials["access"]

    runtime = ModelRuntime()
    runtime._base_providers = {}
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    runtime.register_provider("legacy-oauth", {
        "oauth": {
            "name": "Legacy OAuth",
            "login": login,
            "refresh": refresh_token,
            "get_api_key": get_api_key,
        },
        "auth_header": True,
        "headers": {"X-Provider": "legacy"},
        "models": [{
            "id": "legacy-model",
            "api": "openai-completions",
            "base_url": "https://legacy.example.test/v1",
        }],
    })

    callbacks = _OAuthCallbacks()
    asyncio.run(runtime.login_provider("legacy-oauth", callbacks))
    assert storage.values["legacy-oauth"]["type"] == "oauth"
    assert "_minicode_auth" not in storage.values["legacy-oauth"]
    assert storage.values["legacy-oauth"]["access"] == "legacy-two"
    assert asyncio.run(runtime.refresh_oauth_credentials("legacy-oauth")) is False
    assert seen["refresh_credentials"] == {
        "type": "oauth",
        "access": "legacy-one",
        "refresh": "legacy-refresh",
        "expires": 1,
    }
    assert runtime.resolve_provider_auth("legacy-oauth") == {
        "auth": {
            "api_key": "legacy-two",
            "headers": {
                "X-Provider": "legacy",
                "Authorization": "Bearer legacy-two",
            },
        },
        "source": "oauth",
    }


def test_oauth_delivery_failure_and_invalid_credentials_never_reach_storage() -> None:
    async def login(_callbacks):
        return {
            "type": "oauth",
            "access": "access-one",
            "refresh": "refresh-one",
            "expires": 1,
        }

    async def to_auth(credentials):
        return {"api_key": credentials["access"]}

    runtime, storage = _oauth_runtime({
        "name": "Modern OAuth",
        "login": login,
        "refresh": login,
        "to_auth": to_auth,
    })
    with pytest.raises(ConnectionError, match="notification delivery failed"):
        asyncio.run(runtime.login_provider(
            "modern-oauth",
            _OAuthCallbacks(drain_error=ConnectionError("notification delivery failed")),
        ))
    assert storage.values == {}

    invalid_credentials = (
        {"refresh": "refresh", "expires": 1},
        {"access": "access", "expires": 1},
        {"access": "access", "refresh": "refresh", "expires": float("inf")},
    )
    for credentials in invalid_credentials:
        async def invalid_login(_callbacks, payload=credentials):
            return payload

        invalid_runtime, invalid_storage = _oauth_runtime({
            "name": "Invalid OAuth",
            "login": invalid_login,
            "refresh": invalid_login,
            "to_auth": to_auth,
        })
        with pytest.raises(ProviderRegistrationError):
            asyncio.run(invalid_runtime.login_provider("modern-oauth", _OAuthCallbacks()))
        assert invalid_storage.values == {}


def test_oauth_auth_projection_rejects_header_injection_before_storage() -> None:
    async def login(_callbacks):
        return {
            "type": "oauth",
            "access": "access-one",
            "refresh": "refresh-one",
            "expires": 1,
        }

    async def to_auth(_credentials):
        return {"headers": {"Authorization": "Bearer safe\r\nX-Injected: yes"}}

    runtime, storage = _oauth_runtime({
        "name": "Unsafe OAuth",
        "login": login,
        "refresh": login,
        "to_auth": to_auth,
    })

    with pytest.raises(ProviderRegistrationError, match="control separators"):
        asyncio.run(runtime.login_provider("modern-oauth", _OAuthCallbacks()))
    assert storage.values == {}






def test_provider_layers_apply_extension_oauth_then_models_json_override_last() -> None:
    runtime = ModelRuntime(provider_configs={
        "layered": {
            "api_key": "configured-key",
            "models": [
                {
                    "id": "model-1",
                    "name": "Config Model",
                    # models.json is validated independently before an
                    # extension can register a custom streamSimple API. Use a
                    # connected standard API in this lower layer; the
                    # extension replacement below still proves that the
                    # provider-native extension layer wins afterward.
                    "api": "openai-completions",
                    "base_url": "https://config.example.test/v1",
                    "cost": {
                        "input": 3,
                        "output": 4,
                        "cacheRead": 5,
                        "cacheWrite": 6,
                    },
                    "headers": {
                        "X-Config-Definition": "definition",
                        "X-Case": "config-definition",
                    },
                }
            ],
            "model_overrides": {
                "model-1": {
                    "name": "Final Override",
                    "reasoning": True,
                    "thinking_level_map": {"high": "override-high"},
                    "cost": {"output": 99},
                    "headers": {
                        "X-Override": "override",
                        "X-Case": "override",
                    },
                }
            },
        }
    })
    runtime._base_providers = {
        "layered": {
            "name": "Base Provider",
            "api_key": "base-key",
            "models": (
                ModelDefinition(
                    provider="layered",
                    id="model-1",
                    name="Base Model",
                    api="provider-native",
                    base_url="https://base.example.test/v1",
                    cost={
                        "input": 1,
                        "output": 2,
                        "cacheRead": 3,
                        "cacheWrite": 4,
                    },
                    context_window=128_000,
                    max_context_window=128_000,
                    max_tokens=16_384,
                    max_output_tokens=16_384,
                ),
            ),
        }
    }

    def modify_models(models, _credential):
        return [
            {
                **models[0],
                "name": "OAuth Projection",
                "cost": {**models[0]["cost"], "input": 7},
                "thinking_level_map": {
                    **(models[0].get("thinking_level_map") or {}),
                    "medium": "oauth-medium",
                },
            }
        ]

    async def login(_callbacks):
        raise AssertionError("login should not run")

    async def refresh(credentials):
        return credentials

    runtime.register_provider(
        "layered",
        {
            "name": "Extension Provider",
            "api": "openai-completions",
            "oauth": {
                "name": "Layered OAuth",
                "login": login,
                "refresh": refresh,
                "to_auth": lambda credential: {"api_key": credential["access"]},
                "modify_models": modify_models,
            },
            "models": [
                {
                    "id": "model-1",
                    "name": "Extension Model",
                    "api": "openai-completions",
                    "base_url": "https://extension.example.test/v1",
                    "thinking_level_map": {"low": "extension-low"},
                    "cost": {
                        "input": 5,
                        "output": 6,
                        "cacheRead": 7,
                        "cacheWrite": 8,
                    },
                    "headers": {
                        "X-Extension": "extension",
                        "X-Case": "extension",
                    },
                }
            ],
        },
    )
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    storage.values["layered"] = {
        "type": "oauth",
        "access": "access",
        "refresh": "refresh",
        "expires": 4_102_444_800_000,
    }
    asyncio.run(runtime.refresh_dynamic_models(allow_network=False, force=True))

    model = runtime.get_model("layered", "model-1")

    assert model is not None
    assert model.name == "Final Override"
    assert model.reasoning is True
    assert model.base_url == "https://extension.example.test/v1"
    assert model.thinking_level_map == {
        "low": "extension-low",
        "medium": "oauth-medium",
        "high": "override-high",
    }
    assert model.cost == {
        "input": 7,
        "output": 99,
        "cacheRead": 7,
        "cacheWrite": 8,
    }
    assert model.headers == {
        "X-Extension": "extension",
        "X-Case": "extension",
        "X-Override": "override",
        "X-Config-Definition": "definition",
    }


@pytest.mark.skip(reason="obsolete upstream compat/model override shape; MiniCode model schema is independent")
def test_models_json_compat_cost_and_thinking_overrides_use_pi_merge_rules() -> None:
    runtime = ModelRuntime(provider_configs={
        "compat-provider": {
            "api_key": "configured-key",
            "compat": {
                "openRouterRouting": {"zdr": True},
                "chatTemplateKwargs": {"configured": 2},
                "opaque": {"configured": 2},
            },
            "model_overrides": {
                "model-1": {
                    "thinking_level_map": {"high": "override-high"},
                    "cost": {
                        "output": 22,
                        "tiers": [{
                            "inputTokensAbove": 10,
                            "input": 21,
                            "output": 22,
                            "cacheRead": 23,
                            "cacheWrite": 24,
                        }],
                    },
                    "compat": {
                        "openRouterRouting": {"require_parameters": True},
                        "chatTemplateKwargs": {"base": 9},
                        "opaque": {"final": 3},
                    },
                }
            },
        }
    })
    runtime._base_providers = {
        "compat-provider": {
            "name": "Compat Provider",
            "api_key": "base-key",
            "models": (
                ModelDefinition(
                    provider="compat-provider",
                    id="model-1",
                    name="Compat Model",
                    api="openai-completions",
                    base_url="https://compat.example.test/v1",
                    thinking_level_map={"low": "base-low"},
                    cost={
                        "input": 11,
                        "output": 12,
                        "cacheRead": 13,
                        "cacheWrite": 14,
                        "tiers": [{"inputTokensAbove": 1}],
                    },
                    context_window=128_000,
                    max_context_window=128_000,
                    max_tokens=16_384,
                    max_output_tokens=16_384,
                    compat={
                        "openRouterRouting": {
                            "order": ["base"],
                            "zdr": False,
                        },
                        "chatTemplateKwargs": {"base": 1},
                        "opaque": {"base": 1},
                    },
                ),
            ),
        }
    }

    model = runtime.get_model("compat-provider", "model-1")

    assert model is not None
    assert model.thinking_level_map == {
        "low": "base-low",
        "high": "override-high",
    }
    assert model.cost == {
        "input": 11,
        "output": 22,
        "cacheRead": 13,
        "cacheWrite": 14,
        "tiers": [{
            "inputTokensAbove": 10,
            "input": 21,
            "output": 22,
            "cacheRead": 23,
            "cacheWrite": 24,
        }],
    }
    assert model.compat == {
        "openRouterRouting": {
            "order": ["base"],
            "zdr": True,
            "require_parameters": True,
        },
        "chatTemplateKwargs": {"base": 9, "configured": 2},
        "opaque": {"final": 3},
    }




@pytest.mark.parametrize(
    ("provider_id", "config", "expected"),
    [
        (
            "metadata-only",
            {
                "name": "Metadata only",
                "api": "openai-completions",
                "futureField": {"opaque": True},
            },
            'must specify "base_url", "headers", "api_key", "auth_header", '
            '"model_overrides", or "models"',
        ),
    ],
)
def test_config_only_composition_failure_is_not_published_as_a_provider(
    provider_id,
    config,
    expected,
) -> None:
    runtime = ModelRuntime(provider_configs={provider_id: config})

    assert runtime.get_models(provider_id) == ()
    assert runtime.get_provider(provider_id) is None
    assert provider_id not in {provider.id for provider in runtime.get_providers()}
    assert expected in str(runtime.get_error())


def test_models_json_jsonc_file_is_loaded_and_invalid_file_is_nonfatal(tmp_path) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(
        """
        {
          // Provider comments are accepted like Pi's models.json.
          "providers": {
            "jsonc-provider": {
              "api_key": "configured-key",
              "models": [{
                "id": "model-1",
                "api": "openai-completions",
                "base_url": "https://jsonc.example.test/v1",
              },],
              "model_overrides": {
                "model-1": {"name": "JSONC Override",},
              },
            },
          },
        }
        """,
        encoding="utf-8",
    )

    runtime = ModelRuntime(models_path=models_path)
    model = runtime.get_model("jsonc-provider", "model-1")

    assert model is not None
    assert model.name == "JSONC Override"
    assert runtime.get_registered_provider_config("jsonc-provider") is None

    models_path.write_text('{"providers": { /* unterminated', encoding="utf-8")
    broken = ModelRuntime(models_path=models_path)
    assert "Failed to parse models.json" in str(broken.get_error())


def test_models_json_accepts_bom_and_rejects_duplicate_keys(tmp_path) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(
        '\ufeff{"providers":{"bom-provider":{"api_key":"configured-key","models":[]}}}',
        encoding="utf-8",
    )
    runtime = ModelRuntime(models_path=models_path)
    assert runtime.get_error() is None

    models_path.write_text(
        '{"providers":{"duplicate":{"api_key":"first","api_key":"second","models":[]}}}',
        encoding="utf-8",
    )
    duplicate = ModelRuntime(models_path=models_path)
    assert "duplicate JSON object key: api_key" in str(duplicate.get_error())


@pytest.mark.skip(reason="obsolete upstream open TypeBox metadata; MiniCode preserves only its canonical model fields")
def test_models_json_open_typebox_objects_preserve_unknown_fields(tmp_path) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps({
            "rootExtension": {"enabled": True},
            "providers": {
                "open-schema-provider": {
                    "api_key": "configured-key",
                    "providerExtension": {"enabled": True},
                    "compat": {"opaque": {"provider": True}},
                    "models": [{
                        "id": "model-1",
                        "api": "openai-completions",
                        "base_url": "https://open-schema.example.test/v1",
                        "modelExtension": {"enabled": True},
                        "thinking_level_map": {
                            "low": "low-wire",
                            "future": {"opaque": True},
                        },
                        "cost": {
                            "input": 1,
                            "output": 2,
                            "cacheRead": 3,
                            "cacheWrite": 4,
                            "futureRate": 5,
                            "tiers": [{
                                "inputTokensAbove": 10,
                                "input": 11,
                                "output": 12,
                                "cacheRead": 13,
                                "cacheWrite": 14,
                                "futureTier": True,
                            }],
                        },
                    }],
                    "model_overrides": {
                        "model-1": {
                            "overrideExtension": True,
                            "compat": {"opaque": {"override": True}},
                        }
                    },
                }
            },
        }),
        encoding="utf-8",
    )

    runtime = ModelRuntime(models_path=models_path)
    model = runtime.get_model("open-schema-provider", "model-1")

    assert runtime.get_error() is None
    assert model is not None
    assert model.thinking_level_map == {
        "low": "low-wire",
        "future": {"opaque": True},
    }
    assert model.cost["futureRate"] == 5
    assert model.cost["tiers"][0]["futureTier"] is True
    assert model.compat == {"opaque": {"override": True}}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "providers must be an object"),
        (
            {
                "providers": {
                    "invalid": {
                        "api_key": "key",
                        "models": [{
                            "id": "model-1",
                            "api": "openai-completions",
                            "base_url": "https://invalid.example.test/v1",
                            "thinking_level_map": {"low": 1},
                        }],
                    }
                }
            },
            "thinking_level_map contains an invalid thinking-level entry",
        ),
        (
            {
                "providers": {
                    "invalid": {
                        "api_key": "key",
                        "models": [{
                            "id": "model-1",
                            "api": "openai-completions",
                            "base_url": "https://invalid.example.test/v1",
                            "input": ["audio"],
                        }],
                    }
                }
            },
            "input may contain only 'text' and 'image'",
        ),
        (
            {
                "providers": {
                    "invalid": {
                        "api_key": "key",
                        "models": [{
                            "id": "model-1",
                            "api": "openai-completions",
                            "base_url": "https://invalid.example.test/v1",
                            "cost": {"input": 1},
                        }],
                    }
                }
            },
            "cost is missing required field(s)",
        ),
        (
            {
                "providers": {
                    "invalid": {
                        "api_key": "key",
                        "models": [{
                            "id": "model-1",
                            "api": "openai-completions",
                            "base_url": "https://invalid.example.test/v1",
                            "cost": {
                                "input": 1,
                                "output": 2,
                                "cacheRead": 3,
                                "cacheWrite": 4,
                                "tiers": [{"inputTokensAbove": 10}],
                            },
                        }],
                    }
                }
            },
            "cost.tiers[0] is missing required field(s)",
        ),
    ],
)
def test_models_json_known_schema_fields_are_validated(
    tmp_path,
    payload,
    expected,
) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps(payload), encoding="utf-8")

    runtime = ModelRuntime(models_path=models_path)

    # A broken models.json layer is rejected without erasing independent
    # native/settings providers.  The invalid file must not publish its
    # provider into the effective registry.
    assert runtime.get_provider("invalid") is None
    assert "Invalid models.json schema" in str(runtime.get_error())
    assert expected in str(runtime.get_error())


def test_models_json_rejects_non_json_number_constants(tmp_path) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(
        '{"providers":{"invalid":{"models":[{"id":"m","cost":'
        '{"input":NaN,"output":1,"cacheRead":1,"cacheWrite":1}}]}}}',
        encoding="utf-8",
    )

    runtime = ModelRuntime(models_path=models_path)

    assert runtime.get_provider("invalid") is None
    assert "Failed to parse models.json" in str(runtime.get_error())
    assert "invalid JSON number constant: NaN" in str(runtime.get_error())


def test_models_json_override_does_not_invent_a_context_window_relation() -> None:
    runtime = ModelRuntime(provider_configs={
        "override-provider": {
            "api_key": "configured-key",
            "model_overrides": {"model-1": {"max_tokens": 1_000}},
        }
    })
    base_model = ModelDefinition(
        provider="override-provider",
        id="model-1",
        name="Base Model",
        api="openai-completions",
        base_url="https://override.example.test/v1",
        context_window=100,
        max_context_window=100,
        max_tokens=50,
        max_output_tokens=50,
    )
    runtime._base_providers = {
        "override-provider": {
            "name": "Override Provider",
            "api_key": "base-key",
            "models": (base_model,),
        }
    }

    model = runtime.get_model("override-provider", "model-1")

    assert model is not None
    assert model.context_window == 100
    assert model.max_tokens == 1_000
    assert runtime.get_error() is None


def test_native_base_refresh_runs_before_extension_refresh(monkeypatch) -> None:
    events: list[str] = []

    async def resolve(input_value):
        credential = input_value.get("credential") or {}
        return {"auth": {"api_key": credential.get("key")}}

    async def base_refresh(_context):
        events.append("base")

    async def extension_refresh(_context):
        events.append("extension")
        return [
            {
                "id": "model-2",
                "api": "openai-completions",
                "base_url": "https://refresh.example.test/v2",
            }
        ]

    base = {
        "name": "Refresh Base",
        "auth": {"api_key": {"resolve": resolve}},
        "models": (
            ModelDefinition(
                provider="refresh-provider",
                id="model-1",
                name="Model One",
                api="provider-native",
                base_url="https://refresh.example.test/v1",
                context_window=128_000,
                max_context_window=128_000,
                max_tokens=16_384,
                max_output_tokens=16_384,
            ),
        ),
        "refresh_models": base_refresh,
    }
    runtime = ModelRuntime()
    runtime._base_providers = {"refresh-provider": base}
    monkeypatch.setattr(
        runtime,
        "_load_base_providers",
        lambda: {"refresh-provider": base},
    )
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    storage.values["refresh-provider"] = {
        "type": "api_key",
        "key": "stored-key",
    }
    runtime.register_provider(
        "refresh-provider",
        {
            "api": "openai-completions",
            "refresh_models": extension_refresh,
        },
    )

    asyncio.run(runtime.refresh_dynamic_models(force=True))

    assert events == ["base", "extension"]
    assert runtime.get_model("refresh-provider", "model-2") is not None


def test_extension_filter_models_is_not_promoted_to_composed_provider_filter() -> None:
    filter_calls = 0

    def extension_filter(models, _credential):
        nonlocal filter_calls
        filter_calls += 1
        return []

    runtime = ModelRuntime()
    runtime._base_providers = {}
    runtime.register_provider(
        "extension-filter",
        {
            "api_key": "configured-key",
            "models": [
                {
                    "id": "model-1",
                    "api": "openai-completions",
                    "base_url": "https://extension-filter.example.test/v1",
                }
            ],
            "filter_models": extension_filter,
        },
    )

    assert [model.id for model in runtime.get_available("extension-filter")] == [
        "model-1"
    ]
    assert filter_calls == 0


def test_provider_auth_context_ignores_blank_values_and_falls_back_to_ambient(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PI_CONTEXT_EMPTY_AMBIENT", "")
    monkeypatch.setenv("PI_CONTEXT_EXPLICIT_EMPTY", "ambient-empty-fallback")
    monkeypatch.setenv("PI_CONTEXT_EXPLICIT_SPACE", "ambient-space-fallback")
    monkeypatch.setenv("PI_CONTEXT_EXPLICIT_VALUE", "ambient-must-not-win")
    context = definitions_module._ProviderAuthContext({
        "PI_CONTEXT_EXPLICIT_EMPTY": "",
        "PI_CONTEXT_EXPLICIT_SPACE": "   ",
        "PI_CONTEXT_EXPLICIT_VALUE": "explicit-value",
    })

    async def resolve() -> tuple[object, ...]:
        return (
            await context.env("PI_CONTEXT_EMPTY_AMBIENT"),
            await context.env("PI_CONTEXT_EXPLICIT_EMPTY"),
            await context.env("PI_CONTEXT_EXPLICIT_SPACE"),
            await context.env("PI_CONTEXT_EXPLICIT_VALUE"),
            await context.env("PI_CONTEXT_MISSING"),
        )

    assert asyncio.run(resolve()) == (
        None,
        "ambient-empty-fallback",
        "ambient-space-fallback",
        "explicit-value",
        None,
    )


@pytest.mark.skip(reason="obsolete provider layering model; MiniCode uses canonical base/config/extension registry")
def test_refresh_provider_auth_without_id_visits_every_composed_provider_layer(
    monkeypatch,
) -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {"base-layer": {}}
    runtime._transport_providers = {"native-layer": {}}
    runtime._model_configs = {"config-layer": {}}
    runtime._extension_providers = {"extension-layer": {}}
    seen: list[str] = []

    def handler(provider_id: str):
        async def resolve(_input_value):
            seen.append(provider_id)
            return {
                "auth": {"api_key": f"{provider_id}-key"},
                "source": provider_id,
            }

        return {"resolve": resolve}

    handlers = {
        provider_id: handler(provider_id)
        for provider_id in (
            "base-layer",
            "native-layer",
            "config-layer",
            "extension-layer",
        )
    }
    monkeypatch.setattr(
        runtime,
        "_api_key_provider",
        lambda provider_id: handlers.get(provider_id),
    )

    asyncio.run(runtime.refresh_provider_auth(None, publish_snapshot=False))

    assert seen == [
        "base-layer",
        "native-layer",
        "config-layer",
        "extension-layer",
    ]


def test_modern_api_key_auth_supports_async_ambient_header_only_base_url_and_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMBIENT_PROVIDER_KEY", "ambient-key")
    monkeypatch.setenv("CONFIGURED_HEADER", "configured-value")
    seen: dict[str, object] = {}

    async def check(input_value):
        seen["check_credential"] = input_value.get("credential")
        assert await input_value.ctx.env("AMBIENT_PROVIDER_KEY") == "ambient-key"
        return {"type": "api_key", "source": "AMBIENT_PROVIDER_KEY"}

    async def resolve(input_value):
        seen["resolve_credential"] = input_value.get("credential")
        return {
            "auth": {
                "headers": {
                    "authorization": "callback-value",
                    "X-Callback": "present",
                },
                "base_url": "https://credential.example.test/v2",
            },
            "env": {"PROVIDER_ACCOUNT": "account-1"},
            "source": "ambient",
        }

    runtime, _storage = _modern_api_key_runtime(
        {"check": check, "resolve": resolve},
        headers={
            "Authorization": "configured-wins",
            "X-Configured": "$CONFIGURED_HEADER",
        },
    )

    asyncio.run(runtime.refresh_provider_auth("modern-auth"))
    spec = runtime.resolve_adapter_spec("modern-auth", "model-1")

    assert seen == {"check_credential": None, "resolve_credential": None}
    assert spec.api_key == ""
    assert spec.headers == {
        "Authorization": "configured-wins",
        "X-Callback": "present",
        "X-Configured": "configured-value",
    }
    assert spec.base_url == "https://credential.example.test/v2"
    assert spec.model.base_url == "https://credential.example.test/v2"
    assert spec.env == {"PROVIDER_ACCOUNT": "account-1"}
    assert runtime.get_provider_auth_status("modern-auth") == {
        "configured": True,
        "source": "AMBIENT_PROVIDER_KEY",
        "oauth_supported": False,
    }


def test_modern_api_key_keyless_provider_is_configured_only_after_resolve() -> None:
    calls = 0

    async def resolve(_input_value):
        nonlocal calls
        calls += 1
        return {"auth": {}, "source": "local socket"}

    runtime, _storage = _modern_api_key_runtime({"resolve": resolve})

    assert runtime.has_configured_auth("modern-auth") is False
    asyncio.run(runtime.refresh_provider_auth("modern-auth"))

    assert calls == 1
    assert runtime.has_configured_auth("modern-auth") is True
    spec = runtime.resolve_adapter_spec("modern-auth", "model-1")
    assert spec.api_key == ""
    assert spec.headers == {}


def test_api_key_check_unavailable_skips_resolve_and_clears_availability() -> None:
    resolve_calls = 0

    async def check(_input_value):
        return None

    async def resolve(_input_value):
        nonlocal resolve_calls
        resolve_calls += 1
        return {"auth": {"api_key": "must-not-run"}}

    runtime, _storage = _modern_api_key_runtime(
        {"check": check, "resolve": resolve}
    )

    asyncio.run(runtime.refresh_provider_auth("modern-auth"))

    assert resolve_calls == 0
    assert runtime.has_configured_auth("modern-auth") is False
    assert runtime.resolve_provider_auth("modern-auth") is None


def test_api_key_resolve_failure_preserves_last_good_generation_cache() -> None:
    fail = False

    async def resolve(_input_value):
        if fail:
            raise ConnectionError("temporary resolver failure")
        return {"auth": {"api_key": "last-good"}, "source": "ambient"}

    runtime, _storage = _modern_api_key_runtime({"resolve": resolve})
    asyncio.run(runtime.refresh_provider_auth("modern-auth"))
    fail = True

    with pytest.raises(ConnectionError, match="temporary resolver failure"):
        asyncio.run(runtime.refresh_provider_auth("modern-auth"))

    assert runtime.resolve_adapter_spec("modern-auth", "model-1").api_key == "last-good"


def test_stored_credential_type_owns_provider_and_blocks_ambient_fallback() -> None:
    api_key_calls: list[object] = []

    async def api_key_resolve(input_value):
        api_key_calls.append(input_value.get("credential"))
        return {"auth": {"api_key": "ambient"}, "source": "ambient"}

    runtime, storage = _modern_api_key_runtime({"resolve": api_key_resolve})
    storage.values["modern-auth"] = {
        "type": "oauth",
        "access": "orphaned-access",
        "refresh": "orphaned-refresh",
        "expires": 4_102_444_800_000,
    }

    asyncio.run(runtime.refresh_provider_auth("modern-auth"))

    assert api_key_calls == []
    assert runtime.resolve_provider_auth("modern-auth") is None


def test_stored_api_key_routes_through_modern_handler_with_credential_env() -> None:
    seen: dict[str, Any] = {}

    async def resolve(input_value):
        credential = dict(input_value.credential)
        seen.update(credential)
        return {
            "auth": {"api_key": credential["key"]},
            "source": "stored credential",
        }

    runtime, storage = _modern_api_key_runtime(
        {"resolve": resolve},
        headers={"X-Account": "$ACCOUNT_ID"},
    )
    storage.values["modern-auth"] = {
        "type": "api_key",
        "key": "stored-key",
        "env": {"ACCOUNT_ID": "account-7"},
    }

    asyncio.run(runtime.refresh_provider_auth("modern-auth"))
    spec = runtime.resolve_adapter_spec("modern-auth", "model-1")

    assert seen == {
        "type": "api_key",
        "key": "stored-key",
        "env": {"ACCOUNT_ID": "account-7"},
    }
    assert spec.api_key == "stored-key"
    assert spec.headers == {"X-Account": "account-7"}
    assert spec.env == {"ACCOUNT_ID": "account-7"}


def test_auth_header_fails_closed_without_resolved_api_key() -> None:
    async def resolve(_input_value):
        return {"auth": {"headers": {"X-Auth": "header-only"}}}

    runtime, _storage = _modern_api_key_runtime(
        {"resolve": resolve},
        auth_header=True,
    )

    with pytest.raises(ProviderRegistrationError, match="auth_header requires"):
        asyncio.run(runtime.refresh_provider_auth("modern-auth"))


def test_provider_registration_rejects_header_injection_and_non_boolean_auth_header() -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}
    base = {
        "auth": {"api_key": {"resolve": lambda _input: {"auth": {"api_key": "x"}}}},
        "api": "openai-completions",
        "models": [
            {
                "id": "model-1",
                "base_url": "https://example.test/v1",
            }
        ],
    }

    with pytest.raises(ProviderRegistrationError, match="control separators"):
        runtime.register_provider(
            "unsafe",
            {**base, "headers": {"X-Test": "safe\r\nX-Injected: yes"}},
        )
    with pytest.raises(ProviderRegistrationError, match="auth_header must be a boolean"):
        runtime.register_provider("unsafe", {**base, "auth_header": "false"})


def test_retired_runtime_never_publishes_async_api_key_callback_result() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def resolve(_input_value):
            entered.set()
            await release.wait()
            return {"auth": {"api_key": "late-key"}}

        runtime, _storage = _modern_api_key_runtime({"resolve": resolve})
        task = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
        await entered.wait()
        runtime.retire()
        release.set()
        with pytest.raises(RuntimeError, match="retired extension generation"):
            await task
        assert runtime._resolved_api_key_auth == {}
        assert runtime._api_key_auth_status == {}

    asyncio.run(run())


def test_unregister_reregister_generation_fence_rejects_late_pi_auth_result() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def old_resolve(_input_value):
            entered.set()
            await release.wait()
            return {"auth": {"api_key": "stale-key"}}

        async def new_resolve(_input_value):
            return {"auth": {"api_key": "fresh-key"}}

        runtime, _storage = _modern_api_key_runtime({"resolve": old_resolve})
        old_refresh = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
        await entered.wait()

        replacement = dict(runtime._extension_providers["modern-auth"])
        replacement["auth"] = {"api_key": {"resolve": new_resolve}}
        runtime.unregister_provider("modern-auth")
        runtime.register_provider("modern-auth", replacement)
        new_refresh = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
        await new_refresh

        release.set()
        with pytest.raises(RuntimeError, match="changed during an auth operation"):
            await old_refresh

        spec = runtime.resolve_adapter_spec("modern-auth", "model-1")
        assert spec.api_key == "fresh-key"
        assert "stale-key" not in json.dumps(runtime._resolved_api_key_auth)

    asyncio.run(run())


def test_sync_auth_resolution_requires_async_cache_warmup() -> None:
    async def resolve(_input_value):
        return {"auth": {"api_key": "async-key"}}

    runtime, _storage = _modern_api_key_runtime({"resolve": resolve})

    with pytest.raises(ProviderRegistrationError, match="asynchronous auth resolution"):
        runtime.resolve_provider_auth("modern-auth")


def test_oauth_future_expiry_skips_refresh_but_rederives_request_auth() -> None:
    refresh_calls = 0
    to_auth_calls = 0

    async def login(_callbacks):
        raise AssertionError("login should not run")

    async def refresh(_credential):
        nonlocal refresh_calls
        refresh_calls += 1
        raise AssertionError("future credential must not refresh")

    async def to_auth(credential):
        nonlocal to_auth_calls
        to_auth_calls += 1
        return {"api_key": credential["access"]}

    runtime, storage = _oauth_runtime(
        {"login": login, "refresh": refresh, "to_auth": to_auth}
    )
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "future-access",
        "refresh": "future-refresh",
        "expires": 4_102_444_800_000,
    }

    assert asyncio.run(runtime.refresh_oauth_credentials("modern-oauth")) is False
    assert refresh_calls == 0
    assert to_auth_calls == 1
    assert runtime.resolve_adapter_spec("modern-oauth", "model-1").api_key == "future-access"


def test_concurrent_oauth_resolution_refreshes_rotated_token_once() -> None:
    async def run() -> None:
        refresh_calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def login(_callbacks):
            raise AssertionError("login should not run")

        async def refresh(_credential):
            nonlocal refresh_calls
            refresh_calls += 1
            entered.set()
            await release.wait()
            return {
                "type": "oauth",
                "access": "rotated-access",
                "refresh": "rotated-refresh",
                "expires": 4_102_444_800_000,
            }

        async def to_auth(credential):
            return {"api_key": credential["access"]}

        runtime, storage = _oauth_runtime(
            {"login": login, "refresh": refresh, "to_auth": to_auth}
        )
        storage.values["modern-oauth"] = {
            "type": "oauth",
            "access": "expired-access",
            "refresh": "expired-refresh",
            "expires": 1,
        }

        first = asyncio.create_task(runtime.refresh_oauth_credentials("modern-oauth"))
        await entered.wait()
        second = asyncio.create_task(runtime.refresh_oauth_credentials("modern-oauth"))
        release.set()
        outcomes = await asyncio.gather(first, second)

        assert outcomes.count(True) == 1
        assert outcomes.count(False) == 1
        assert refresh_calls == 1
        assert storage.values["modern-oauth"]["access"] == "rotated-access"
        assert runtime.resolve_adapter_spec("modern-oauth", "model-1").api_key == "rotated-access"

    asyncio.run(run())


def test_oauth_logout_is_serialized_and_clears_request_cache() -> None:
    async def login(_callbacks):
        return {
            "type": "oauth",
            "access": "access",
            "refresh": "refresh",
            "expires": 4_102_444_800_000,
        }

    async def refresh(credential):
        return credential

    runtime, storage = _oauth_runtime(
        {"login": login, "refresh": refresh, "to_auth": lambda value: {"api_key": value["access"]}}
    )
    asyncio.run(runtime.login_provider("modern-oauth", _OAuthCallbacks()))
    assert runtime.resolve_provider_auth("modern-oauth") is not None

    assert asyncio.run(runtime.logout_provider("modern-oauth")) is True

    assert storage.values == {}
    assert runtime.resolve_provider_auth("modern-oauth") is None
    assert runtime._resolved_oauth_auth == {}


def test_oauth_modifier_can_change_full_model_shape_and_filter_all_models() -> None:
    empty = False

    async def login(_callbacks):
        raise AssertionError("login should not run")

    async def refresh(credential):
        return credential

    def modify_models(models, _credential):
        if empty:
            return []
        return [
            {
                **models[0],
                "name": "Credential Model",
                "reasoning": True,
                "context_window": 4_096,
                "max_tokens": 2_048,
                "headers": {"X-Model": "credential"},
            }
        ]

    runtime, storage = _oauth_runtime(
        {
            "login": login,
            "refresh": refresh,
            "to_auth": lambda credential: {"api_key": credential["access"]},
            "modify_models": modify_models,
        }
    )
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "access",
        "refresh": "refresh",
        "expires": 4_102_444_800_000,
    }
    asyncio.run(runtime.refresh_dynamic_models(allow_network=False, force=True))

    model = runtime.get_model("modern-oauth", "model-1")
    assert model is not None
    assert model.name == "Credential Model"
    assert model.reasoning is True
    assert model.context_window == 4_096
    assert model.max_tokens == 2_048
    assert model.headers == {"X-Model": "credential"}

    empty = True
    assert runtime.get_models("modern-oauth") == ()


def test_oauth_modifier_uses_refresh_captured_credential_and_offline_refresh_does_not_rotate() -> None:
    refresh_calls = 0

    async def login(_callbacks):
        raise AssertionError("login should not run")

    async def refresh(credential):
        nonlocal refresh_calls
        refresh_calls += 1
        return {
            **credential,
            "access": "network-rotated",
            "expires": 4_102_444_800_000,
        }

    def modify_models(models, credential):
        return [{**models[0], "name": f"Credential {credential['access']}"}]

    runtime, storage = _oauth_runtime(
        {
            "login": login,
            "refresh": refresh,
            "to_auth": lambda credential: {"api_key": credential["access"]},
            "modify_models": modify_models,
        }
    )
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "captured-one",
        "refresh": "refresh-one",
        "expires": 1,
    }

    asyncio.run(runtime.refresh_dynamic_models(allow_network=False, force=True))

    assert refresh_calls == 0
    assert runtime.get_model("modern-oauth", "model-1").name == "Credential captured-one"

    # Direct credential-store mutation must not silently change synchronous
    # getModels output. The next model refresh captures the new credential.
    storage.values["modern-oauth"] = {
        "type": "oauth",
        "access": "captured-two",
        "refresh": "refresh-two",
        "expires": 1,
    }
    assert runtime.get_model("modern-oauth", "model-1").name == "Credential captured-one"

    asyncio.run(runtime.refresh_dynamic_models(allow_network=False, force=True))

    assert refresh_calls == 0
    assert runtime.get_model("modern-oauth", "model-1").name == "Credential captured-two"


def test_dynamic_refresh_publishes_one_filtered_snapshot_after_auth_and_models_complete() -> None:
    async def resolve(_input_value):
        return {"auth": {"api_key": "ambient-key"}}

    runtime, _storage = _modern_api_key_runtime({"resolve": resolve})
    calls: list[bool] = []
    publish = runtime._refresh_available_snapshot

    def track_publish(*, apply_filters):
        calls.append(bool(apply_filters))
        publish(apply_filters=apply_filters)

    runtime._refresh_available_snapshot = track_publish  # type: ignore[method-assign]

    asyncio.run(runtime.refresh_dynamic_models(allow_network=False, force=True))

    assert calls == [True]
    assert [model.id for model in runtime.get_available_snapshot()] == ["model-1"]


def test_refresh_models_receives_minicode_context_and_publishes_validated_models() -> None:
    seen: dict[str, Any] = {}

    async def resolve(_input_value):
        return {
            "auth": {"api_key": "ambient-key"},
            "env": {"ACCOUNT": "account-1"},
        }

    async def refresh_models(context):
        seen["credential"] = dict(context.credential)
        seen["allow_network"] = context.allow_network
        seen["force"] = context.force
        await context.store.write({"models": [{"id": "cached"}]})
        return [
            {
                "id": "model-2",
                "name": "Refreshed Model",
                "api": "openai-completions",
                "base_url": "https://models.example.test/v2",
                "context_window": 32_000,
                "max_tokens": 4_096,
            }
        ]

    runtime, _storage = _modern_api_key_runtime(
        {"resolve": resolve},
        refresh_models=refresh_models,
    )

    asyncio.run(
        runtime.refresh_dynamic_models(
            allow_network=False,
            force=True,
        )
    )

    assert seen == {
        "credential": {
            "type": "api_key",
            "key": "ambient-key",
            "env": {"ACCOUNT": "account-1"},
        },
        "allow_network": False,
        "force": True,
    }
    assert runtime.get_model("modern-auth", "model-1") is None
    refreshed = runtime.get_model("modern-auth", "model-2")
    assert refreshed is not None
    assert refreshed.context_window == 32_000


def test_refresh_models_cache_restore_runs_after_auth_failure_and_preserves_original_error() -> None:
    seen: dict[str, Any] = {}

    async def resolve(_input_value):
        raise RuntimeError("primary auth failed")

    async def refresh_models(context):
        seen["allow_network"] = context.allow_network
        seen["credential_present"] = "credential" in context
        raise RuntimeError("cache restore also failed")

    runtime, _storage = _modern_api_key_runtime(
        {"resolve": resolve},
        refresh_models=refresh_models,
    )

    asyncio.run(runtime.refresh_dynamic_models(allow_network=True, force=True))

    assert seen == {
        "allow_network": False,
        "credential_present": False,
    }
    error = str(runtime.get_error())
    assert "primary auth failed" in error
    assert "cache restore also failed" not in error


def test_concurrent_dynamic_model_refresh_readers_coalesce() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls: list[bool] = []

        async def resolve(_input_value):
            return {"auth": {"api_key": "ambient-key"}}

        async def refresh_models(context):
            calls.append(bool(context.force))
            entered.set()
            await release.wait()
            return [
                {
                    "id": "model-1",
                    "api": "openai-completions",
                    "base_url": "https://models.example.test/v1",
                }
            ]

        runtime, _storage = _modern_api_key_runtime(
            {"resolve": resolve},
            refresh_models=refresh_models,
        )
        first = asyncio.create_task(runtime.refresh_dynamic_models())
        await entered.wait()
        second = asyncio.create_task(runtime.refresh_dynamic_models())
        await asyncio.sleep(0)

        assert calls == [False]
        release.set()
        await asyncio.gather(first, second)
        assert calls == [False]

    asyncio.run(run())


def test_forced_dynamic_model_refresh_queues_after_pending_reader() -> None:
    async def run() -> None:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        calls: list[bool] = []

        async def resolve(_input_value):
            return {"auth": {"api_key": "ambient-key"}}

        async def refresh_models(context):
            calls.append(bool(context.force))
            if len(calls) == 1:
                first_entered.set()
                await first_release.wait()
            else:
                second_entered.set()
                await second_release.wait()
            return [
                {
                    "id": f"model-{len(calls)}",
                    "api": "openai-completions",
                    "base_url": "https://models.example.test/v1",
                }
            ]

        runtime, _storage = _modern_api_key_runtime(
            {"resolve": resolve},
            refresh_models=refresh_models,
        )
        first = asyncio.create_task(runtime.refresh_dynamic_models())
        await first_entered.wait()
        forced = asyncio.create_task(runtime.refresh_dynamic_models(force=True))
        coalesced = asyncio.create_task(runtime.refresh_dynamic_models())
        await asyncio.sleep(0)

        assert calls == [False]
        assert second_entered.is_set() is False
        first_release.set()
        await second_entered.wait()
        assert calls == [False, True]
        second_release.set()
        await asyncio.gather(first, forced, coalesced)
        assert calls == [False, True]

    asyncio.run(run())


def test_refresh_models_does_not_publish_after_signal_aborts() -> None:
    class Signal:
        aborted = False

    signal = Signal()

    async def resolve(_input_value):
        return {"auth": {"api_key": "ambient-key"}}

    async def refresh_models(context):
        assert context.signal is signal
        signal.aborted = True
        return [
            {
                "id": "must-not-publish",
                "api": "openai-completions",
                "base_url": "https://models.example.test/v2",
            }
        ]

    runtime, _storage = _modern_api_key_runtime(
        {"resolve": resolve},
        refresh_models=refresh_models,
    )

    asyncio.run(runtime.refresh_dynamic_models(signal=signal))

    assert runtime.get_model("modern-auth", "model-1") is not None
    assert runtime.get_model("modern-auth", "must-not-publish") is None


def test_refresh_models_store_persists_catalog_across_runtime_generations(
    tmp_path,
) -> None:
    store = ProviderModelsStorage(tmp_path / "models-store.json")

    async def resolve(_input_value):
        return {"auth": {"api_key": "ambient-key"}}

    async def populate(context):
        await context.store.write(
            {
                "models": [
                    {
                        "id": "persisted-model",
                        "api": "openai-completions",
                        "base_url": "https://models.example.test/persisted",
                        "context_window": 16_000,
                        "max_tokens": 2_000,
                    }
                ],
                "checked_at": 1,
                "etag": '"catalog-v1"',
            }
        )

    first, _storage = _modern_api_key_runtime(
        {"resolve": resolve},
        refresh_models=populate,
    )
    first._models_store = store
    asyncio.run(first.refresh_dynamic_models(allow_network=False))
    assert first.get_model("modern-auth", "persisted-model") is not None

    async def restore_only(context):
        assert context.allow_network is False
        assert await context.store.read() is not None
        return None

    second, _storage = _modern_api_key_runtime(
        {"resolve": resolve},
        refresh_models=restore_only,
    )
    second._models_store = ProviderModelsStorage(tmp_path / "models-store.json")
    asyncio.run(second.refresh_dynamic_models(allow_network=False))

    restored = second.get_model("modern-auth", "persisted-model")
    assert restored is not None
    assert restored.context_window == 16_000
    assert restored.max_tokens == 2_000


def test_filter_models_receives_stored_credential_and_filters_available_snapshot() -> None:
    seen: dict[str, object] = {}

    def filter_models(models, credential):
        seen["model_ids"] = [model["id"] for model in models]
        seen["credential"] = dict(credential or {})
        return [
            {
                **models[1],
                "name": "Credential-filtered model",
            }
        ]

    runtime, storage, _catalog = _native_filter_runtime(
        filter_models,
        models=[
            {
                "id": "model-1",
                "api": "openai-completions",
                "base_url": "https://models.example.test/v1",
            },
            {
                "id": "model-2",
                "api": "openai-completions",
                "base_url": "https://models.example.test/v2",
            },
        ],
    )
    storage.values["native-filter"] = {
        "type": "api_key",
        "key": "stored-key",
        "env": {"TENANT": "tenant-one"},
    }
    asyncio.run(runtime.refresh_provider_auth("native-filter"))

    available = runtime.get_available("native-filter")

    assert seen == {
        "model_ids": ["model-1", "model-2"],
        "credential": {
            "type": "api_key",
            "key": "stored-key",
            "env": {"TENANT": "tenant-one"},
        },
    }
    assert [model.id for model in available] == ["model-2"]
    assert available[0].name == "Credential-filtered model"


def test_available_snapshot_reads_never_reinvoke_provider_filters() -> None:
    filter_calls = 0

    def filter_models(models, _credential):
        nonlocal filter_calls
        filter_calls += 1
        return models

    runtime, storage, _catalog = _native_filter_runtime(filter_models)
    storage.values["native-filter"] = {
        "type": "api_key",
        "key": "stored-key",
    }
    asyncio.run(runtime.refresh_provider_auth("native-filter"))
    assert filter_calls == 1
    assert [model.id for model in runtime.get_available_snapshot()] == [
        "model-1"
    ]
    filter_calls = 0

    runtime.get_available()
    assert filter_calls == 1
    assert [model.id for model in runtime.get_available_snapshot()] == [
        "model-1"
    ]
    assert [model.id for model in runtime.get_available_snapshot()] == [
        "model-1"
    ]
    assert filter_calls == 1


def test_register_and_unregister_publish_provisional_available_snapshot() -> None:
    runtime = ModelRuntime()
    runtime._base_providers = {}

    runtime.register_provider(
        "snapshot-provider",
        {
            "api_key": "configured-key",
            "models": [
                {
                    "id": "snapshot-model",
                    "api": "openai-completions",
                    "base_url": "https://snapshot.example.test/v1",
                }
            ],
        },
    )
    assert [model.id for model in runtime.get_available_snapshot()] == [
        "snapshot-model"
    ]

    runtime.unregister_provider("snapshot-provider")

    assert runtime.get_available_snapshot() == ()


def test_filter_failure_preserves_last_good_available_snapshot() -> None:
    fail = False

    def filter_models(models, _credential):
        if fail:
            raise RuntimeError("filter exploded")
        return models

    runtime, storage, _catalog = _native_filter_runtime(filter_models)
    storage.values["native-filter"] = {
        "type": "api_key",
        "key": "stored-key",
    }
    asyncio.run(runtime.refresh_provider_auth("native-filter"))
    expected = runtime.get_available_snapshot()
    fail = True

    with pytest.raises(RuntimeError, match="filter exploded"):
        asyncio.run(runtime.refresh_provider_auth("native-filter"))

    assert runtime.get_available_snapshot() == expected


@pytest.mark.skip(reason="MiniCode exposes dynamic model filter failure instead of restoring a last-good snapshot")
def test_refresh_models_filter_failure_keeps_last_good_published_snapshot() -> None:
    fail = False
    catalog: list[dict]

    async def refresh_models(_context):
        catalog[:] = [
            {
                "id": "model-2",
                "api": "openai-completions",
                "base_url": "https://models.example.test/v2",
            }
        ]

    def filter_models(models, _credential):
        if fail:
            raise RuntimeError("filter exploded")
        return models

    runtime, storage, catalog = _native_filter_runtime(
        filter_models,
        refresh_models=refresh_models,
    )
    storage.values["native-filter"] = {
        "type": "api_key",
        "key": "stored-key",
    }
    asyncio.run(runtime.refresh_provider_auth("native-filter"))
    expected = runtime.get_available_snapshot()
    fail = True

    asyncio.run(runtime.refresh_dynamic_models())

    assert runtime.get_model("native-filter", "model-2") is not None
    assert runtime.get_available_snapshot() == expected
    assert "filter exploded" in str(runtime.get_error())


def test_provider_auth_storage_serializes_modify_and_logout_across_instances(
    tmp_path,
) -> None:
    class MemoryVault:
        def __init__(self, path, values):
            self._path = path
            self.values = values

        def get(self, name):
            return self.values.get(name)

        def set(self, name, value, **_kwargs):
            self.values[name] = value

        def delete(self, name):
            return self.values.pop(name, None) is not None

    async def run() -> None:
        values: dict[str, str] = {}
        path = tmp_path / "vault.json"
        first = ProviderAuthStorage(MemoryVault(path, values))
        second = ProviderAuthStorage(MemoryVault(path, values))
        first.set(
            "provider",
            {
                "type": "oauth",
                "access": "old",
                "refresh": "old-refresh",
                "expires": 1,
            },
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        delete_finished = asyncio.Event()

        async def rotate(current):
            assert current is not None and current["access"] == "old"
            entered.set()
            await release.wait()
            return {
                "type": "oauth",
                "access": "rotated",
                "refresh": "rotated-refresh",
                "expires": 4_102_444_800_000,
            }

        rotate_task = asyncio.create_task(first.modify("provider", rotate))
        await entered.wait()

        async def delete_after_refresh() -> bool:
            removed = await second.delete_serialized("provider")
            delete_finished.set()
            return removed

        delete_task = asyncio.create_task(delete_after_refresh())
        await asyncio.sleep(0)
        assert delete_finished.is_set() is False
        release.set()

        rotated, removed = await asyncio.gather(rotate_task, delete_task)
        assert rotated is not None and rotated["access"] == "rotated"
        assert removed is True
        assert first.get("provider") is None

    asyncio.run(run())


def test_provider_auth_storage_serializes_modify_and_logout_across_processes(
    tmp_path,
) -> None:
    context = multiprocessing.get_context("spawn")
    vault_path = tmp_path / "process-vault.json"
    storage = ProviderAuthStorage(_ProcessJsonVault(vault_path))
    storage.set(
        "provider",
        {
            "type": "oauth",
            "access": "old",
            "refresh": "old-refresh",
            "expires": 1,
        },
    )

    rotate_entered = context.Event()
    rotate_release = context.Event()
    delete_entered = context.Event()
    delete_finished = context.Event()
    rotate_results = context.Queue()
    delete_results = context.Queue()
    rotate_process = context.Process(
        target=_provider_auth_rotate_process,
        args=(
            str(vault_path),
            rotate_entered,
            rotate_release,
            rotate_results,
        ),
    )
    delete_process = context.Process(
        target=_provider_auth_delete_process,
        args=(
            str(vault_path),
            delete_entered,
            delete_finished,
            delete_results,
        ),
    )
    try:
        rotate_process.start()
        assert rotate_entered.wait(10)
        delete_process.start()
        assert delete_entered.wait(10)
        time.sleep(0.25)
        assert delete_finished.is_set() is False

        rotate_release.set()
        rotate_process.join(15)
        delete_process.join(15)

        assert rotate_process.exitcode == 0
        assert delete_process.exitcode == 0
        assert rotate_results.get(timeout=2)["access"] == "rotated"
        assert delete_results.get(timeout=2) is True
        assert storage.get("provider") is None
    finally:
        rotate_release.set()
        for process in (rotate_process, delete_process):
            if process.is_alive():
                process.terminate()
            process.join(5)
