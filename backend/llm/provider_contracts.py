"""MiniCode-owned provider/model data contracts.

Transport implementations may implement these contracts, but the harness core
does not depend on a particular provider SDK or external agent runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


TokenNumber = int | float


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
    }
)


@dataclass(frozen=True)
class ModelDefinition:
    provider: str
    id: str
    name: str
    api: str
    base_url: str
    reasoning: bool = False
    thinking_level_map: Mapping[str, Any] | None = None
    input: tuple[str, ...] = ("text",)
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
    headers: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def model_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            **dict(self.extra), "provider": self.provider, "id": self.id,
            "name": self.name, "api": self.api, "baseUrl": self.base_url,
            "reasoning": self.reasoning, "input": list(self.input),
            "cost": dict(self.cost), "contextWindow": self.context_window,
            "maxTokens": self.max_tokens,
        }
        if self.thinking_level_map is not None:
            result["thinkingLevelMap"] = dict(self.thinking_level_map)
        if self.headers:
            result["headers"] = dict(self.headers)
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
            "cost": dict(self.cost),
            "context_window": self.context_window,
            "max_context_window": self.max_context_window,
            "max_tokens": self.max_tokens,
            "headers": dict(self.headers),
        }
        if self.thinking_level_map is not None:
            result["thinking_level_map"] = dict(self.thinking_level_map)
        return result


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


@dataclass(frozen=True)
class ReasoningPolicy:
    """Canonical reasoning selection plus its provider wire projection."""

    level: str
    wire_level: str = ""
    wire_levels: tuple[str, ...] = ()


__all__ = [
    "ModelDefinition", "ProviderAdapterSpec", "ProviderDefinition",
    "ReasoningPolicy",
    "ProviderRegistrationError", "TokenNumber",
    "UnsupportedProviderCapabilityError",
]
