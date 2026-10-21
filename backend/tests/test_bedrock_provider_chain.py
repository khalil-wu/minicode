from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, UTC, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
from dataclasses import replace

import httpx
import psutil
import pytest

from backend.config import load_llm_settings, save_llm_settings, get_custom_settings
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.bedrock import BedrockRequestAuth, bedrock_base_url, bedrock_models, discover_bedrock
from backend.llm.bedrock_credentials import AwsCredentialExport, AwsAuthRefresh
from backend.llm.model_runtime import ModelRuntime
from backend.services.llm_adapter_factory import build_provider_adapter
from backend.llm.openai_adapter import OpenAIAdapter


@pytest.fixture(autouse=True)
def aws_environment(tmp_path, monkeypatch):
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))


@contextmanager
def endpoint(*, deny_first=False):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            requests.append((self.path, dict(self.headers), raw, body))
            if deny_first and len(requests) == 1:
                result = b'{"message":"ExpiredToken"}'
                self.send_response(401); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(result))); self.end_headers(); self.wfile.write(result)
                return
            message = {"id": "aws-message", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "AWS_RESULT", "annotations": []}]}
            response = {"id": "aws-response", "status": "completed", "model": body["model"], "output": [message],
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
            if body.get("stream"):
                events = [{"type": "response.output_text.delta", "item_id": "aws-message", "output_index": 0,
                    "content_index": 0, "delta": "AWS_RESULT"}, {"type": "response.completed", "response": response}]
                result = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                media = "text/event-stream"
            else:
                result, media = json.dumps(response).encode(), "application/json"
            self.send_response(200); self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(result))); self.end_headers(); self.wfile.write(result)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}/openai/v1", requests
    finally: server.shutdown(); server.server_close(); thread.join()


def verify_signature(record, secret, service="bedrock-mantle"):
    path, headers, raw, _body = record
    headers = {key.lower(): value for key, value in headers.items()}
    authorization = headers["authorization"]
    fields = dict(piece.split("=", 1) for piece in authorization.split(" ", 1)[1].split(", "))
    _key, date, region, signed_service, terminal = fields["Credential"].split("/")
    assert signed_service == service and region == "us-east-1" and terminal == "aws4_request"
    signed_headers = fields["SignedHeaders"].split(";")
    canonical_headers = "".join(name + ":" + " ".join(headers[name].split()) + "\n" for name in signed_headers)
    canonical = "\n".join(["POST", path, "", canonical_headers, fields["SignedHeaders"], hashlib.sha256(raw).hexdigest()])
    scope = "/".join([date, region, service, "aws4_request"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", headers["x-amz-date"], scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    assert hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest() == fields["Signature"]


@pytest.mark.asyncio
async def test_actual_config_catalog_factory_stream_and_side_query_use_the_original_profile_and_signed_bytes(tmp_path, monkeypatch):
    Path(os.environ["AWS_SHARED_CREDENTIALS_FILE"]).write_text("[original]\naws_access_key_id=TESTACCESS\naws_secret_access_key=fixture-secret\naws_session_token=fixture-session\n")
    # Source profile beats environment bearer credentials.
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "other-environment-bearer")
    with endpoint() as (url, requests):
        settings = {"llm": {"provider": "custom", "custom": {"base_url": url, "model": "openai.gpt-6.1-sol",
            "available_models": ["openai.gpt-6.1-sol"], "wire_api": "responses", "proxy_mode": "direct",
            "aws": {"profile": "original", "region": "us-east-1"}}}}
        config = load_llm_settings(settings)
        assert config.aws["profile"] == "original"
        runtime = ModelRuntime(settings_snapshot=settings, provider_configs={})
        assert runtime.get_provider("custom").configured
        model = runtime.get_model("custom", config.model)
        assert model.extra["multi_agent_version"] == "v2"
        assert runtime.token_budget_auth_context(model).has_aws
        assert not runtime.history_notes_backend_enabled(model)
        adapter = build_provider_adapter("custom", config.model, model_runtime=runtime)
        settings["llm"]["custom"]["aws"]["region"] = "eu-west-1"
        runtime._base_providers["custom"]["aws"]["region"] = "eu-west-1"
        try:
            assert await adapter.simple_chat([LLMMessage(role="user", content="Read 中文")]) == "AWS_RESULT"
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="Stream 中文")])]
            assert any(event.type == StreamEventType.TEXT_CHUNK and event.content == "AWS_RESULT" for event in events)
        finally:
            await adapter.aclose(); runtime.retire()
        assert len(requests) == 2
        for record in requests:
            verify_signature(record, "fixture-secret")
            assert record[3]["model"] == "openai.gpt-6.1-sol"
            assert record[1]["X-Amz-Security-Token"] == "fixture-session"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_kind,service", [("mantle", "bedrock-mantle"), ("runtime", "bedrock")])
