from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import psutil
import pytest

from backend.config import AppConfig, load_llm_settings
from backend.config_helpers import get_image_generation_settings, get_llm_settings_payload
from backend.llm.base import LLMMessage, SideQueryOptions, StreamEventType
from backend.llm.command_auth import CommandBearerAuth, command_auth_config
from backend.llm.gcp import GCP_MODELS, gcp_command_config, memory_consolidation_adapter, vertex_base_url
from backend.llm.model_request_policy import ModelRequestPolicy
from backend.llm.model_runtime import ModelRuntime
from backend.services.llm_adapter_factory import build_provider_adapter, create_session_llm
from backend.tests.test_bedrock_provider_chain import endpoint


@pytest.fixture(autouse=True)
def gcp_environment(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)


def token_command(tmp_path, *, interval=3000000):
    script = tmp_path / "token.py"
    script.write_text("import pathlib,time\n"
        "p=pathlib.Path('calls.json')\n"
        "n=int(p.read_text())+1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "time.sleep(.05)\n"
        "print(' token-'+str(n)+' ')\n", encoding="utf-8")
    return {"command": sys.executable, "args": ["token.py"], "refresh_interval_ms": interval}


def settings_for(config, model="gpt-6-astra"):
    return {"llm": {"provider": "gcp", "gcp": {"model": model}}, "model_providers": {"gcp": config}}


@pytest.mark.parametrize("config,project,location,expected", [
    ({}, "example-project", None, ("example-project", "global")),
    ({}, "  example-project ", " us-central1 ", ("example-project", "us-central1")),
    ({"project_id": "configured"}, "ambient", "us-east4", ("configured", "us-east4")),
    ({"location": "configured"}, "ambient", "us-east4", ("ambient", "configured")),
    ({"project_id": "explicit", "location": "us-west1"}, "ambient", "us-east4", ("explicit", "us-west1")),
    ({}, "project", " ", ("project", "global")),
])
def test_vertex_endpoint_precedence(config, project, location, expected, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project)
    if location is not None:
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", location)
    assert vertex_base_url(config) == f"https://aiplatform.googleapis.com/v1/projects/{expected[0]}/locations/{expected[1]}/publishers/openai/v1"


@pytest.mark.parametrize("config,match", [
    ({}, "Set model_providers.gcp.project_id"),
    ({"project_id": " "}, "project_id must be non-empty"),
    ({"project_id": "a/b"}, "project_id must be non-empty"),
    ({"project_id": "中文"}, "project_id must be non-empty"),
    ({"project_id": "valid", "location": ""}, "location must be non-empty"),
])
def test_vertex_rejects_invalid_explicit_path_fields(config, match):
    with pytest.raises(ValueError, match=match):
        vertex_base_url(config)
    assert vertex_base_url({**config, "base_url": "https://fixture.invalid/custom/v1"}) == "https://fixture.invalid/custom/v1"


@pytest.mark.asyncio
async def test_catalog_default_auth_and_configuration_do_not_run_credentials(tmp_path):
    config = {"project_id": "example", "auth": token_command(tmp_path)}
    settings = settings_for(config)
    runtime = ModelRuntime(settings_snapshot=settings, provider_configs={}, cwd=tmp_path)
    assert [model.id for model in runtime.get_models("gcp")] == list(GCP_MODELS)
    assert runtime.get_provider("gcp").configured
    assert runtime.get_provider_auth_status("gcp")["source"] == "command"
    await runtime.refresh_dynamic_models(allow_network=False)
    payload = get_llm_settings_payload(settings)
    assert payload["active_model"] == "gpt-6-astra"
    assert payload["gcp"]["available_models"] == list(GCP_MODELS)
    assert not (tmp_path / "calls.json").exists()
    assert "token.py" not in json.dumps(payload)
    defaults = gcp_command_config({}, cwd=tmp_path)
    assert defaults == {"command": "gcloud.cmd" if os.name == "nt" else "gcloud",
        "args": ["auth", "application-default", "print-access-token"], "timeout_ms": 30000,
        "refresh_interval_ms": 3000000, "cwd": str(tmp_path)}
    model = runtime.get_model("gcp", "gpt-6-astra")
    assert model.tool_mode == "direct" and not model.supports_custom_tools
    assert not model.responses_websocket and not model.supports_hosted_web_search
    assert model.native_compaction and not model.extra["supports_reasoning_summary_parameter"]
    assert model.extra["use_responses_lite"] is False
    assert model.extra["apply_patch_tool_type"] is None
    assert model.source_model_metadata is not None
    assert not runtime.history_notes_backend_enabled(model)
    assert not get_image_generation_settings("gcp", settings)["enabled"]
    runtime.retire()


