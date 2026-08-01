"""Provider capability helpers.

Capabilities come from the selected adapter or an explicit wire contract.
Unknown model and gateway capabilities remain unknown; this module never
infers support from hostnames or model-name patterns.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any
from backend.config import LLMSettings, normalize_custom_wire_api
from backend.llm.reasoning_effort import reasoning_effort_levels


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str = "unknown"
    model: str = ""
    wire_api: str = ""
    provider_id: str = ""
    base_url: str = ""
    streaming: bool | None = None
    tool_calling: bool | None = None
    parallel_tool_calls: bool | None = None
    json_mode: bool | None = None
    reasoning_effort: bool | None = None
    reasoning_effort_levels: tuple[str, ...] = ()
    vision: bool | None = None
    native_pdf: bool | None = None
    image_generation: bool | None = None
    stateful_continuation: bool | None = None
    prompt_caching: bool | None = None
    native_cache_editing: bool | None = None
    cache_deleted_usage: bool | None = None
    confidence: str = "unknown"
    limitations: tuple[str, ...] = ()
    adapters: tuple["ProviderCapabilities", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["limitations"] = list(self.limitations)
        data["adapters"] = [adapter.to_dict() for adapter in self.adapters]
        return data


@dataclass(frozen=True)
class CapabilityCheck:
    ok: bool
    reason: str = ""
    capability: str = ""
    capabilities: ProviderCapabilities | None = None


def _normalized_base_url(settings: LLMSettings, wire_api: str) -> str:
    base_url = str(settings.base_url or "").strip()
    if not base_url:
        return ""
    if wire_api == "anthropic":
        return base_url
    return base_url.rstrip("/")


def _provider_id(provider: str, settings: LLMSettings, wire_api: str) -> str:
    del settings
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider == "anthropic":
        return "anthropic"
    if normalized_provider == "custom" and wire_api == "anthropic":
        return "custom_anthropic"
    return normalized_provider or "unknown"


def capabilities_from_openai_settings(settings: LLMSettings, *, provider: str = "openai") -> ProviderCapabilities:
    model = str(settings.model or "").strip()
    raw_wire_api = str(settings.wire_api or "chat").strip().lower() or "chat"
    if str(provider or "").strip().lower() == "custom":
        wire_api = normalize_custom_wire_api(str(settings.base_url or ""), raw_wire_api, "chat")
    else:
        wire_api = raw_wire_api
    base_url = _normalized_base_url(settings, wire_api)
    provider_id = _provider_id(provider, settings, wire_api)
    effort_levels = reasoning_effort_levels(
        model,
        wire_api,
        getattr(settings, "reasoning_effort_levels", ()),
    )
    limitations: list[str] = []

    tool_calling: bool | None = None
    if wire_api not in {"chat", "responses"}:
        tool_calling = False
        limitations.append(f"unsupported_openai_wire_api:{wire_api}")
    if bool(settings.responses_stateful_continuation) and wire_api != "responses":
        limitations.append("stateful_continuation_requires_responses_api")
    return ProviderCapabilities(
        provider=provider,
        model=model,
        wire_api=wire_api,
        provider_id=provider_id,
        base_url=base_url,
        streaming=True,
        tool_calling=tool_calling,
        parallel_tool_calls=None,
        json_mode=None,
        reasoning_effort=bool(effort_levels),
        reasoning_effort_levels=effort_levels,
        vision=None,
        native_pdf=None,
        image_generation=None,
        stateful_continuation=wire_api == "responses" and bool(settings.responses_stateful_continuation),
        prompt_caching=None,
        confidence="api_contract" if not limitations else "configured",
        limitations=tuple(limitations),
    )


def capabilities_from_anthropic_adapter(adapter: Any) -> ProviderCapabilities:
    model = str(getattr(adapter, "_model", "") or "").strip()
    base_url = str(getattr(adapter, "_base_url", "") or "").strip()
    cache_editing = bool(
        getattr(adapter, "_cache_editing_beta_header", "")
        and not getattr(adapter, "_cache_editing_disabled_reason", "")
    )
    return ProviderCapabilities(
        provider="anthropic",
        model=model,
        wire_api="anthropic",
        provider_id="anthropic",
        base_url=base_url,
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        json_mode=False,
        reasoning_effort=bool(getattr(adapter, "_thinking_budget", None)),
        reasoning_effort_levels=(),
        vision=None,
        native_pdf=None,
        image_generation=False,
        prompt_caching=True,
        native_cache_editing=cache_editing,
        cache_deleted_usage=cache_editing,
        confidence="known",
    )


def combine_fallback_capabilities(adapters: list[Any]) -> ProviderCapabilities:
    children = tuple(capabilities_for_adapter(adapter) for adapter in adapters if adapter is not None)
    if not children:
        return ProviderCapabilities(provider="fallback", tool_calling=False, confidence="unknown", limitations=("no_adapters",))
    def supported(attribute: str) -> bool | None:
        values = [getattr(child, attribute) for child in children]
        if any(value is True for value in values):
            return True
        if values and all(value is False for value in values):
            return False
        return None

    return ProviderCapabilities(
        provider="fallback",
        model=children[0].model,
        wire_api=children[0].wire_api,
        streaming=supported("streaming"),
        tool_calling=supported("tool_calling"),
        parallel_tool_calls=supported("parallel_tool_calls"),
        json_mode=supported("json_mode"),
        reasoning_effort=supported("reasoning_effort"),
        reasoning_effort_levels=next((child.reasoning_effort_levels for child in children if child.reasoning_effort_levels), ()),
        vision=supported("vision"),
        native_pdf=supported("native_pdf"),
        image_generation=supported("image_generation"),
        stateful_continuation=supported("stateful_continuation"),
        prompt_caching=supported("prompt_caching"),
        native_cache_editing=supported("native_cache_editing"),
        cache_deleted_usage=supported("cache_deleted_usage"),
        confidence="mixed",
        limitations=tuple(
            sorted({limitation for child in children for limitation in child.limitations})
        ),
        adapters=children,
    )


def capabilities_for_adapter(adapter: Any) -> ProviderCapabilities:
    explicit = getattr(adapter, "capabilities", None)
    if isinstance(explicit, ProviderCapabilities):
        return explicit

    adapters = getattr(adapter, "_adapters", None)
    if isinstance(adapters, list):
        return combine_fallback_capabilities(adapters)

    settings = getattr(adapter, "_settings", None)
    if isinstance(settings, LLMSettings):
        provider = str(getattr(settings, "provider", "custom") or "custom")
        return capabilities_from_openai_settings(settings, provider=provider)

    return ProviderCapabilities(provider="unknown", confidence="unknown")


def require_tool_calling(adapter: Any, *, tool_count: int) -> CapabilityCheck:
    capabilities = capabilities_for_adapter(adapter)
    if tool_count <= 0 or capabilities.tool_calling is not False:
        return CapabilityCheck(ok=True, capabilities=capabilities)

    detail = f"{capabilities.provider}/{capabilities.model or 'unknown model'} via {capabilities.wire_api or 'unknown API'}"
    if capabilities.limitations:
        detail = f"{detail} ({', '.join(capabilities.limitations)})"
    return CapabilityCheck(
        ok=False,
        capability="tool_calling",
        reason=f"Provider/model does not support tool calling: {detail}",
        capabilities=capabilities,
    )


def with_limitation(capabilities: ProviderCapabilities, limitation: str) -> ProviderCapabilities:
    if limitation in capabilities.limitations:
        return capabilities
    return replace(capabilities, limitations=(*capabilities.limitations, limitation))
