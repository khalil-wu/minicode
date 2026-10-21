"""Source Bedrock Mantle/runtime endpoints and AWS request authentication."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
import os

import httpx
from backend.async_cleanup import to_thread_cancel_safe


MANTLE_REGIONS = ("us-east-2", "us-east-1", "us-west-2", "ap-southeast-3", "ap-south-1",
    "ap-northeast-1", "eu-central-1", "eu-west-1", "eu-west-2", "eu-south-1", "eu-north-1", "sa-east-1",
    "us-gov-east-1", "us-gov-west-1")
MANTLE_MODELS = ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
    "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")
MODEL_ALIASES = {**{"openai." + model: model for model in MANTLE_MODELS},
    **{routing + "openai." + model: model for routing in ("global.", "us.") for model in MANTLE_MODELS[:-1]}}


def bedrock_models(config: Mapping) -> list[str]:
    if config.get("endpoint", "mantle") == "runtime":
        return [routing + "openai." + model for routing in ("global.", "us.") for model in MANTLE_MODELS[:-1]]
    models = MANTLE_MODELS
    if bedrock_region(config) in {"us-gov-east-1", "us-gov-west-1"}:
        models = ("gpt-5.6-terra", "gpt-5.6-luna")
    return ["openai." + model for model in models]


def aws_settings(value: Mapping) -> dict:
    """Read only host-owned provider configuration, never a tool/model payload."""
    result = deepcopy(dict(value))
    endpoint = result.get("endpoint", "mantle")
    if endpoint not in {"mantle", "runtime"}:
        raise ValueError("AWS endpoint must be mantle or runtime")
    result["endpoint"] = endpoint
    if "profile" in result and result["profile"] is not None:
        result["profile"] = result["profile"].strip()
        if not result["profile"]:
            raise ValueError("AWS profile name must not be empty")
    return result


def aws_session(config: Mapping, *, proxy_mode: str = "inherit", base_url: str = "", network_policy=None):
    from botocore.session import Session
    from botocore.config import Config
    session = Session(profile=config.get("profile"))
    from backend.application_network import application_network_policy
    policy = network_policy if network_policy is not None else application_network_policy()
    if policy.managed:
        from backend.llm.bedrock_network import install_aws_transport
        session.set_default_client_config(Config(proxies={}))
        install_aws_transport(session, policy, proxy_mode=proxy_mode)
        return session
    if proxy_mode == "direct":
        session.set_default_client_config(Config(proxies={}))
    elif base_url:
        from backend.llm.proxy_policy import provider_proxy_url_for_base_url
        proxy = provider_proxy_url_for_base_url(base_url, proxy_mode=proxy_mode)
        if proxy:
            session.set_default_client_config(Config(proxies={"http": proxy, "https": proxy}))
    return session


def bedrock_region(config: Mapping) -> str:
    region = str(config.get("region") or "").strip()
    if not region:
        region = str(os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            or aws_session(config).get_config_variable("region") or "").strip()
    if not region:
        raise ValueError("Amazon Bedrock requires an AWS region")
    if config.get("endpoint", "mantle") == "mantle" and region not in MANTLE_REGIONS:
        raise ValueError(f"Amazon Bedrock does not support region `{region}`")
    return region


def bedrock_base_url(config: Mapping) -> str:
    region = bedrock_region(config)
    if config.get("endpoint", "mantle") == "runtime":
        return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
    return f"https://bedrock-mantle.{region}.api.aws/openai/v1"


def discover_bedrock() -> dict:
    """Source discovery reads names and credential presence without resolving secrets."""
    profiles = aws_session({}).full_config.get("profiles", {})
    environment = []
    region = os.environ.get("AWS_REGION", "").strip() or None
    if os.environ.get("AWS_ACCESS_KEY_ID", "").strip() and os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip():
        environment.append({"type": "accessKeys", "region": region})
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        environment.append({"type": "bedrockApiKey", "region": region})
    return {"profiles": [{"name": name, "region": data.get("region")} for name, data in sorted(profiles.items())],
        "environmentCredentials": environment}


class BedrockRequestAuth(httpx.Auth):
    """Sign the final HTTP body using the selected SDK credential chain.

    The existing OpenAI Responses adapter owns serialization, retries, proxy
    policy and cancellation. AWS credentials and refresh stay on this host.
    """
    requires_request_body = True

    def __init__(self, config: Mapping, *, api_key: str = "", explicit_authorization: bool = False,
        proxy_mode: str = "inherit", base_url: str = "", network_policy=None):
        from backend.application_network import application_network_policy
        self.network_policy = network_policy if network_policy is not None else application_network_policy()
        self.config = aws_settings(config)
        self.region = bedrock_region(self.config)
        self.service = "bedrock" if self.config["endpoint"] == "runtime" else "bedrock-mantle"
        self.proxy_mode, self.base_url = proxy_mode, base_url
        self.session = aws_session(self.config, proxy_mode=proxy_mode, base_url=base_url,
            **({"network_policy": self.network_policy} if self.network_policy.managed else {}))
        self.explicit_authorization = explicit_authorization
        from backend.llm.bedrock_credentials import credential_export, auth_refresh
        self.exporter = credential_export(self.config, network_policy=self.network_policy)
        self.refresher = auth_refresh(self.config, network_policy=self.network_policy)
        managed = None
        if self.config.get("credential_id") and not self.config.get("profile") and self.exporter is None:
            import json
            from backend.vault.store import EnvVault
            raw = EnvVault().get(self.config["credential_id"])
            if raw is None:
                raise ValueError("The selected Amazon Bedrock credential is no longer stored")
            managed = json.loads(raw)
        # An explicitly selected profile has precedence over managed/API-key
        # and environment bearer credentials in Source.
        self.token = "" if self.config.get("profile") or self.exporter is not None else (str(managed.get("apiKey") or "") if managed is not None
            else api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip())
        self.credentials = None
        if managed is not None and managed["type"] == "amazonBedrockAccessKeys":
            from botocore.credentials import Credentials
            self.credentials = Credentials(managed["accessKeyId"], managed["secretAccessKey"], managed.get("sessionToken"))
            self.refresher = None
        self.lock = asyncio.Lock()

    def _sdk_credentials(self):
        from botocore.exceptions import NoCredentialsError
        if self.credentials is None:
            self.credentials = self.session.get_credentials()
        if self.credentials is None:
            raise NoCredentialsError()
        return self.credentials.get_frozen_credentials()

    async def _sign(self, request: httpx.Request) -> None:
        permit = self.network_policy.acquire(str(request.url))
        await permit.run(lambda: self._sign_authorized(request))

    async def _sign_authorized(self, request: httpx.Request) -> None:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        if self.exporter is not None:
            try:
                frozen = await self.exporter.get()
            except ValueError as error:
                raise ValueError("provider_error_type=auth: " + str(error)) from error
        else:
            if self.network_policy.managed:
                self.session._application_transport.bind_loop()
                credentials = asyncio.create_task(asyncio.to_thread(self._sdk_credentials))
                try:
                    frozen = await asyncio.shield(credentials)
                except asyncio.CancelledError:
                    self.session._application_transport.cancel_pending()
                    await asyncio.gather(credentials, return_exceptions=True)
                    raise
            else:
                frozen = await to_thread_cancel_safe(self._sdk_credentials)
        if self.service == "bedrock-mantle":
            # Source's Mantle front door drops snake_case compatibility
            # headers before verification. They cannot participate in SigV4.
            for key in list(request.headers):
                if "_" in key:
                    del request.headers[key]
        headers = {key: value for key, value in request.headers.items()
            if key.lower() not in {"authorization", "x-amz-date", "x-amz-security-token"}}
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(frozen, self.service, self.region).add_auth(signed)
        for key in ("authorization", "x-amz-date", "x-amz-security-token"):
            request.headers.pop(key, None)
        request.headers.update(dict(signed.headers.items()))

    async def async_auth_flow(self, request: httpx.Request):
        from backend.application_network import ApplicationNetworkDenied
        self.network_policy.acquire(str(request.url)).check()
        if self.explicit_authorization:
            yield request
            return
        if self.token:
            request.headers["authorization"] = "Bearer " + self.token
            yield request
            return
        from botocore.exceptions import BotoCoreError
        generation = self.refresher.generation if self.refresher is not None else 0
        recovered = False
        async with self.lock:
            try:
                await self._sign(request)
            except ApplicationNetworkDenied:
                raise
            except (BotoCoreError, OSError) as error:
                if (self.refresher is None and self.exporter is None) or isinstance(error, (FileNotFoundError, PermissionError)):
                    raise ValueError("provider_error_type=auth: Failed to load AWS credentials: " + str(error)) from error
                try:
                    await self._recover(generation)
                    recovered = True
                    await self._sign(request)
                except ApplicationNetworkDenied:
                    raise
                except (BotoCoreError, OSError) as recovery_error:
                    raise ValueError("provider_error_type=auth: Failed to load AWS credentials: " + str(recovery_error)) from recovery_error
        response = yield request
        if (self.refresher is None and self.exporter is None) or recovered:
            return
        if response.status_code == 401:
            refreshable = True
        elif response.status_code == 403:
            body = (await response.aread()).lower()
            refreshable = any(code in body for code in (b"expiredtoken", b"unrecognizedclientexception", b"invalidclienttokenid"))
        else:
            refreshable = False
        if refreshable:
            await response.aread()
            async with self.lock:
                await self._recover(generation)
                await self._sign(request)
            yield request

    async def _recover(self, generation: int):
        if self.exporter is not None:
            await self.exporter.recover(self.refresher)
        else:
            await self.refresher.refresh(generation)
        if self.network_policy.managed:
            await self.session._application_transport.aclose()
        self.session = aws_session(self.config, proxy_mode=self.proxy_mode, base_url=self.base_url,
            **({"network_policy": self.network_policy} if self.network_policy.managed else {}))
        self.credentials = None

    async def aclose(self):
        if self.network_policy.managed:
            await self.session._application_transport.aclose()