def test_configured_catalog_keeps_only_supported_vertex_models_and_forces_wire_capabilities(tmp_path):
    runtime = ModelRuntime(settings_snapshot={}, cwd=tmp_path, provider_configs={"gcp": {
        "project_id": "example", "models": [
            {"id": "gpt-6.1-sol", "tool_mode": "code_mode_only", "supports_custom_tools": True,
             "responses_websocket": True, "supports_hosted_web_search": True},
            {"id": "gpt-6-sol", "api": "openai-responses", "base_url": "https://unused.invalid"},
        ], "model_overrides": {"gpt-6.1-sol": {"tool_mode": "code_mode_only"}}}})
    assert runtime.get_error() is None
    assert [model.id for model in runtime.get_models("gcp")] == ["gpt-6.1-sol"]
    model = runtime.get_model("gcp", "gpt-6.1-sol")
    assert model.tool_mode == "direct" and not model.supports_custom_tools
    assert not model.responses_websocket and not model.supports_hosted_web_search
    assert model.base_url.endswith("/projects/example/locations/global/publishers/openai/v1")
    runtime.retire()


@pytest.mark.asyncio
async def test_real_command_runtime_factory_stream_and_side_query_use_captured_cwd(tmp_path, monkeypatch):
    config = {"auth": token_command(tmp_path), "proxy_mode": "direct"}
    with endpoint() as (url, requests):
        config["base_url"] = url
        runtime = ModelRuntime(settings_snapshot=settings_for(config), provider_configs={}, cwd=tmp_path)
        monkeypatch.chdir(tmp_path.parent)
        ticks = []
        async def tick():
            await asyncio.sleep(.01)
            ticks.append(True)
        await asyncio.gather(runtime.refresh_provider_auth("gcp"), tick())
        assert ticks
        adapter = build_provider_adapter("gcp", "gpt-6-astra", model_runtime=runtime)
        adapter.apply_model_request_policy(ModelRequestPolicy(model="gpt-6-astra", reasoning_summary="auto"))
        try:
            assert await adapter.simple_chat([LLMMessage(role="user", content="Read 中文")]) == "AWS_RESULT"
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="Stream 中文")])]
            assert any(event.type == StreamEventType.TEXT_CHUNK and event.content == "AWS_RESULT" for event in events)
            assert adapter.capabilities.image_generation is False
        finally:
            await adapter.aclose()
            runtime.retire()
        assert (tmp_path / "calls.json").read_text() == "1"
        assert len(requests) == 2
        for path, headers, _, body in requests:
            assert path == "/openai/v1/responses"
            assert httpx.Headers(headers)["authorization"] == "Bearer token-1"
            assert body["model"] == "gpt-6-astra"
            assert "summary" not in body.get("reasoning", {})


@pytest.mark.asyncio
async def test_command_cache_refresh_interval_zero_and_forced_refresh(tmp_path):
    config = command_auth_config(token_command(tmp_path), cwd=tmp_path)
    auth = CommandBearerAuth(config)
    assert await asyncio.gather(auth.token(), auth.token(), auth.token()) == ["token-1"] * 3
    auth._fetched_at -= 3001
    assert await auth.token() == "token-2"
    auth.config["refresh_interval_ms"] = 0
    auth._fetched_at -= 9999999
    assert await auth.token() == "token-2"
    assert await auth.token(force=True) == "token-3"
    assert (tmp_path / "calls.json").read_text() == "3"


@pytest.mark.asyncio
async def test_unauthorized_refresh_retries_once_with_real_new_token(tmp_path):
    config = {"auth": token_command(tmp_path, interval=0), "proxy_mode": "direct"}
    with endpoint(deny_first=True) as (url, requests):
        config["base_url"] = url
        settings = load_llm_settings(settings_for(config), cwd=tmp_path)
        adapter = create_session_llm(AppConfig(llm=settings))
        try:
            assert await adapter.simple_chat([LLMMessage(role="user", content="one request")]) == "AWS_RESULT"
        finally:
            await adapter.aclose()
        assert [httpx.Headers(request[1])["authorization"] for request in requests] == ["Bearer token-1", "Bearer token-2"]
        assert requests[0][2] == requests[1][2]
        assert (tmp_path / "calls.json").read_text() == "2"