async def test_environment_access_keys_sign_the_actual_request(endpoint_kind, service, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTACCESS"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    with endpoint() as (url, requests):
        async with httpx.AsyncClient(auth=BedrockRequestAuth({"endpoint": endpoint_kind, "region": "us-east-1"})) as client:
            assert (await client.post(url + "/responses", headers={"thread_id": "original-thread", "session_id": "original-session"},
                json={"model": "explicit-model", "input": "body"})).status_code == 200
        verify_signature(requests[0], "fixture-secret", service)
        assert ("thread_id" in {key.lower() for key in requests[0][1]}) is (endpoint_kind == "runtime")


@pytest.mark.asyncio
async def test_api_key_and_explicit_header_precedence_use_no_sigv4(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "environment-bearer")
    with endpoint() as (url, requests):
        for auth, headers, expected in [
            (BedrockRequestAuth({"region": "us-east-1"}), {}, "Bearer environment-bearer"),
            (BedrockRequestAuth({"region": "us-east-1"}, api_key="managed-key"), {}, "Bearer managed-key"),
            (BedrockRequestAuth({"region": "us-east-1"}, explicit_authorization=True), {"authorization": "Bearer explicit-header"}, "Bearer explicit-header")]:
            async with httpx.AsyncClient(auth=auth) as client:
                await client.post(url + "/responses", headers=headers, json={"model": "explicit", "input": "body"})
            assert requests[-1][1]["authorization"] == expected
            assert "X-Amz-Date" not in requests[-1][1]


def test_source_endpoints_catalog_govcloud_and_discovery_do_not_resolve_credentials(tmp_path, monkeypatch):
    Path(os.environ["AWS_CONFIG_FILE"]).write_text("[profile profile-a]\nregion=eu-west-1\n[default]\nregion=us-east-1\n")
    monkeypatch.setenv("AWS_REGION", "us-east-1"); monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTACCESS")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    assert bedrock_base_url({"region": "us-east-1"}) == "https://bedrock-mantle.us-east-1.api.aws/openai/v1"
    assert bedrock_base_url({"region": "us-east-1", "endpoint": "runtime"}) == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    assert len(bedrock_models({"region": "us-east-1"})) == 8
    assert len(bedrock_models({"region": "us-east-1", "endpoint": "runtime"})) == 14
    assert bedrock_models({"region": "us-gov-west-1"}) == ["openai.gpt-5.6-terra", "openai.gpt-5.6-luna"]
    discovered = discover_bedrock()
    assert {item["name"] for item in discovered["profiles"]} == {"default", "profile-a"}
    assert discovered["environmentCredentials"] == [{"type": "accessKeys", "region": "us-east-1"}]
    assert "fixture-secret" not in json.dumps(discovered)
    with pytest.raises(ValueError, match="does not support region"):
        bedrock_base_url({"region": "us-west-1"})


def test_sdk_credential_clients_keep_the_selected_direct_or_inherited_proxy(monkeypatch):
    monkeypatch.delenv("LLM_PROXY_URL", raising=False)
    monkeypatch.setenv("MINICODE_LLM_PROXY_URL", "http://127.0.0.1:8181")
    monkeypatch.setenv("NO_PROXY", ""); monkeypatch.setenv("no_proxy", "")
    url = "https://bedrock-mantle.us-east-1.api.aws/openai/v1"
    direct = BedrockRequestAuth({"region": "us-east-1"}, proxy_mode="direct", base_url=url)
    inherited = BedrockRequestAuth({"region": "us-east-1"}, proxy_mode="inherit", base_url=url)
    assert direct.session.get_default_client_config().proxies == {}
    assert inherited.session.get_default_client_config().proxies == {"http": "http://127.0.0.1:8181", "https": "http://127.0.0.1:8181"}
    monkeypatch.setenv("MINICODE_LLM_PROXY_URL", "http://127.0.0.1:8282")
    assert inherited.session.get_default_client_config().proxies["https"].endswith(":8181")


@pytest.mark.asyncio
async def test_actual_credential_export_is_cached_signed_and_cancelled_under_its_process_owner(tmp_path):
    exporter_script = tmp_path / "export.py"
    count = tmp_path / "count"
    exporter_script.write_text("import json,pathlib,sys\np=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        "print(json.dumps({'AccessKeyId':'TESTACCESS','SecretAccessKey':'fixture-secret','SessionToken':'fixture-session'}))\n")
    config = {"command": sys.executable, "args": [str(exporter_script), str(count)]}
    auth = BedrockRequestAuth({"region": "us-east-1", "credential_export": config})
    with endpoint() as (url, requests):
        async with httpx.AsyncClient(auth=auth) as client:
            await asyncio.gather(*(client.post(url + "/responses", json={"model": "explicit", "input": "body"}) for _ in range(2)))
        assert count.read_text() == "x"
        for record in requests: verify_signature(record, "fixture-secret")
    pid_file = tmp_path / "pid"
    sleeper = AwsCredentialExport({"command": sys.executable, "args": ["-c",
        "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)", str(pid_file)]})
    task = asyncio.create_task(sleeper.get())
    for _ in range(100):
        if pid_file.exists(): break
        await asyncio.sleep(.02)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not psutil.pid_exists(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid", "oversize", "expired", "timeout"])
async def test_credential_export_reports_source_boundaries_without_secret_output(mode):
    if mode == "invalid": program = "print('SECRET-DO-NOT-ECHO')"
    elif mode == "oversize": program = "print('X'*65537)"
    elif mode == "timeout": program = "import time;time.sleep(60)"
    else: program = "import json;print(json.dumps({'AccessKeyId':'TEST','SecretAccessKey':'SECRET-DO-NOT-ECHO','Expiration':'2000-01-01T00:00:00Z'}))"
    exporter = AwsCredentialExport({"command": sys.executable, "args": ["-c", program], "timeout_ms": 150 if mode == "timeout" else 10000})
    with pytest.raises((ValueError, TimeoutError)) as failure: await exporter.get()
    assert "SECRET-DO-NOT-ECHO" not in str(failure.value)


def test_auth_refresh_accepts_only_the_source_command():
    with pytest.raises(ValueError, match="must be `aws`"):
        AwsAuthRefresh({"command": sys.executable})


@pytest.mark.asyncio
async def test_missing_sdk_credentials_are_a_real_auth_failure_before_network_request():
    from backend.config import LLMSettings
    with endpoint() as (url, requests):
        adapter = OpenAIAdapter(LLMSettings(api_key="", wire_api="responses", model="openai.gpt-6.1-sol", base_url=url,
            aws={"region": "us-east-1"}, proxy_mode="direct"))
        try:
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="body")])]
        finally:
            await adapter.aclose()
        failures = [event for event in events if event.type == StreamEventType.ERROR]
        assert len(failures) == 1 and failures[0].raw["provider_error_type"] == "auth"
        assert requests == []


