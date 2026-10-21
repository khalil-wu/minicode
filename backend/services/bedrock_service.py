"""User-configured AWS onboarding feeds the existing provider/vault pipeline."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Literal
from uuid import uuid4
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from backend.config import SETTINGS_FILE, get_custom_settings, load_config, save_llm_settings
from backend.hooks.runtime import run_config_change_hook, raise_if_config_change_blocked
from backend.llm.bedrock import aws_session, bedrock_base_url, bedrock_models
from backend.llm.model_metadata import source_guardian_model_metadata
from backend.vault.store import EnvVault
from backend.application_network import application_network_policy, reload_application_config


def bedrock_gov_cloud_check(config=None) -> dict:
    config = config if config is not None else load_config(cwd=None, resolve_credentials=False)
    aws = config.llm.aws
    if aws is None:
        return {"isGovCloud": False, "shouldWarn": False}
    domain = urlsplit(config.llm.base_url).hostname
    if not domain:
        raise ValueError("Amazon Bedrock endpoint has no valid hostname")
    domain = domain.rstrip(".")
    region = None
    service, _, suffix = domain.partition(".")
    if service == "bedrock-mantle" and suffix.endswith(".api.aws"):
        candidate = suffix[:-len(".api.aws")]
        if candidate and "." not in candidate:
            region = candidate
    elif service in {"bedrock-runtime", "bedrock-runtime-fips"} and suffix.endswith(".amazonaws.com"):
        candidate = suffix[:-len(".amazonaws.com")]
        if candidate and "." not in candidate:
            region = candidate
    if region is None:
        from backend.llm.bedrock import bedrock_region
        region = bedrock_region(aws)
    if region not in {"us-gov-east-1", "us-gov-west-1"}:
        return {"isGovCloud": False, "shouldWarn": False}
    requirements = config.config_layer_stack.requirements.raw if config.config_layer_stack is not None else {}
    methods = requirements.get("allowed_login_methods", [])
    network = requirements.get("application", {}).get("network", {})
    api_only = bool(methods) and all(method == "api" for method in methods)
    network_allowed = network.get("enabled", True) is True and network.get("domains", {}).get(domain) == "allow"
    return {"isGovCloud": True, "shouldWarn": not api_only or not network_allowed}


class BedrockSetupRequest(BaseModel):
    type: Literal["profile", "environment", "amazonBedrock", "amazonBedrockAccessKeys"]
    region: str
    profile: str | None = None
    endpoint: Literal["mantle", "runtime"] = "mantle"
    apiKey: str = Field(default="", repr=False)
    accessKeyId: str = Field(default="", repr=False)
    secretAccessKey: str = Field(default="", repr=False)
    sessionToken: str | None = Field(default=None, repr=False)
    confirm_sensitive_change: bool = False


async def setup_bedrock(request: BedrockSetupRequest):
    if not request.confirm_sensitive_change:
        raise ValueError("LLM settings changes require explicit confirmation")
    previous = get_custom_settings(resolve_credentials=False)
    previous_aws = previous["aws"]
    aws = dict(previous_aws) if previous_aws is not None else {}
    if aws.get("credential_export") is not None:
        raise ValueError("Amazon Bedrock is configured to use `aws.credential_export`. Please clear this setting to use another sign-in method.")
    aws.update(region=request.region.strip(), endpoint=request.endpoint)
    aws.pop("profile", None)
    endpoint = bedrock_base_url(aws)
    credential = None
    if request.type == "profile":
        profile = (request.profile or "").strip()
        if not profile:
            raise ValueError("AWS profile name must not be empty")
        aws["profile"] = profile
        policy = application_network_policy()
        permit = policy.acquire(endpoint)
        validation_session = aws_session(aws, **({"network_policy": policy} if policy.managed else {}))
        if policy.managed:
            validation_session._application_transport.bind_loop()
        def validate_profile():
            credentials = validation_session.get_credentials()
            if credentials is None:
                raise ValueError("The selected AWS profile has no credentials")
            credentials.get_frozen_credentials()
        try:
            await permit.run(lambda: asyncio.to_thread(validate_profile))
        finally:
            if policy.managed:
                await validation_session._application_transport.aclose()
    elif request.type == "environment":
        if aws.get("credential_id"):
            raise ValueError("Managed Bedrock credentials take priority over AWS environment credentials; clear the stored credentials first")
        if not (os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
            or (os.environ.get("AWS_ACCESS_KEY_ID", "").strip() and os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip())):
            raise ValueError("No AWS environment credentials were found")
        from backend.config import resolve_provider_api_key_for_base_url
        if resolve_provider_api_key_for_base_url("custom", endpoint):
            raise ValueError("A stored Bedrock API key takes priority over environment credentials; clear the stored key first")
    elif request.type == "amazonBedrock":
        if not request.apiKey.strip():
            raise ValueError("Amazon Bedrock API key must not be empty")
        credential = {"type": request.type, "apiKey": request.apiKey.strip()}
    else:
        if not request.accessKeyId.strip() or not request.secretAccessKey.strip():
            raise ValueError("AWS access key ID and secret access key are required")
        credential = {"type": request.type, "accessKeyId": request.accessKeyId.strip(),
            "secretAccessKey": request.secretAccessKey.strip(), "sessionToken": request.sessionToken}
    hook = await run_config_change_hook(source="llm", file_path=str(SETTINGS_FILE))
    raise_if_config_change_blocked(hook, source="llm", file_path=str(SETTINGS_FILE))
    models = bedrock_models(aws)
    model = previous["model"] if previous_aws is not None and previous["model"] in models else models[0]
    payload = {"provider": "custom", "custom": {"display_name": "Amazon Bedrock", "base_url": endpoint,
        "aws": aws, "wire_api": "responses", "model": model, "available_models": models,
        "models_source": "source_bedrock_catalog", "model_metadata": {model: source_guardian_model_metadata(model) for model in models},
        "image_mode": "disabled", "auth_header": previous["auth_header"] if previous_aws is not None else False,
        "headers": previous["default_headers"] if previous_aws is not None else {}}}
    published = None
    config = None
    def persist():
        nonlocal published, config
        def reload():
            nonlocal config
            config = reload_application_config(load_config, invalidate=True)
        published = save_llm_settings(payload, after_publish=reload)
    if credential is not None:
        key = "minicode-bedrock-" + hashlib.sha256(endpoint.encode()).hexdigest() + "-" + uuid4().hex
        aws["credential_id"] = key
        EnvVault().set_many({key: (json.dumps(credential), "Amazon Bedrock credentials", "provider_auth")}, publish=persist)
    else:
        persist()
    return published, config
