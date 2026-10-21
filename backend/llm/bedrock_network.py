"""Botocore credential HTTP uses the application's admitted async transport.

Botocore runs in its existing credential worker. Its HTTP boundary dispatches
onto the owning event loop, where destination permits cancel real I/O/body reads.
No process-wide HTTPX or botocore patch is installed.
"""
from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import CancelledError

import httpx
from botocore import credentials as sdk
from botocore.awsrequest import AWSResponse
from botocore.utils import ContainerMetadataFetcher, InstanceMetadataFetcher, METADATA_BASE_URL

from backend.application_network import ApplicationNetworkDenied, application_http_client


class _Body:
    def __init__(self, content, permit):
        self.content, self.permit = content, permit

    def stream(self, amt=None, decode_content=False):
        self.permit.check()
        size = amt or max(1, len(self.content))
        for offset in range(0, len(self.content), size):
            self.permit.check()
            yield self.content[offset:offset + size]


class _AWSResponse(AWSResponse):
    def __init__(self, response, content):
        super().__init__(str(response.url), response.status_code, dict(response.headers), _Body(content, response.permit))
        self.permit = response.permit

    @property
    def content(self):
        self.permit.check()
        return super().content


class AwsApplicationTransport:
    def __init__(self, policy, proxy_mode, *, timeout=60, owner=None):
        self.policy, self.proxy_mode, self.timeout = policy, proxy_mode, timeout
        self.owner = owner if owner is not None else self
        self.loop = None
        self.client = None
        self.children = []
        self.lock = threading.Lock()
        self.pending = set()
        self.processes = set()
        self.cancelled = False

    def with_timeout(self, timeout):
        child = AwsApplicationTransport(self.policy, self.proxy_mode, timeout=timeout, owner=self.owner)
        self.owner.children.append(child)
        return child

    def bind_loop(self):
        with self.owner.lock:
            self.owner.loop = asyncio.get_running_loop()
            self.owner.cancelled = False

    def cancel_pending(self):
        with self.owner.lock:
            self.owner.cancelled = True
            for future in self.owner.pending:
                future.cancel()
            for process in self.owner.processes:
                if process.poll() is None:
                    process.kill()

    def send(self, request, **_kwargs):
        # The SDK transport is synchronous; the caller must be the credential
        # worker owned by BedrockRequestAuth, never the event loop itself.
        self.policy.acquire(request.url).check()
        if self.owner.loop is None:
            raise ApplicationNetworkDenied("unsupported_transport")
        with self.owner.lock:
            if self.owner.cancelled:
                raise CancelledError()
            future = asyncio.run_coroutine_threadsafe(self._send(request), self.owner.loop)
            self.owner.pending.add(future)
        try:
            return future.result()
        finally:
            with self.owner.lock:
                self.owner.pending.remove(future)

    async def _send(self, request):
        if self.client is None:
            self.client = application_http_client(request.url, policy=self.policy,
                proxy_mode=self.proxy_mode, timeout=self.timeout, follow_redirects=False)
        headers = {key: value.decode("latin-1") if isinstance(value, bytes) else value for key, value in request.headers.items()}
        async with self.client.stream(request.method, request.url, headers=headers, content=request.body) as response:
            content = await response.aread()
            return _AWSResponse(response, content)

    async def aclose(self):
        self.cancel_pending()
        await asyncio.gather(*(child.aclose() for child in self.children))
        if self.client is not None:
            await self.client.aclose()


class _InstanceMetadataFetcher(InstanceMetadataFetcher):
    def __init__(self, session, transport, config):
        # Botocore's constructor eagerly resolves proxies before accepting a
        # transport. Keep its endpoint/config semantics while injecting here.
        self._timeout = session.get_config_variable("metadata_service_timeout")
        self._num_attempts = session.get_config_variable("metadata_service_num_attempts")
        self._base_url = self._select_base_url(METADATA_BASE_URL, config)
        self._config = config
        self._disabled = os.environ.get("AWS_EC2_METADATA_DISABLED", "false").lower() == "true"
        self._imds_v1_disabled = config.get("ec2_metadata_v1_disabled")
        self._user_agent = session.user_agent()
        self._session = transport.with_timeout(self._timeout)


class _ProcessProvider(sdk.ProcessProvider):
    def __init__(self, provider, policy, transport):
        super().__init__(provider._profile_name, provider._load_config, provider._popen)
        self.policy = policy
        self.transport = transport

    def _retrieve_credentials_using(self, command):
        permit = self.policy.acquire_for_unsupported_sdk()
        processes = []
        def spawn(*args, **kwargs):
            permit.check()
            process = self._popen(*args, **kwargs)
            processes.append(process)
            with self.transport.owner.lock:
                self.transport.owner.processes.add(process)
                if self.transport.owner.cancelled and process.poll() is None:
                    process.kill()
            def revoke(_future):
                if process.poll() is None:
                    process.kill()
            permit.revocation.add_done_callback(revoke)
            return process
        provider = sdk.ProcessProvider(self._profile_name, self._load_config, spawn)
        try:
            return provider._retrieve_credentials_using(command)
        finally:
            with self.transport.owner.lock:
                self.transport.owner.processes.difference_update(processes)
            permit.check()


class _ProfileProviders(sdk.ProfileProviderBuilder):
    def __init__(self, session, policy, cache, transport):
        super().__init__(session, cache=cache)
        self.policy = policy
        self.transport = transport

    def providers(self, profile_name, disable_env_vars=False):
        return [_ProcessProvider(provider, self.policy, self.transport) if isinstance(provider, sdk.ProcessProvider) else provider
            for provider in super().providers(profile_name, disable_env_vars)]


def install_aws_transport(session, policy, *, proxy_mode):
    """Keep botocore's provider order and inject its actual HTTP consumers."""
    transport = AwsApplicationTransport(policy, proxy_mode)
    session._application_transport = transport
    session.register("before-send", transport.send)
    session._components.lazy_register_component("credential_provider", lambda: _credential_resolver(session, policy, transport))
    return transport


def _credential_resolver(session, policy, transport):
    profile = session.get_config_variable("profile") or "default"
    disable_environment = session.instance_variables().get("profile") is not None
    cache = {}
    env = sdk.EnvProvider()
    container = sdk.ContainerProvider(fetcher=ContainerMetadataFetcher(session=transport.with_timeout(ContainerMetadataFetcher.TIMEOUT_SECONDS)))
    imds_config = {
        "ec2_metadata_service_endpoint": session.get_config_variable("ec2_metadata_service_endpoint"),
        "ec2_metadata_service_endpoint_mode": sdk.resolve_imds_endpoint_mode(session),
        "ec2_credential_refresh_window": sdk._DEFAULT_ADVISORY_REFRESH_TIMEOUT,
        "ec2_metadata_v1_disabled": session.get_config_variable("ec2_metadata_v1_disabled"),
    }
    instance = sdk.InstanceMetadataProvider(iam_role_fetcher=_InstanceMetadataFetcher(session, transport, imds_config))
    profiles = _ProfileProviders(session, policy, cache, transport)
    role = sdk.AssumeRoleProvider(load_config=lambda: session.full_config,
        client_creator=sdk._get_client_creator(session, None), cache=cache, profile_name=profile,
        credential_sourcer=sdk.CanonicalNameCredentialSourcer([env, container, instance]), profile_provider_builder=profiles)
    pre = [role] if disable_environment else [env, role]
    providers = pre + profiles.providers(profile, disable_env_vars=disable_environment) + [sdk.OriginalEC2Provider(), sdk.BotoProvider(), container, instance]
    return sdk.CredentialResolver(providers)
