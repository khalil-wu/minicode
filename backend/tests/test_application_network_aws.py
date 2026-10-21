from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import sys

import httpx
import pytest

from backend.application_network import (
    ApplicationHttpClient, ApplicationNetworkController, ApplicationNetworkDenied, DestinationPolicy,
)
from backend.llm.bedrock import BedrockRequestAuth, aws_session
from backend.llm.bedrock_credentials import AwsAuthRefresh, AwsCredentialExport


MODEL_HOST = "bedrock-mantle.us-east-1.api.aws"
MODEL_URL = "https://" + MODEL_HOST + "/openai/v1/responses"


@pytest.fixture
def isolated_aws(tmp_path, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
            "AWS_BEARER_TOKEN_BEDROCK", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    config, credentials = tmp_path / "config", tmp_path / "credentials"
    config.write_text("", encoding="utf-8")
    credentials.write_text("", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return config


def owner(*hosts):
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy(frozenset(hosts)))
    return controller, controller.policy.for_current_generation()


def intercept_http(monkeypatch, handler):
    def client(_url, *, policy, **_kwargs):
        return ApplicationHttpClient(policy, lambda _destination: httpx.AsyncClient(transport=httpx.MockTransport(handler)), timeout=None)
    monkeypatch.setattr("backend.llm.bedrock_network.application_http_client", client)


@pytest.mark.asyncio
async def test_restricted_exporter_and_reauthentication_are_denied_before_a_process_is_created(monkeypatch):
    _controller, policy = owner(MODEL_HOST)
    invoked = []
    async def spawn(*args, **kwargs):
        invoked.append((args, kwargs))
        raise AssertionError("A restricted external command reached process creation")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    exporter = AwsCredentialExport({"command": sys.executable, "args": ["fixture"]}, network_policy=policy)
    refresh = AwsAuthRefresh({"command": "aws", "args": ["sso", "login"]}, network_policy=policy)
    for operation in (exporter.get, lambda: exporter.recover(refresh), lambda: refresh.refresh(0)):
        with pytest.raises(ApplicationNetworkDenied, match="SDK transport"):
            await operation()
    assert not invoked and exporter.cached is None


@pytest.mark.asyncio
async def test_running_unrestricted_exporter_is_killed_when_restrictions_are_published(monkeypatch):
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy())
    policy = controller.policy.for_current_generation()
    processes, started = [], asyncio.Event()
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    exporter = AwsCredentialExport({"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}, network_policy=policy)
    task = asyncio.create_task(exporter.get())
    await started.wait()
    controller.publish(0, DestinationPolicy(frozenset({MODEL_HOST})))
    with pytest.raises(ApplicationNetworkDenied, match="revoked"):
        await asyncio.wait_for(task, 3)
    assert len(processes) == 1 and processes[0].returncode is not None and exporter.cached is None


@pytest.mark.asyncio
async def test_actual_botocore_container_credentials_use_admitted_application_http_before_sigv4(isolated_aws, monkeypatch):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "https://credentials.allowed.test/credentials")
    _controller, policy = owner(MODEL_HOST, "credentials.allowed.test")
    calls = []
    async def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"AccessKeyId": "fixture-access", "SecretAccessKey": "fixture-secret", "Token": "fixture-token",
            "Expiration": (datetime.now(UTC) + timedelta(hours=1)).isoformat()})
    intercept_http(monkeypatch, respond)
    auth = BedrockRequestAuth({"region": "us-east-1"}, network_policy=policy, proxy_mode="direct", base_url=MODEL_URL)
    try:
        request = httpx.Request("POST", MODEL_URL, content=b"{}")
        await auth._sign(request)
        assert calls == ["https://credentials.allowed.test/credentials"]
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=fixture-access/")
        assert request.headers["x-amz-security-token"] == "fixture-token"
    finally:
        await auth.aclose()