@pytest.mark.parametrize("host,region,requirements,expected", [
    ("bedrock-mantle.us-east-1.api.aws", "us-gov-east-1", {}, (False, False)),
    ("bedrock-runtime-fips.us-gov-east-1.amazonaws.com", "us-east-1", {}, (True, True)),
    ("bedrock-mantle.us-gov-west-1.api.aws", "us-gov-west-1", {"allowed_login_methods": ["api"], "application": {
        "network": {"enabled": True, "domains": {"bedrock-mantle.us-gov-west-1.api.aws": "allow"}}}}, (True, False)),
    ("bedrock-mantle.us-gov-west-1.api.aws.proxy.invalid", "us-east-1", {}, (False, False)),
])
def test_govcloud_advisory_uses_source_exact_host_and_managed_requirements(host, region, requirements, expected):
    from types import SimpleNamespace
    from backend.config import LLMSettings
    from backend.config_requirements import ConfigRequirements
    from backend.services.bedrock_service import bedrock_gov_cloud_check
    config = SimpleNamespace(llm=LLMSettings(api_key="", base_url="https://" + host + "/openai/v1", aws={"region": region}),
        config_layer_stack=SimpleNamespace(requirements=ConfigRequirements(raw=requirements)))
    result = bedrock_gov_cloud_check(config)
    assert (result["isGovCloud"], result["shouldWarn"]) == expected


