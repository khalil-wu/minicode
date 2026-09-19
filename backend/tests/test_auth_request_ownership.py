from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from backend.config import AppConfig, LLMSettings
from backend.llm.base import LLMMessage
from backend.llm.model_runtime import ProviderRegistrationError
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tests.test_model_runtime import _modern_api_key_runtime, _oauth_runtime
from backend.ws.agent_runner import _get_or_create_session_llm


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_kind", ["api_key", "oauth"])
async def test_changed_request_auth_replaces_adapter_and_preserves_inflight_lease(monkeypatch, auth_kind):
    version = 0
    def material():
        return {"api_key": f"fixture-{version}", "base_url": f"https://route-{version}.invalid/v1", "headers": {"X-Account": str(version)}}
    async def resolve(_input):
        return {"auth": material()}
    async def unused(*args):
        raise AssertionError("Unexpired OAuth credentials must not rotate")
    async def to_auth(_credential):
        return material()
    if auth_kind == "api_key":
        runtime, _ = _modern_api_key_runtime({"resolve": resolve})
        provider = "modern-auth"
    else:
        runtime, storage = _oauth_runtime({"login": unused, "refresh": unused, "to_auth": to_auth})
        provider = "modern-oauth"
        storage.set(provider, {"type": "oauth", "access": "fixture-access", "refresh": "fixture-refresh", "expires": time.time() * 1000 + 60000})

    entered, release = asyncio.Event(), asyncio.Event()
    requests, adapters = [], []
    async def endpoint(request):
        requests.append((str(request.url), request.headers["Authorization"], request.headers["X-Account"]))
        if len(requests) == 1:
            entered.set()
            await release.wait()
        chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    host = SimpleNamespace()
    config = AppConfig(llm=LLMSettings(api_key="", provider=provider, model="model-1"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        def build(settings):
            adapter = OpenAIAdapter(settings, http_client=client)
            adapters.append(adapter)
            return adapter
        monkeypatch.setattr("backend.services.llm_adapter_factory.OpenAIAdapter", build)
        await runtime.refresh_provider_auth(provider)
        revision = runtime.revision
        async def request_old():
            adapter = _get_or_create_session_llm(host, config=config, provider=provider, model="model-1", model_runtime=runtime)
            return await adapter.simple_chat([LLMMessage(role="user", content="old request")])
        running = asyncio.create_task(request_old())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            version = 1
            await runtime.refresh_provider_auth(provider)
            assert runtime.revision == revision
            new = _get_or_create_session_llm(host, config=config, provider=provider, model="model-1", model_runtime=runtime)
            assert new is not adapters[0]
            assert not adapters[0]._closed
            assert await new.simple_chat([LLMMessage(role="user", content="new request")]) == "ok"
            await runtime.refresh_provider_auth(provider)
            assert _get_or_create_session_llm(host, config=config, provider=provider, model="model-1", model_runtime=runtime) is new
            assert len(adapters) == 2
            assert requests == [
                ("https://route-0.invalid/v1/chat/completions", "Bearer fixture-0", "0"),
                ("https://route-1.invalid/v1/chat/completions", "Bearer fixture-1", "1"),
            ]
        finally:
            release.set()
            await running
            await asyncio.gather(*host._llm_close_tasks)
            for adapter in adapters:
                await adapter.aclose()
        assert adapters[0]._closed
        assert not client.is_closed


@pytest.mark.asyncio
async def test_queued_old_auth_callback_cannot_publish_into_replacement_generation():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def old(_input):
        calls.append("old")
        entered.set()
        await release.wait()
        return {"auth": {"api_key": "fixture-old"}}
    async def new(_input):
        calls.append("new")
        return {"auth": {"api_key": "fixture-new"}}
    runtime, _ = _modern_api_key_runtime({"resolve": old})
    first = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
    await entered.wait()
    queued = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
    await asyncio.sleep(0)
    assert not queued.done()
    config = {**runtime._extension_providers["modern-auth"], "auth": {"api_key": {"resolve": new}}}
    runtime.unregister_provider("modern-auth")
    runtime.register_provider("modern-auth", config)
    await runtime.refresh_provider_auth("modern-auth")
    release.set()
    results = await asyncio.gather(first, queued, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) and "changed during" in str(result) for result in results)
    assert calls == ["old", "new"]
    assert runtime.resolve_provider_auth("modern-auth")["auth"]["api_key"] == "fixture-new"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["waiting_lock", "checking", "resolving"])