@pytest.mark.asyncio
async def test_config_refresh_keeps_running_auth_owner_but_uses_new_endpoint_for_next_adapter(tmp_path):
    config = {"base_url": "https://first.invalid/v1", "auth": token_command(tmp_path)}
    runtime = ModelRuntime(settings_snapshot=settings_for(config), provider_configs={}, cwd=tmp_path)
    await runtime.refresh_provider_auth("gcp")
    before = runtime.resolve_adapter_spec("gcp", "gpt-6-astra")
    runtime.refresh(settings_snapshot=settings_for({**config, "base_url": "https://second.invalid/v1"}))
    await runtime.refresh_provider_auth("gcp")
    after = runtime.resolve_adapter_spec("gcp", "gpt-6-astra")
    assert before.base_url == "https://first.invalid/v1" and after.base_url == "https://second.invalid/v1"
    assert before.request_auth is not after.request_auth
    assert await before.request_auth.token() == "token-1"
    assert await after.request_auth.token() == "token-2"
    runtime.retire()


@pytest.mark.asyncio
async def test_memory_and_guardian_use_vertex_preferred_models_with_the_same_credentials(tmp_path):
    from backend.agent.guardian.config import review_model_for_execution

    with endpoint() as (url, requests):
        config = {"base_url": url, "auth": token_command(tmp_path), "proxy_mode": "direct"}
        runtime = ModelRuntime(settings_snapshot=settings_for(config), provider_configs={}, cwd=tmp_path)
        await runtime.refresh_provider_auth("gcp")
        adapter = build_provider_adapter("gcp", "gpt-6-astra", model_runtime=runtime)
        parent = SimpleNamespace(model_runtime=runtime, provider="gcp", model="gpt-6-astra",
            model_info=runtime.get_model("gcp", "gpt-6-astra"), config=AppConfig(llm=adapter._settings))
        assert review_model_for_execution(parent).model == "gpt-6-luna"
        try:
            await adapter.side_query([LLMMessage(role="user", content="phase1")], options=SideQueryOptions(operation="memory_phase1_extraction"))
            async with memory_consolidation_adapter(adapter) as consolidation:
                assert consolidation._provider_request_auth is adapter._provider_request_auth
                await consolidation.simple_chat([LLMMessage(role="user", content="phase2")])
            assert not adapter._closed and consolidation._closed
        finally:
            await adapter.aclose()
            runtime.retire()
        assert [request[3]["model"] for request in requests] == ["gpt-6-luna", "gpt-6.1-sol"]
        assert (tmp_path / "calls.json").read_text() == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_command_process_is_reaped_on_timeout_or_cancellation(tmp_path, mode):
    pid_file = tmp_path / "pid"
    script = "import os,pathlib,time;pathlib.Path('pid').write_text(str(os.getpid()));time.sleep(60)"
    auth = CommandBearerAuth(command_auth_config({"command": sys.executable, "args": ["-c", script],
        "timeout_ms": 600 if mode == "timeout" else 10000}, cwd=tmp_path))
    task = asyncio.create_task(auth.token())
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(.02)
    pid = int(pid_file.read_text())
    if mode == "cancel":
        task.cancel()
    with pytest.raises(TimeoutError if mode == "timeout" else asyncio.CancelledError):
        await task
    assert not psutil.pid_exists(pid)
    assert auth._token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("program,match", [
    ("print('  ')", "empty token"),
    ("import sys;sys.stdout.buffer.write(b'\\xff')", "non-UTF-8"),
    ("import sys;print('TOKEN-NEVER-IN-ERROR');print('Login required',file=sys.stderr);sys.exit(2)", "Login required"),
])
async def test_failed_command_does_not_publish_stdout_as_credentials(tmp_path, program, match):
    auth = CommandBearerAuth(command_auth_config({"command": sys.executable, "args": ["-c", program]}, cwd=tmp_path))
    with pytest.raises((OSError, ValueError), match=match) as error:
        await auth.token()
    assert "TOKEN-NEVER-IN-ERROR" not in str(error.value)
    assert auth._token is None


@pytest.mark.asyncio
async def test_failed_refresh_does_not_reuse_a_rejected_token(tmp_path):
    config = command_auth_config(token_command(tmp_path, interval=0), cwd=tmp_path)
    auth = CommandBearerAuth(config)
    assert await auth.token() == "token-1"
    (tmp_path / "token.py").write_text("import sys;sys.exit(3)")
    with pytest.raises(OSError, match="exited with 3"):
        await auth.token(rejected_generation=auth._generation)
    assert auth._token is None
    token_command(tmp_path)
    assert await auth.token() == "token-2"