def test_provider_model_change_preserves_aws_and_endpoint_change_clears_it(monkeypatch):
    save_llm_settings({"provider": "custom", "custom": {"base_url": "https://bedrock-mantle.us-east-1.api.aws/openai/v1",
        "wire_api": "responses", "model": "openai.gpt-6.1-sol", "aws": {"region": "us-east-1"}}})
    save_llm_settings({"provider": "custom", "custom": {"model": "openai.gpt-6-sol"}})
    assert get_custom_settings(resolve_credentials=False)["aws"] == {"region": "us-east-1"}
    save_llm_settings({"provider": "custom", "custom": {"base_url": "https://another.invalid/v1"}})
    assert get_custom_settings(resolve_credentials=False)["aws"] is None


@pytest.mark.asyncio
async def test_managed_onboarding_keeps_private_secrets_and_each_original_credential_generation(tmp_path, monkeypatch):
    from backend.services import bedrock_service as service
    from backend.vault.store import EnvVault
    memory = {}
    monkeypatch.setattr("keyring.get_password", lambda namespace, name: memory.get((namespace, name)))
    monkeypatch.setattr("keyring.set_password", lambda namespace, name, value: memory.__setitem__((namespace, name), value))
    monkeypatch.setattr("keyring.delete_password", lambda namespace, name: memory.pop((namespace, name), None))
    async def allowed(**_kwargs): return None
    monkeypatch.setattr(service, "run_config_change_hook", allowed)
    first, config_a = await service.setup_bedrock(service.BedrockSetupRequest(type="amazonBedrockAccessKeys",
        region="us-east-1", accessKeyId="TESTA", secretAccessKey="original-secret", confirm_sensitive_change=True))
    second, config_b = await service.setup_bedrock(service.BedrockSetupRequest(type="amazonBedrockAccessKeys",
        region="us-east-1", accessKeyId="TESTB", secretAccessKey="new-secret", confirm_sensitive_change=True))
    assert config_a.llm.aws["credential_id"] != config_b.llm.aws["credential_id"]
    assert config_a.llm.api_key == config_b.llm.api_key == ""
    assert "original-secret" not in json.dumps(first) and "new-secret" not in json.dumps(second)
    from backend.config import SETTINGS_FILE
    assert "original-secret" not in SETTINGS_FILE.read_text(encoding="utf-8")
    assert "new-secret" not in SETTINGS_FILE.read_text(encoding="utf-8")
    assert not any(key.startswith("minicode-bedrock-") for key in EnvVault().inject_into_env())
    # Even an adapter first constructed after the credential change keeps the
    # original run's immutable reference rather than borrowing B's credentials.
    with endpoint() as (url, requests):
        a = OpenAIAdapter(replace(config_a.llm, base_url=url, proxy_mode="direct"))
        b = OpenAIAdapter(replace(config_b.llm, base_url=url, proxy_mode="direct"))
        try:
            assert await a.simple_chat([LLMMessage(role="user", content="original")]) == "AWS_RESULT"
            assert await b.simple_chat([LLMMessage(role="user", content="new")]) == "AWS_RESULT"
        finally:
            await a.aclose(); await b.aclose()
        verify_signature(requests[0], "original-secret"); verify_signature(requests[1], "new-secret")


