from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

import backend.config as config
import backend.config_helpers as helpers
import backend.config_providers as providers
from backend.api import _state, routes_llm
from backend.api.models import LLMProviderHistoryDeleteRequest
from backend.llm.base import UsageInfo
from backend.llm.cost_tracker import CostTracker
from backend.llm.model_runtime import ModelRuntime
from backend.llm.provider_contracts import ProviderRegistrationError
from backend.services import llm_settings_service
from backend.services.session_inspect_service import build_usage_inspect_result
from backend.vault.store import EnvVault, VaultReadError


@pytest.fixture
def credential_profile(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CUSTOM_API_KEY", "MINICODE_OPENAI_IMAGE_API_KEY", "MINICODE_ANTHROPIC_IMAGE_API_KEY", "MINICODE_CUSTOM_IMAGE_API_KEY")) or name in {"OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "CUSTOM_BASE_URL"}:
            monkeypatch.delenv(name)
    path = tmp_path / "settings.json"
    vault = EnvVault(tmp_path / "vault.json")
    endpoint = "https://publication.invalid/v1"
    image_endpoint = "https://image-publication.invalid/v1"
    text_scope, image_scope = {}, {}
    for module in (config, helpers, providers):
        monkeypatch.setattr(module, "SETTINGS_FILE", path)
        monkeypatch.setattr(module, "_RUNTIME_API_KEY_SCOPES", text_scope)
        monkeypatch.setattr(module, "_RUNTIME_IMAGE_API_KEY_SCOPES", image_scope)
    monkeypatch.setattr("backend.vault.EnvVault", lambda: vault)
    values = {}
    control = SimpleNamespace(fail_delete="")
    monkeypatch.setattr("keyring.get_password", lambda service, name: values.get(name))
    monkeypatch.setattr("keyring.set_password", lambda service, name, value: values.__setitem__(name, value))

    def delete(service, name):
        if name == control.fail_delete:
            raise KeyringError("injected delete failure")
        if name not in values:
            raise PasswordDeleteError("absent")
        del values[name]

    monkeypatch.setattr("keyring.delete_password", delete)
    history = {"provider": "custom", "base_url": endpoint, "wire_api": "responses", "model": "fixture-model", "image_mode": "custom", "image_base_url": image_endpoint, "image_model": "fixture-image"}
    path.write_text(json.dumps({"llm": {"provider": "custom", "custom": history, "provider_history": [history]}}), encoding="utf-8")
    changes, text_owner = providers._credential_changes("custom", "text-placeholder", endpoint)
    image_changes, image_owner = providers._credential_changes("custom", "image-placeholder", image_endpoint, image=True)
    changes.update(image_changes)
    providers._commit_credential_changes(changes, [text_owner, image_owner])
    bootstrap = SimpleNamespace(config=object())
    monkeypatch.setattr(_state, "bootstrap", bootstrap)

    async def hook(**kwargs):
        return None

    monkeypatch.setattr(routes_llm, "run_config_change_hook", hook)
    request = LLMProviderHistoryDeleteRequest(provider="custom", base_url=endpoint, wire_api="responses", model="fixture-model", clear_api_key=True, confirm_sensitive_change=True)
    return SimpleNamespace(path=path, vault=vault, values=values, control=control, names=list(changes), text_scope=text_scope, image_scope=image_scope, request=request, bootstrap=bootstrap)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["text_delete", "image_delete", "settings_write", "reload", "receipt"])
async def test_delete_api_restores_complete_profile_on_failure(monkeypatch, credential_profile, failure):
    profile = credential_profile
    before = (profile.path.read_bytes(), profile.vault._path.read_bytes(), dict(profile.values), {name: os.environ[name] for name in profile.names}, dict(profile.text_scope), dict(profile.image_scope), profile.bootstrap.config)

    def unavailable(*args, **kwargs):
        raise OSError("injected publication failure")

    if failure.endswith("delete"):
        prefix = "MINICODE_CUSTOM_IMAGE_API_KEY" if failure == "image_delete" else "CUSTOM_API_KEY_"
        profile.control.fail_delete = [name for name in profile.names if name.startswith(prefix)][1]
    elif failure == "settings_write":
        monkeypatch.setattr(providers, "_write_settings_json", unavailable)
    elif failure == "reload":
        monkeypatch.setattr(llm_settings_service, "load_config", unavailable)
    else:
        monkeypatch.setattr(providers, "get_llm_settings_payload", unavailable)
    with pytest.raises((RuntimeError, OSError)):
        await routes_llm.delete_llm_provider_history_api(profile.request)
    assert profile.path.read_bytes() == before[0]
    assert profile.vault._path.read_bytes() == before[1]
    assert profile.values == before[2]
    assert {name: os.environ[name] for name in profile.names} == before[3]
    assert profile.text_scope == before[4]
    assert profile.image_scope == before[5]
    assert profile.bootstrap.config is before[6]


@pytest.mark.asyncio
async def test_delete_api_publishes_all_family_members_and_reload(monkeypatch, credential_profile):
    profile = credential_profile
    new_config = object()
    monkeypatch.setattr(llm_settings_service, "load_config", lambda: new_config)
    receipt = await routes_llm.delete_llm_provider_history_api(profile.request)
    assert receipt["provider_history"] == []
    assert profile.values == {}
    assert json.loads(profile.vault._path.read_text(encoding="utf-8"))["entries"] == {}
    assert all(name not in os.environ for name in profile.names)
    assert profile.text_scope == profile.image_scope == {}
    assert profile.bootstrap.config is new_config


