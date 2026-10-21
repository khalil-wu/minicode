"""MiniCode-owned provider/model data contracts.

Transport implementations may implement these contracts, but the harness core
does not depend on a particular provider SDK or external agent runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import math
from typing import Any, Mapping
from urllib.parse import urlparse


TokenNumber = int | float
MAX_SAFE_INTEGER = 9_007_199_254_740_991
TOOL_MODES = frozenset({"direct", "code_mode", "code_mode_only"})


def normalize_tool_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in TOOL_MODES else ""


def hosted_web_search_supported(*, wire_api: str, base_url: str, declared: bool | None) -> bool:
    """Resolve the same hosted-search wire contract for runtime and idle catalogs."""
    if wire_api not in {"responses", "anthropic"}:
        return False
    if declared is not None:
        return declared
    if wire_api == "anthropic":
        return urlparse(base_url or "https://api.anthropic.com").hostname == "api.anthropic.com"
    return urlparse(base_url or "https://api.openai.com/v1").hostname in {"api.openai.com", "chatgpt.com"}


def normalize_model_limit(value: Any) -> int:
    """Keep exact positive model limits that survive the frontend number wire."""
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdecimal():
            return 0
        text = text.lstrip("0") or "0"
        if len(text) > 16:
            return 0
        value = int(text)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 < value <= MAX_SAFE_INTEGER else 0
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value) if 0 < value <= MAX_SAFE_INTEGER else 0
    return 0


class ProviderRegistrationError(ValueError):
    """A provider capability could not be registered safely."""


class UnsupportedProviderCapabilityError(ProviderRegistrationError):
    """A provider advertised a capability MiniCode does not support."""


_MODEL_KNOWN_KEYS = frozenset(
    {
        "provider", "id", "name", "api", "baseUrl", "base_url", "reasoning",
        "thinkingLevelMap", "thinking_level_map", "input", "cost",
        "contextWindow", "context_window", "maxContextWindow",
        "max_context_window", "maxTokens", "max_tokens", "headers",
        "supports_custom_tools", "supports_hosted_web_search", "model_instructions", "responses_websocket", "native_compaction",
        "parallel_tool_calls", "comp_hash",
        "tool_mode",
        "supports_experimental_context",
        "truncation_policy", "supports_image_detail_original",
        "supports_search_tool",
        "source_model_metadata", "used_fallback_model_metadata",
    }
)


@dataclass(frozen=True)
class ModelMetadataSource:
    reference_commit: str
    catalog_file: str


@dataclass(frozen=True)
class ModelDefinition:
    provider: str
    id: str
    name: str
    api: str
    base_url: str
    reasoning: bool = False
    thinking_level_map: Mapping[str, Any] | None = None
    # No declaration is capability-unknown; only an explicit text-only list
    # may suppress image input.
    input: tuple[str, ...] = ()
    cost: Mapping[str, Any] = field(default_factory=dict)
    context_window: TokenNumber = 0
    context_window_source: str = ""
    context_window_verified: bool = False
    max_context_window: TokenNumber = 0
    max_context_window_source: str = ""
    max_context_window_verified: bool = False
    max_tokens: TokenNumber = 0
    max_output_tokens: TokenNumber = 0
    max_output_tokens_source: str = ""
    max_output_tokens_verified: bool = False
    reasoning_effort_levels: tuple[str, ...] = ()
    default_reasoning_effort: str = ""
    default_reasoning_summary: str = ""
    supports_custom_tools: bool = False
    responses_websocket: bool = False
    native_compaction: bool | None = None
    comp_hash: str | None = None
    model_instructions: str = ""
    parallel_tool_calls: bool | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)
    tool_mode: str = ""
    supports_hosted_web_search: bool | None = None
    supports_experimental_context: bool = False
    truncation_policy: Mapping[str, Any] | None = None
    supports_image_detail_original: bool = False
    supports_search_tool: bool = False
    # Host-owned catalog provenance. It is neither accepted from a model's
    # extension dictionary nor projected back into that dictionary.
    source_model_metadata: ModelMetadataSource | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.supports_experimental_context, bool):
            raise ProviderRegistrationError("supports_experimental_context must be a boolean")
        if self.extra.get("model_messages") is not None and not isinstance(self.extra["model_messages"], Mapping):
            raise ProviderRegistrationError("model_messages must be an object")
        object.__setattr__(self, "extra", deepcopy({key: value for key, value in self.extra.items()
            if key not in {"source_model_metadata", "used_fallback_model_metadata"}}))
        if self.truncation_policy is not None:
            mode, limit = self.truncation_policy["mode"], self.truncation_policy["limit"]
            if mode not in {"bytes", "tokens"} or type(limit) is not int or limit < 0:
                raise ProviderRegistrationError("truncation_policy must contain a bytes/tokens mode and a nonnegative integer limit")
            object.__setattr__(self, "truncation_policy", deepcopy(dict(self.truncation_policy)))
        if not isinstance(self.supports_image_detail_original, bool):
            raise ProviderRegistrationError("supports_image_detail_original must be a boolean")
        if not isinstance(self.supports_search_tool, bool):
            raise ProviderRegistrationError("supports_search_tool must be a boolean")

    @property
    def model_messages(self) -> Mapping[str, Any] | None:
        return self.extra.get("model_messages")

    @property
    def model_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            **deepcopy(dict(self.extra)), "provider": self.provider, "id": self.id,
            "name": self.name, "api": self.api, "baseUrl": self.base_url,
            "reasoning": self.reasoning, "input": list(self.input),
            "cost": deepcopy(dict(self.cost)), "contextWindow": self.context_window,
            "maxTokens": self.max_tokens,
        }
        if self.thinking_level_map is not None:
            result["thinkingLevelMap"] = dict(self.thinking_level_map)
        if self.headers:
            result["headers"] = dict(self.headers)
        if self.supports_custom_tools:
            result["supports_custom_tools"] = True
        if self.supports_hosted_web_search is not None:
            result["supports_hosted_web_search"] = self.supports_hosted_web_search
        if self.responses_websocket:
            result["responses_websocket"] = True
        if self.native_compaction is not None:
            result["native_compaction"] = self.native_compaction
        if self.comp_hash is not None:
            result["comp_hash"] = self.comp_hash
        if self.model_instructions:
            result["model_instructions"] = self.model_instructions
        if self.parallel_tool_calls is not None:
            result["parallel_tool_calls"] = self.parallel_tool_calls
        if self.tool_mode:
            result["tool_mode"] = self.tool_mode
        result["supports_experimental_context"] = self.supports_experimental_context
        if self.truncation_policy is not None:
            result["truncation_policy"] = deepcopy(dict(self.truncation_policy))
        result["supports_image_detail_original"] = self.supports_image_detail_original
        result["supports_search_tool"] = self.supports_search_tool
        return result

    def to_public_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        for key in self.extra:
            if str(key) not in _MODEL_KNOWN_KEYS:
                result.pop(str(key), None)
        return result

    def to_extension_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "id": self.id,
            "name": self.name,
            "api": self.api,
            "base_url": self.base_url,
            "reasoning": self.reasoning,
            "input": list(self.input),
            "cost": deepcopy(dict(self.cost)),
            "context_window": self.context_window,
            "max_context_window": self.max_context_window,
            "max_tokens": self.max_tokens,
            "headers": dict(self.headers),
        }
        if self.thinking_level_map is not None:
            result["thinking_level_map"] = dict(self.thinking_level_map)
        result["supports_custom_tools"] = self.supports_custom_tools
        result["supports_hosted_web_search"] = self.supports_hosted_web_search
        result["responses_websocket"] = self.responses_websocket
        result["native_compaction"] = self.native_compaction
        result["comp_hash"] = self.comp_hash
        result["model_instructions"] = self.model_instructions
        result["parallel_tool_calls"] = self.parallel_tool_calls
        result["tool_mode"] = self.tool_mode
        result["supports_experimental_context"] = self.supports_experimental_context
        if self.truncation_policy is not None:
            result["truncation_policy"] = deepcopy(dict(self.truncation_policy))
        result["supports_image_detail_original"] = self.supports_image_detail_original
        result["supports_search_tool"] = self.supports_search_tool
        if self.model_messages is not None:
            result["model_messages"] = deepcopy(dict(self.model_messages))
        return result


@dataclass(frozen=True, slots=True)
class TokenBudgetAuthContext:
    auth_mode: str | None = None
    account_plan_type: str | None = None
    supports_codex_backend_routes: bool = False
    requires_openai_auth: bool = False
    has_env_key: bool = False
    has_experimental_bearer_token: bool = False
    has_auth: bool = False
    has_aws: bool = False


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    name: str
    base_url: str
    models: tuple[ModelDefinition, ...]
    configured: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "baseUrl": self.base_url,
            "models": [model.to_public_dict() for model in self.models],
            "configured": self.configured,
            "source": self.source,
        }


@dataclass(frozen=True)
class ProviderAdapterSpec:
    provider_id: str
    model_id: str
    api: str
    api_key: str
    base_url: str
    headers: Mapping[str, str]
    auth_header: bool
    max_tokens: TokenNumber
    env: Mapping[str, str] = field(default_factory=dict)
    proxy_mode: str = "inherit"
    model: ModelDefinition | None = None
    small_fast_model: str = ""
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    thinking_budget: int | None = None
    prompt_cache_retention: str = ""
    reasoning_effort_levels: tuple[str, ...] = ()
    context_window: TokenNumber = 0
    context_window_source: str = ""
    context_window_verified: bool = False
    max_context_window: TokenNumber = 0
    max_context_window_source: str = ""
    max_context_window_verified: bool = False
    max_output_tokens: TokenNumber = 0
    max_output_tokens_source: str = ""
    max_output_tokens_verified: bool = False
    default_reasoning_effort: str = ""
    default_reasoning_summary: str = ""
    extension_defined: bool = False
    supports_custom_tools: bool = False
    responses_websocket: bool = False
    native_compaction: bool | None = None
    comp_hash: str | None = None
    model_instructions: str = ""
    parallel_tool_calls: bool | None = None
    tool_mode: str = ""
    supports_hosted_web_search: bool | None = None
    supports_search_tool: bool = False
    aws: Mapping[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ReasoningPolicy:
    """Canonical reasoning selection plus its provider wire projection."""

    level: str
    wire_level: str = ""
    wire_levels: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()


__all__ = [
    "ModelDefinition", "ProviderAdapterSpec", "ProviderDefinition",
    "ReasoningPolicy", "TokenBudgetAuthContext",
    "ProviderRegistrationError", "TokenNumber", "MAX_SAFE_INTEGER", "normalize_model_limit",
    "UnsupportedProviderCapabilityError",
]