@pytest.mark.asyncio
async def test_source_auth_refresh_runs_once_and_resigns_the_exact_body(tmp_path, monkeypatch):
    credentials = Path(os.environ["AWS_SHARED_CREDENTIALS_FILE"])
    credentials.write_text("[rotating]\naws_access_key_id=TESTOLD\naws_secret_access_key=old-secret\n")
    receipt = tmp_path / "refresh-count"
    program = tmp_path / "refresh.py"
    program.write_text("import pathlib,sys\np=pathlib.Path(sys.argv[1]);p.write_text('[rotating]\\naws_access_key_id=TESTNEW\\naws_secret_access_key=new-secret\\n')\n"
        "q=pathlib.Path(sys.argv[2]);q.write_text(q.read_text()+'x' if q.exists() else 'x')\n")
    original_spawn = asyncio.create_subprocess_exec
    async def dispatch(command, *args, **kwargs):
        assert command == "aws"
        return await original_spawn(sys.executable, *args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", dispatch)
    auth = BedrockRequestAuth({"region": "us-east-1", "profile": "rotating", "auth_refresh": {
        "command": "aws", "args": [str(program), str(credentials), str(receipt)]}})
    with endpoint(deny_first=True) as (url, requests):
        async with httpx.AsyncClient(auth=auth) as client:
            response = await client.post(url + "/responses", json={"model": "explicit", "input": "unchanged 中文"})
        assert response.status_code == 200 and len(requests) == 2
        assert requests[0][2] == requests[1][2]
        verify_signature(requests[0], "old-secret"); verify_signature(requests[1], "new-secret")
        assert receipt.read_text() == "x"


@pytest.mark.asyncio
async def test_setup_profile_and_environment_preserve_existing_refresh_headers_and_model(monkeypatch):
    from backend.services import bedrock_service as service
    async def allowed(**_kwargs): return None
    monkeypatch.setattr(service, "run_config_change_hook", allowed)
    credentials = Path(os.environ["AWS_SHARED_CREDENTIALS_FILE"])
    credentials.write_text("[engineering]\naws_access_key_id=TESTPROFILE\naws_secret_access_key=profile-secret\n")
    refresh = {"command": "aws", "args": ["login"]}
    save_llm_settings({"provider": "custom", "custom": {
        "base_url": "https://bedrock-mantle.us-east-1.api.aws/openai/v1", "wire_api": "responses",
        "model": "openai.gpt-6-luna", "headers": {"X-Existing": "preserved"},
        "aws": {"region": "us-east-1", "profile": "old", "auth_refresh": refresh}}})
    _, profile = await service.setup_bedrock(service.BedrockSetupRequest(type="profile", profile=" engineering ",
        region="us-west-2", confirm_sensitive_change=True))
    assert profile.llm.aws == {"endpoint": "mantle", "region": "us-west-2", "profile": "engineering", "auth_refresh": refresh}
    assert profile.llm.model == "openai.gpt-6-luna"
    assert dict(profile.llm.default_headers) == {"X-Existing": "preserved"}
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "environment-secret")
    _, environment = await service.setup_bedrock(service.BedrockSetupRequest(type="environment",
        region="us-east-2", confirm_sensitive_change=True))
    assert environment.llm.aws == {"endpoint": "mantle", "region": "us-east-2", "auth_refresh": refresh}
    assert environment.llm.model == "openai.gpt-6-luna"
    assert dict(environment.llm.default_headers) == {"X-Existing": "preserved"}


@pytest.mark.asyncio
@pytest.mark.parametrize("aws,kind,message", [
    ({"credential_export": {"command": "exporter"}}, "amazonBedrock", "credential_export"),
    ({"credential_id": "original-managed-credential"}, "environment", "Managed Bedrock credentials"),
])
async def test_setup_does_not_discard_an_existing_credential_source(aws, kind, message):
    from backend.services import bedrock_service as service
    from backend.config import SETTINGS_FILE
    save_llm_settings({"provider": "custom", "custom": {
        "base_url": "https://bedrock-mantle.us-east-1.api.aws/openai/v1", "wire_api": "responses",
        "model": "openai.gpt-6-luna", "aws": {"region": "us-east-1", **aws}}})
    before = SETTINGS_FILE.read_bytes()
    with pytest.raises(ValueError, match=message):
        await service.setup_bedrock(service.BedrockSetupRequest(type=kind, apiKey="replacement-api-key",
            region="us-east-1", confirm_sensitive_change=True))
    assert SETTINGS_FILE.read_bytes() == before