@pytest.mark.asyncio
async def test_delete_old_profile_preserves_live_alias_and_shared_image(monkeypatch, credential_profile):
    profile = credential_profile
    settings = json.loads(profile.path.read_text(encoding="utf-8"))
    retained = {**settings["llm"]["custom"], "base_url": "https://retained.invalid/v1"}
    settings["llm"]["custom"] = retained
    settings["llm"]["provider_history"].append(retained)
    profile.path.write_text(json.dumps(settings), encoding="utf-8")
    providers._set_runtime_api_key("custom", "text-placeholder", retained["base_url"])
    image_names = helpers._image_scoped_vault_names("custom", retained["image_base_url"])
    image_before = {name: profile.values[name] for name in image_names}
    monkeypatch.setattr(llm_settings_service, "load_config", lambda: object())
    receipt = await routes_llm.delete_llm_provider_history_api(profile.request)
    assert len(receipt["provider_history"]) == 1
    assert profile.values["CUSTOM_API_KEY"] == os.environ["CUSTOM_API_KEY"] == "text-placeholder"
    assert {name: profile.values[name] for name in image_names} == image_before
    assert all(name in os.environ for name in image_names)
    assert profile.text_scope["custom"] == helpers._provider_key_scope(retained["base_url"])


@pytest.mark.parametrize("cost", ["bad", {"input": 1}, {"input": -1, "output": 0, "cacheRead": 0, "cacheWrite": 0}, {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0, "tiers": None}, {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0, "tiers": [{"inputTokensAbove": -1, "input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}]}])
def test_bad_registered_prices_do_not_replace_published_model(cost):
    runtime = ModelRuntime(provider_configs={})
    provider = {"api": "openai-responses", "base_url": "https://price.invalid/v1", "api_key": "placeholder", "models": [{"id": "priced-model", "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}}]}
    runtime.register_provider("fixture", provider)
    before = runtime.get_model("fixture", "priced-model")
    with pytest.raises(ProviderRegistrationError):
        runtime.register_provider("fixture", {**provider, "models": [{"id": "priced-model", "cost": cost}]})
    assert runtime.get_model("fixture", "priced-model") == before


def test_invalid_provider_cost_is_unknown_and_session_projection_keeps_subtotal():
    tracker = CostTracker()
    usage = UsageInfo(input_tokens=10, output_tokens=2, cost_usd=float("nan"))
    assert tracker.record_usage_info(usage, model_id="unpriced", session_id="fixture") == "unknown"
    assert usage.cost_usd is None
    summary = tracker.get_summary("fixture")
    _, _, outcome = build_usage_inspect_result(session_id="fixture", conversation_id="fixture-chat", tracker_summary=summary, budget_snapshot={"used": 12, "total": 1000})
    assert "session cost unknown" in outcome.message
    assert "$0.0000" not in outcome.message
    tracker.record_usage_info(UsageInfo(input_tokens=10, cost_usd=.03), model_id="priced", session_id="fixture")
    summary = tracker.get_summary("fixture")
    _, _, outcome = build_usage_inspect_result(session_id="fixture", conversation_id="fixture-chat", tracker_summary=summary, budget_snapshot={"used": 12, "total": 1000})
    assert "subtotal $0.0300" in outcome.message
    assert "1 unpriced requests" in outcome.message
    assert outcome.data["cost"] is summary


@pytest.mark.skipif(os.name != "nt", reason="Windows keyring backend contract")
def test_windows_native_credential_error_is_reported_and_rollback_errors_survive(monkeypatch, tmp_path):
    import pywintypes

    values = {"fixture": "placeholder"}
    monkeypatch.setattr("keyring.get_password", lambda service, name: values.get(name))
    monkeypatch.setattr("keyring.set_password", lambda service, name, value: values.__setitem__(name, value))
    vault = EnvVault(tmp_path / "vault.json")
    vault.set("fixture", "placeholder")
    before = vault._path.read_bytes()

    def read_unavailable(*args, **kwargs):
        raise pywintypes.error(5, "CredRead", "injected native access denial")

    monkeypatch.setattr("keyring.get_password", read_unavailable)
    with pytest.raises(VaultReadError):
        vault.get("fixture")
    monkeypatch.setattr("keyring.get_password", lambda service, name: values.get(name))
    monkeypatch.setattr("keyring.delete_password", lambda service, name: values.pop(name, None))

    def store_unavailable(*args, **kwargs):
        raise pywintypes.error(5, "CredWrite", "injected native rollback denial")

    def publication_unavailable():
        raise OSError("injected settings failure")

    monkeypatch.setattr("keyring.set_password", store_unavailable)
    with pytest.raises(ExceptionGroup) as group:
        vault.set_many({"fixture": (None, "", "")}, publish=publication_unavailable)
    assert isinstance(group.value.exceptions[0], OSError)
    assert isinstance(group.value.exceptions[1], pywintypes.error)
    assert vault._path.read_bytes() == before