async def test_cancelled_api_auth_keeps_last_publication(boundary):
    entered, release = asyncio.Event(), asyncio.Event()
    pending = False
    calls = []
    async def check(_input):
        calls.append("check")
        if pending and boundary == "checking":
            entered.set()
            await release.wait()
            return None
        return {"type": "api_key"}
    async def resolve(_input):
        calls.append("resolve")
        if pending and boundary == "resolving":
            entered.set()
            await release.wait()
        return {"auth": {"api_key": "fixture-next" if pending else "fixture-original"}}
    runtime, _ = _modern_api_key_runtime({"check": check, "resolve": resolve})
    await runtime.refresh_provider_auth("modern-auth")
    original = runtime.resolve_provider_auth("modern-auth")
    calls.clear()
    pending = True
    signal = SimpleNamespace(aborted=False)
    lock = runtime._provider_lock("modern-auth", oauth=False)
    if boundary == "waiting_lock":
        await lock.acquire()
    task = asyncio.create_task(runtime.refresh_provider_auth("modern-auth", signal=signal))
    if boundary == "waiting_lock":
        await asyncio.sleep(0)
        assert not task.done()
    else:
        await entered.wait()
    signal.aborted = True
    if boundary == "waiting_lock":
        lock.release()
    release.set()
    await task
    assert runtime.resolve_provider_auth("modern-auth") == original
    assert calls == {"waiting_lock": [], "checking": ["check"], "resolving": ["check", "resolve"]}[boundary]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_cancel_during_oauth_derivation_does_not_publish_but_preserves_rotated_credentials(expired):
    pending = False
    entered, release = asyncio.Event(), asyncio.Event()
    async def unused(*args):
        raise AssertionError("No login")
    async def refresh(credential):
        return {**credential, "access": "rotated-access", "refresh": "rotated-refresh", "expires": time.time() * 1000 + 60000}
    async def to_auth(credential):
        if pending:
            entered.set()
            await release.wait()
        return {"api_key": credential["access"], "headers": {"X-Stage": "pending" if pending else "original"}}
    runtime, storage = _oauth_runtime({"login": unused, "refresh": refresh, "to_auth": to_auth})
    storage.set("modern-oauth", {"type": "oauth", "access": "original-access", "refresh": "original-refresh", "expires": time.time() * 1000 + 60000})
    await runtime.refresh_oauth_credentials("modern-oauth")
    previous = deepcopy(runtime._resolved_oauth_auth)
    if expired:
        storage.values["modern-oauth"]["expires"] = 1
    pending = True
    signal = SimpleNamespace(aborted=False)
    task = asyncio.create_task(runtime.refresh_oauth_credentials("modern-oauth", signal=signal))
    await entered.wait()
    signal.aborted = True
    release.set()
    assert await task is expired
    assert runtime._resolved_oauth_auth == previous
    assert storage.values["modern-oauth"]["access"] == ("rotated-access" if expired else "original-access")


@pytest.mark.asyncio
async def test_late_oauth_derivation_cannot_erase_new_credential_cache():
    entered, release = asyncio.Event(), asyncio.Event()
    async def unused(*args):
        raise AssertionError("Unexpired credentials")
    async def to_auth(credential):
        if credential["access"] == "old":
            entered.set()
            await release.wait()
        return {"api_key": credential["access"]}
    runtime, storage = _oauth_runtime({"login": unused, "refresh": unused, "to_auth": to_auth})
    credential = {"type": "oauth", "access": "old", "refresh": "fixture-refresh", "expires": time.time() * 1000 + 60000}
    storage.set("modern-oauth", credential)
    old = asyncio.create_task(runtime.refresh_oauth_credentials("modern-oauth"))
    await entered.wait()
    storage.set("modern-oauth", {**credential, "access": "new"})
    await runtime.refresh_oauth_credentials("modern-oauth")
    release.set()
    assert await old is False
    assert runtime.resolve_provider_auth("modern-oauth")["auth"]["api_key"] == "new"