@pytest.mark.asyncio
async def test_exporter_only_auth_recovery_resigns_the_actual_request_without_a_login_command(tmp_path):
    program = tmp_path / "rotate.py"
    receipt = tmp_path / "exports"
    program.write_text("import json,pathlib,sys\np=pathlib.Path(sys.argv[1]);n=len(p.read_text()) if p.exists() else 0;p.write_text('x'*(n+1))\n"
        "print(json.dumps({'AccessKeyId':'TEST','SecretAccessKey':'old-secret' if n==0 else 'new-secret'}))\n")
    auth = BedrockRequestAuth({"region": "us-east-1", "credential_export": {
        "command": sys.executable, "args": [str(program), str(receipt)]}})
    with endpoint(deny_first=True) as (url, requests):
        async with httpx.AsyncClient(auth=auth) as client:
            response = await client.post(url + "/responses", json={"model": "explicit", "input": "original bytes"})
        assert response.status_code == 200 and len(requests) == 2
        assert requests[0][2] == requests[1][2]
        verify_signature(requests[0], "old-secret"); verify_signature(requests[1], "new-secret")
    assert receipt.read_text() == "xx" and auth.exporter.recovery_generation == 1


@pytest.mark.asyncio
async def test_temporary_exporter_failure_recovers_before_sending_any_anonymous_request(tmp_path):
    program = tmp_path / "initial.py"
    receipt = tmp_path / "exports"
    program.write_text("import json,pathlib,sys\np=pathlib.Path(sys.argv[1]);n=len(p.read_text()) if p.exists() else 0;p.write_text('x'*(n+1))\n"
        "if n==0: sys.exit(1)\nprint(json.dumps({'AccessKeyId':'TEST','SecretAccessKey':'new-secret'}))\n")
    auth = BedrockRequestAuth({"region": "us-east-1", "credential_export": {
        "command": sys.executable, "args": [str(program), str(receipt)]}})
    with endpoint() as (url, requests):
        async with httpx.AsyncClient(auth=auth) as client:
            response = await client.post(url + "/responses", json={"model": "explicit", "input": "body"})
        assert response.status_code == 200 and len(requests) == 1
        verify_signature(requests[0], "new-secret")
    assert receipt.read_text() == "xx" and auth.exporter.recovery_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["export_failure", "login_failure", "cancelled"])
async def test_refresh_holds_the_original_export_cache_and_failure_never_republishes_rejected_credentials(tmp_path, monkeypatch, outcome):
    from backend.llm import bedrock_credentials as module
    output = tmp_path / "credentials.json"
    output.write_text(json.dumps({"AccessKeyId": "TESTOLD", "SecretAccessKey": "old-secret"}))
    program = tmp_path / "read.py"
    program.write_text("import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())")
    config = {"region": "us-east-1", "profile": "first", "credential_export": {
        "command": sys.executable, "args": [str(program), str(output)]}, "auth_refresh": {"command": "aws", "args": ["login"]}}
    first, overlapping = BedrockRequestAuth(config), BedrockRequestAuth(config)
    other = BedrockRequestAuth({**config, "profile": "second"})
    assert first.exporter is overlapping.exporter and first.refresher is overlapping.refresher
    assert first.exporter is not other.exporter and first.refresher is not other.refresher
    assert (await first.exporter.get()).access_key == "TESTOLD"
    entered, release = asyncio.Event(), asyncio.Event()
    original_command = module.run_aws_command
    async def login_gate(command, *, export):
        if export:
            return await original_command(command, export=True)
        entered.set()
        await release.wait()
        if outcome == "login_failure":
            raise OSError("AWS auth refresh command exited with 1")
        return b""
    monkeypatch.setattr(module, "run_aws_command", login_gate)
    output.write_text("invalid JSON which must not appear in the error")
    recovery = asyncio.create_task(first._recover(0))
    await asyncio.wait_for(entered.wait(), 2)
    waiting = asyncio.create_task(overlapping.exporter.get())
    await asyncio.sleep(0)
    assert not waiting.done() and first.exporter.cached is None
    if outcome == "cancelled":
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError): await recovery
    else:
        release.set()
        with pytest.raises((ValueError, OSError)): await recovery
    with pytest.raises(ValueError, match="invalid credentials JSON"): await waiting
    assert first.exporter.cached is None and first.exporter.recovery_generation == 0
    assert first.refresher.generation == (1 if outcome == "export_failure" else 0)
    output.write_text(json.dumps({"AccessKeyId": "TESTNEW", "SecretAccessKey": "new-secret"}))
    assert (await overlapping.exporter.get()).access_key == "TESTNEW"