@pytest.mark.asyncio
async def test_command_obeys_network_owner_before_spawn_and_on_revocation(tmp_path, monkeypatch):
    from backend.application_network import ApplicationNetworkController, ApplicationNetworkDenied, DestinationPolicy

    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy(frozenset({"aiplatform.googleapis.com"})))
    config = command_auth_config(token_command(tmp_path), cwd=tmp_path)
    original = CommandBearerAuth(config)
    restricted = original.with_network_policy(controller.policy.for_current_generation())
    assert restricted is not original
    with pytest.raises(ApplicationNetworkDenied):
        await restricted.token()
    assert not (tmp_path / "calls.json").exists()
    assert original._token is None

    controller.publish(0, DestinationPolicy())
    script = "import pathlib,os,time;pathlib.Path('pid').write_text(str(os.getpid()));time.sleep(60)"
    auth = CommandBearerAuth(command_auth_config({"command": sys.executable, "args": ["-c", script]}, cwd=tmp_path),
        network_policy=controller.policy.for_current_generation())
    task = asyncio.create_task(auth.token())
    for _ in range(100):
        if (tmp_path / "pid").exists():
            break
        await asyncio.sleep(.02)
    pid = int((tmp_path / "pid").read_text())
    controller.publish(0, DestinationPolicy(frozenset({"aiplatform.googleapis.com"})))
    with pytest.raises(ApplicationNetworkDenied):
        await task
    assert not psutil.pid_exists(pid)
    assert auth._token is None


@pytest.mark.asyncio
async def test_native_default_gcloud_command_uses_captured_session_directory(tmp_path, monkeypatch):
    token_command(tmp_path)
    if os.name == "nt":
        (tmp_path / "gcloud.cmd").write_text(f'@"{sys.executable}" "%~dp0token.py"\n')
    else:
        script = tmp_path / "gcloud"
        script.write_text(f'#!{sys.executable}\nexec(open({str(tmp_path / "token.py")!r}).read())\n')
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    runtime = ModelRuntime(settings_snapshot=settings_for({"project_id": "example"}), provider_configs={}, cwd=tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    await runtime.refresh_provider_auth("gcp")
    assert runtime.resolve_adapter_spec("gcp", "gpt-6-astra").api_key == "token-1"
    assert (tmp_path / "calls.json").read_text() == "1"
    runtime.retire()


@pytest.mark.asyncio
async def test_unconfigured_native_provider_fails_before_running_auth_or_sending(tmp_path):
    runtime = ModelRuntime(settings_snapshot={}, provider_configs={}, cwd=tmp_path)
    assert not runtime.has_configured_auth("gcp")
    with pytest.raises(ValueError, match="Set model_providers.gcp.project_id"):
        await runtime.refresh_provider_auth("gcp")
    runtime.retire()


@pytest.mark.asyncio
async def test_admitted_auth_refresh_transfers_the_new_command_owner(tmp_path):
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.ws.agent_runner import _refresh_session_model_auth

    config = {"base_url": "https://vertex.invalid/v1", "auth": token_command(tmp_path)}
    runtime = ModelRuntime(settings_snapshot=settings_for(config), provider_configs={}, cwd=tmp_path)
    await runtime.refresh_provider_auth("gcp")
    original = build_provider_adapter("gcp", "gpt-6-astra", model_runtime=runtime)
    snapshot = ModelExecutionSnapshot(config=AppConfig(llm=original._settings), llm=original,
        provider="gcp", model="gpt-6-astra", model_runtime=runtime, model_info=runtime.get_model("gcp", "gpt-6-astra"))
    session = SimpleNamespace(_llm_adapter_leases={}, _retired_llm_adapters={})
    runtime.refresh(settings_snapshot=settings_for({**config, "auth": {**config["auth"], "refresh_interval_ms": 0}}))
    refreshed = await _refresh_session_model_auth(session, snapshot, True, asyncio.current_task())
    try:
        assert refreshed.llm.provider_adapter_spec.api_key == "token-2"
        assert refreshed.model == snapshot.model and refreshed.model_info is snapshot.model_info
        assert refreshed.llm._provider_request_auth is runtime._gcp_auth().request_auth
        assert refreshed.llm._provider_request_auth is not original._provider_request_auth
        assert await original._provider_request_auth.token() == "token-1"
    finally:
        session._retired_llm_adapters.clear()
        await original.aclose()
        await refreshed.llm.aclose()
        runtime.retire()