@pytest.mark.asyncio
async def test_settings_refresh_fences_pending_auth_and_invalidates_only_changed_provider():
    entered, release = asyncio.Event(), asyncio.Event()
    pending = False
    async def resolve(_input):
        if pending:
            entered.set()
            await release.wait()
        return {"auth": {"api_key": "fixture-key"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    runtime.register_provider("other", runtime._extension_providers["modern-auth"])
    await runtime.refresh_provider_auth()
    original_other = runtime.resolve_provider_auth("other")
    other_generation = runtime._provider_generation("other")
    pending = True
    task = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
    await entered.wait()
    runtime._load_base_providers = lambda: {"modern-auth": {"headers": {"X-Route": "changed"}}}
    runtime.refresh()
    release.set()
    with pytest.raises(RuntimeError, match="changed during"):
        await task
    assert "modern-auth" not in runtime._resolved_api_key_auth
    assert runtime.resolve_provider_auth("other") == original_other
    assert runtime._provider_generation("other") == other_generation
    await runtime.refresh_provider_auth("modern-auth")
    assert runtime.resolve_provider_auth("modern-auth")["auth"]["headers"] == {"X-Route": "changed"}


@pytest.mark.asyncio
async def test_stored_api_credential_change_rejects_pending_resolution():
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(input_value):
        key = input_value.credential.key
        entered.set()
        await release.wait()
        return {"auth": {"api_key": key}}
    runtime, storage = _modern_api_key_runtime({"resolve": resolve})
    storage.set("modern-auth", {"type": "api_key", "key": "old"})
    task = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
    await entered.wait()
    storage.set("modern-auth", {"type": "api_key", "key": "new"})
    release.set()
    with pytest.raises(ProviderRegistrationError, match="credential changed"):
        await task
    assert "modern-auth" not in runtime._resolved_api_key_auth
    await runtime.refresh_provider_auth("modern-auth")
    assert runtime.resolve_provider_auth("modern-auth")["auth"]["api_key"] == "new"


def test_sync_auth_callback_cannot_reregister_then_publish_old_result():
    runtime = None
    def resolve(_input):
        runtime.register_provider("modern-auth", {"auth": {"api_key": {"resolve": lambda _input: {"auth": {"api_key": "new"}}}}})
        return {"auth": {"api_key": "old"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    with pytest.raises(RuntimeError, match="changed during"):
        runtime.resolve_provider_auth("modern-auth")
    assert "modern-auth" not in runtime._resolved_api_key_auth
    assert runtime.resolve_provider_auth("modern-auth")["auth"]["api_key"] == "new"


@pytest.mark.asyncio
async def test_resolved_auth_is_detached_and_cache_identity_tracks_model_header_environment(monkeypatch):
    async def resolve(_input):
        return {"auth": {"api_key": "fixture", "headers": {"X-Account": "original"}}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    definition = deepcopy(runtime._extension_providers["modern-auth"])
    definition["models"][0]["headers"] = {"X-Model-Route": "$MODEL_AUTH_ROUTE"}
    runtime.register_provider("modern-auth", definition)
    monkeypatch.setenv("MODEL_AUTH_ROUTE", "original")
    await runtime.refresh_provider_auth("modern-auth")
    key = runtime.cache_identity("modern-auth", "model-1")
    auth = runtime.resolve_provider_auth("modern-auth")
    auth["auth"]["headers"]["X-Account"] = "caller mutation"
    assert runtime.cache_identity("modern-auth", "model-1") == key
    monkeypatch.setenv("MODEL_AUTH_ROUTE", "updated")
    assert runtime.cache_identity("modern-auth", "model-1") != key
    assert runtime.resolve_provider_auth("modern-auth")["auth"]["headers"]["X-Account"] == "original"


@pytest.mark.asyncio
async def test_adapter_model_definition_is_reused_until_catalog_changes(monkeypatch):
    async def resolve(_input):
        return {"auth": {"api_key": "fixture"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    await runtime.refresh_provider_auth("modern-auth")
    original = runtime.get_model
    compose = runtime._composed_models
    calls = []
    compositions = []
    def compose_models(*args, **kwargs):
        compositions.append(args)
        return compose(*args, **kwargs)
    def read(provider, model):
        calls.append((provider, model))
        return original(provider, model)
    monkeypatch.setattr(runtime, "get_model", read)
    monkeypatch.setattr(runtime, "_composed_models", compose_models)
    first = runtime.cache_identity("modern-auth", "model-1")
    for _ in range(5):
        assert runtime.cache_identity("modern-auth", "model-1") == first
        assert runtime.resolve_adapter_spec("modern-auth", "model-1").context_window == 128000
    assert len(calls) == 1
    assert len(compositions) == 1
    definition = deepcopy(runtime._extension_providers["modern-auth"])
    definition["models"][0]["context_window"] = 64000
    runtime.register_provider("modern-auth", definition)
    await runtime.refresh_provider_auth("modern-auth")
    assert runtime.cache_identity("modern-auth", "model-1") != first
    assert runtime.resolve_adapter_spec("modern-auth", "model-1").context_window == 64000
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["auth", "models", "store", "error"])
async def test_provider_replacement_fences_pending_dynamic_catalog(boundary):
    entered, release = asyncio.Event(), asyncio.Event()
    model_calls = []
    stale = [{"id": "stale-model", "api": "openai-completions", "base_url": "https://old.invalid/v1"}]
    async def auth(_input):
        if boundary == "auth":
            entered.set()
            await release.wait()
        return {"auth": {"api_key": "fixture"}}
    async def models(_context):
        model_calls.append("old")
        if boundary in {"models", "error"}:
            entered.set()
            await release.wait()
        if boundary == "error":
            raise RuntimeError("old provider failure")
        return None if boundary == "store" else stale
    async def read_store(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"models": stale}
    runtime, _ = _modern_api_key_runtime({"resolve": auth}, refresh_models=models)
    if boundary == "store":
        runtime._models_store = SimpleNamespace(read=read_store)
    task = asyncio.create_task(runtime.refresh_dynamic_models())
    await entered.wait()
    runtime.register_provider("modern-auth", {"models": [
        {"id": "new-model", "api": "openai-completions", "base_url": "https://new.invalid/v1"}
    ]})
    release.set()
    await task
    assert runtime.get_model("modern-auth", "new-model") is not None
    assert runtime.get_model("modern-auth", "stale-model") is None
    assert "old provider failure" not in str(runtime.get_error())
    assert model_calls == ([] if boundary == "auth" else ["old"])


@pytest.mark.asyncio
async def test_new_catalog_readers_queue_after_a_superseded_refresh_and_coalesce():
    old_entered, old_release = asyncio.Event(), asyncio.Event()
    new_entered, new_release = asyncio.Event(), asyncio.Event()
    calls = []
    async def auth(_input):
        return {"auth": {"api_key": "fixture"}}
    async def old_models(_context):
        calls.append("old")
        old_entered.set()
        await old_release.wait()
        return [{"id": "stale", "api": "openai-completions", "base_url": "https://fixture.invalid/v1"}]
    async def new_models(_context):
        calls.append("new")
        new_entered.set()
        await new_release.wait()
        return [{"id": "fresh", "api": "openai-completions", "base_url": "https://fixture.invalid/v1"}]
    runtime, _ = _modern_api_key_runtime({"resolve": auth}, refresh_models=old_models)
    old = asyncio.create_task(runtime.refresh_dynamic_models())
    await old_entered.wait()
    runtime.register_provider("modern-auth", {"refresh_models": new_models})
    readers = [asyncio.create_task(runtime.refresh_dynamic_models()) for _ in range(3)]
    await asyncio.sleep(0)
    assert not new_entered.is_set()
    old_release.set()
    await asyncio.wait_for(new_entered.wait(), 3)
    assert calls == ["old", "new"]
    new_release.set()
    await asyncio.gather(old, *readers)
    assert runtime.get_model("modern-auth", "fresh") is not None
    assert runtime.get_model("modern-auth", "stale") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("start_with_oauth", [False, True])
async def test_auth_mode_switch_preserves_the_new_modes_cache(start_with_oauth):
    entered, release = asyncio.Event(), asyncio.Event()
    api_calls = []
    async def api_auth(_input):
        api_calls.append("api")
        if not start_with_oauth:
            entered.set()
            await release.wait()
        return {"auth": {"api_key": "new-api"}}
    async def oauth_auth(_credential):
        if start_with_oauth:
            entered.set()
            await release.wait()
        return {"api_key": "new-oauth"}
    async def unused(*args):
        raise AssertionError("No interactive login or token rotation")
    runtime, storage = _modern_api_key_runtime({"resolve": api_auth})
    runtime.register_provider("modern-auth", {"auth": {
        "api_key": {"resolve": api_auth},
        "oauth": {"login": unused, "refresh": unused, "to_auth": oauth_auth},
    }})
    oauth_credential = {"type": "oauth", "access": "oauth-access", "refresh": "oauth-refresh", "expires": time.time() * 1000 + 60000}
    if start_with_oauth:
        storage.set("modern-auth", oauth_credential)
    old = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
    await entered.wait()
    queued = None
    if not start_with_oauth:
        queued = asyncio.create_task(runtime.refresh_provider_auth("modern-auth"))
        await asyncio.sleep(0)
    storage.set("modern-auth", {"type": "api_key", "key": "api-access"} if start_with_oauth else oauth_credential)
    await runtime.refresh_provider_auth("modern-auth")
    release.set()
    if start_with_oauth:
        await old
        assert runtime.resolve_provider_auth("modern-auth")["auth"]["api_key"] == "new-api"
    else:
        results = await asyncio.gather(old, queued, return_exceptions=True)
        assert all(isinstance(result, ProviderRegistrationError) for result in results)
        assert runtime.resolve_provider_auth("modern-auth")["auth"]["api_key"] == "new-oauth"
    assert api_calls == ["api"]
