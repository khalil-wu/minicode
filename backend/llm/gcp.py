"""Vertex routing, static model capabilities and command authentication."""
from __future__ import annotations

from dataclasses import replace
from contextlib import asynccontextmanager
from copy import deepcopy
import os
from pathlib import Path

from backend.llm.command_auth import CommandBearerAuth, command_auth_config


GCP_MODELS = ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna")


def provider_config(settings) -> dict:
    return {**settings.get("llm", {}).get("gcp", {}), **settings.get("model_providers", {}).get("gcp", {})}


def configured(config) -> bool:
    return "base_url" in config or "project_id" in config or bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())


def vertex_base_url(config) -> str:
    if "base_url" in config:
        url = config["base_url"].strip()
        if not url:
            raise ValueError("model_providers.gcp.base_url must be non-empty")
        return url
    project = config.get("project_id", os.getenv("GOOGLE_CLOUD_PROJECT", "")).strip()
    location = config.get("location", os.getenv("GOOGLE_CLOUD_LOCATION", "").strip() or "global").strip()
    if not project and "project_id" not in config:
        raise ValueError("Set model_providers.gcp.project_id or GOOGLE_CLOUD_PROJECT, or configure model_providers.gcp.base_url")
    for field, env, value in (("project_id", "GOOGLE_CLOUD_PROJECT", project), ("location", "GOOGLE_CLOUD_LOCATION", location)):
        if not value or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in value):
            name = f"model_providers.gcp.{field}" if field in config else env
            raise ValueError(f"{name} must be non-empty and contain only ASCII letters, digits, and hyphens")
    return f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/openai/v1"


def gcp_command_config(config, *, cwd: Path) -> dict:
    value = config.get("auth")
    if value is None:
        value = {"command": "gcloud.cmd" if os.name == "nt" else "gcloud",
            "args": ["auth", "application-default", "print-access-token"],
            "timeout_ms": 30000, "refresh_interval_ms": 3000000}
    return command_auth_config(value, cwd=cwd)


def vertex_model(model):
    return replace(model, api="openai-responses", tool_mode="direct", supports_custom_tools=False,
        responses_websocket=False, native_compaction=True, supports_hosted_web_search=False,
        default_reasoning_summary="none", extra={**model.extra, "use_responses_lite": False,
            "apply_patch_tool_type": None, "supports_reasoning_summary_parameter": False})


def base_provider(settings) -> dict:
    from backend.llm.model_runtime_definitions import _base_model

    config = provider_config(settings)
    url = vertex_base_url(config) if configured(config) else ""
    models = tuple(vertex_model(_base_model("gcp", model, api="openai-responses", base_url=url,
        max_tokens=config.get("max_tokens", 0), settings=config)) for model in GCP_MODELS)
    return {"name": "Google Cloud Vertex AI", "base_url": url, "api": "openai-responses",
        "models": models, "small_fast_model": "gpt-6-luna", "responses_reasoning_summary": "off",
        "headers": dict(config.get("headers", {})),
        "reasoning_effort": config.get("reasoning_effort", ""), "proxy_mode": config.get("proxy_mode", "inherit"),
        "gcp": config}


class GcpAuth:
    def __init__(self, config, *, cwd, network_policy=None):
        self.config = deepcopy(config)
        self.request_auth = CommandBearerAuth(gcp_command_config(config, cwd=cwd), network_policy=network_policy)

    def check(self, value):
        return {"type": "api_key", "source": "command"}

    async def resolve(self, value):
        url = vertex_base_url(self.config)
        token = await self.request_auth.token(force=value.get("force", False))
        return {"auth": {"api_key": token, "base_url": url}, "source": "command"}


def llm_settings(settings, *, cwd: Path, model_override=None):
    from backend.config_helpers import LLMSettings

    config = provider_config(settings)
    provider = base_provider(settings)
    model_id = model_override or config.get("model", settings.get("model", ""))
    model = next((model for model in provider["models"] if model.id == model_id), None)
    if model is None and model_id:
        raise ValueError(f"Unknown Vertex model '{model_id}'")
    metadata = _model_settings(model) if model is not None else {}
    return LLMSettings(**metadata, provider="gcp", api_key="", base_url=vertex_base_url(config), model=model_id,
        wire_api="responses", command_auth=gcp_command_config(config, cwd=cwd),
        small_fast_model="gpt-6-luna", reasoning_effort=config.get("reasoning_effort", ""),
        proxy_mode=config.get("proxy_mode", "inherit"), input_modalities=model.input if model else (),
        default_headers=tuple(config.get("headers", {}).items()))


def _model_settings(model):
    from backend.config_helpers import LLMSettings
    return {key: value for key, value in model.__dict__.items()
        if key in LLMSettings.__dataclass_fields__ and key not in {"provider", "base_url"}}


@asynccontextmanager
async def memory_consolidation_adapter(llm):
    from backend.llm.openai_adapter import OpenAIAdapter

    if not isinstance(llm, OpenAIAdapter) or llm._settings.provider != "gcp":
        yield llm
        return
    provider = base_provider({"model_providers": {"gcp": {"base_url": llm._settings.base_url}}})
    model = next(model for model in provider["models"] if model.id == "gpt-6.1-sol")
    settings = replace(llm._settings, **_model_settings(model), model=model.id, input_modalities=model.input)
    adapter = OpenAIAdapter(settings, request_auth=llm._provider_request_auth, network_policy=llm._application_network)
    try:
        yield adapter
    finally:
        await adapter.aclose()