@pytest.mark.asyncio
async def test_actual_imds_destination_denial_precedes_proxy_dns_or_credential_transport(isolated_aws, monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "false")
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", "https://metadata.denied.test")
    _controller, policy = owner(MODEL_HOST)
    calls = []
    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Denied SDK target reached HTTP client/proxy creation")
    monkeypatch.setattr("backend.llm.bedrock_network.application_http_client", forbidden)
    auth = BedrockRequestAuth({"region": "us-east-1"}, network_policy=policy, proxy_mode="direct")
    try:
        with pytest.raises(ApplicationNetworkDenied, match="destination"):
            await auth._sign(httpx.Request("POST", MODEL_URL, content=b"{}"))
        assert not calls
    finally:
        await auth.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_host", [MODEL_HOST, "credentials.allowed.test"])
async def test_pending_sdk_credential_response_is_revoked_without_requiring_both_destinations_to_change(isolated_aws, monkeypatch, revoked_host):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "https://credentials.allowed.test/credentials")
    controller, policy = owner(MODEL_HOST, "credentials.allowed.test")
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def respond(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    intercept_http(monkeypatch, respond)
    auth = BedrockRequestAuth({"region": "us-east-1"}, network_policy=policy, proxy_mode="direct")
    try:
        task = asyncio.create_task(auth._sign(httpx.Request("POST", MODEL_URL, content=b"{}")))
        await entered.wait()
        controller.publish(0, DestinationPolicy(frozenset({MODEL_HOST, "credentials.allowed.test"} - {revoked_host})))
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            await asyncio.wait_for(task, 2)
        assert cancelled.is_set()
    finally:
        await auth.aclose()


@pytest.mark.asyncio
async def test_botocore_sts_endpoint_uses_the_same_real_transport_instead_of_its_default_session(isolated_aws, monkeypatch):
    _controller, policy = owner("sts.allowed.test")
    requests = []
    async def respond(request):
        requests.append(request)
        xml = '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><GetCallerIdentityResult><Arn>arn:aws:iam::123456789012:user/fixture</Arn><UserId>fixture-user</UserId><Account>123456789012</Account></GetCallerIdentityResult><ResponseMetadata><RequestId>fixture-request</RequestId></ResponseMetadata></GetCallerIdentityResponse>'
        return httpx.Response(200, headers={"content-type": "text/xml"}, content=xml)
    intercept_http(monkeypatch, respond)
    session = aws_session({}, network_policy=policy, proxy_mode="direct")
    session._application_transport.bind_loop()
    client = session.create_client("sts", region_name="us-east-1", endpoint_url="https://sts.allowed.test",
        aws_access_key_id="fixture-access", aws_secret_access_key="fixture-secret")
    def forbidden(_request):
        raise AssertionError("SDK bypassed the application transport")
    monkeypatch.setattr(client._endpoint.http_session, "send", forbidden)
    try:
        response = await asyncio.to_thread(client.get_caller_identity)
        assert response["Account"] == "123456789012" and len(requests) == 1
        assert requests[0].url.host == "sts.allowed.test"
    finally:
        client.close()
        await session._application_transport.aclose()


@pytest.mark.asyncio
async def test_sdk_credential_process_is_gated_in_the_real_default_provider_chain(isolated_aws, monkeypatch):
    isolated_aws.write_text('[profile audit]\ncredential_process = python -c "print(1)"\n', encoding="utf-8")
    _controller, policy = owner(MODEL_HOST)
    auth = BedrockRequestAuth({"region": "us-east-1", "profile": "audit"}, network_policy=policy, proxy_mode="direct")
    from botocore.credentials import ProcessProvider
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Restricted credential_process was spawned")
    monkeypatch.setattr(ProcessProvider, "_retrieve_credentials_using", forbidden)
    try:
        with pytest.raises(ApplicationNetworkDenied, match="SDK transport"):
            await auth._sign(httpx.Request("POST", MODEL_URL, content=b"{}"))
    finally:
        await auth.aclose()
