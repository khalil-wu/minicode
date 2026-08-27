"""Provider capability helpers.

Capabilities come from the selected adapter or an explicit wire contract.
Unknown model and gateway capabilities remain unknown.  The one model-family
classification kept here is the canonical ``gpt-image-*`` contract used by
Codex's image backend and Pi's separate image-model registry: those are
dedicated image models, not coding models with function-calling tools.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any
from backend.config import LLMSettings, normalize_custom_wire_api
from backend.llm.reasoning_effort import normalize_reasoning_effort, reasoning_effort_levels


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
    configured_reasoning_effort: str = ""
    effective_reasoning_effort: str = ""
    reasoning_effort_supported: bool | None = None
    context_window: int | float = 0
    context_window_source: str = ""
    context_window_verified: bool = False
    max_context_window: int | float = 0
    max_context_window_source: str = ""
    max_context_window_verified: bool = False
    max_output_tokens: int | float = 0
    max_output_tokens_source: str = ""
    max_output_tokens_verified: bool = False
    default_reasoning_effort: str = ""
    default_reasoning_summary: str = ""
    vision: bool | None = None
    native_pdf: bool | None = None
    image_generation: bool | None = None
    prompt_caching: bool | None = None
    native_cache_editing: bool | None = None
    cache_deleted_usage: bool | None = None
    confidence: str = "unknown"
    limitations: tuple[str, ...] = ()
    adapters: tuple["ProviderCapabilities", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # ``asdict`` preserves tuple containers.  Runtime capability snapshots
        # cross the WebSocket/JSON boundary, so expose every repeated field as
        # an actual JSON array instead of relying on framework-specific tuple
        # coercion.  The stricter session restore validator must see the same
        # shape that browsers receive.
        data["reasoning_effort_levels"] = list(self.reasoning_effort_levels)
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


def is_gpt_image_model(model: str) -> bool:
    """Return whether ``model`` is a canonical GPT Image model id.

    MiniCode routes the dedicated image API through the ``gpt-image-*`` model
    family. Provider-qualified names such as ``openai/gpt-image-2`` are matched
    by their terminal model id; unrelated model names remain capability-unknown.
    """

    terminal_model_id = str(model or "").strip().lower().rsplit("/", 1)[-1]
    return terminal_model_id.startswith("gpt-image-")


def capabilities_from_openai_settings(
    settings: LLMSettings, *, provider: str = "openai"
) -> ProviderCapabilities:
    model = str(settings.model or "").strip()
    raw_wire_api = str(settings.wire_api or "chat").strip().lower() or "chat"
    if str(provider or "").strip().lower() == "custom":
        wire_api = normalize_custom_wire_api(
            str(settings.base_url or ""), raw_wire_api, "chat"
        )
    else:
        wire_api = raw_wire_api
    base_url = _normalized_base_url(settings, wire_api)
    provider_id = _provider_id(provider, settings, wire_api)
    effort_levels = reasoning_effort_levels(
        model,
        wire_api,
        getattr(settings, "reasoning_effort_levels", ()),
    )
    configured_effort = str(settings.reasoning_effort or "").strip().lower()
    effective_effort = normalize_reasoning_effort(
        model,
        wire_api,
        configured_effort,
        effort_levels,
        getattr(settings, "default_reasoning_effort", ""),
    )
    dedicated_image_model = is_gpt_image_model(model)
    limitations: list[str] = []

    tool_calling: bool | None = False if dedicated_image_model else None
    if dedicated_image_model:
        limitations.append("dedicated_image_model_uses_images_api")
    if wire_api not in {"chat", "responses"}:
        tool_calling = False
        limitations.append(f"unsupported_openai_wire_api:{wire_api}")
    if (
        str(getattr(settings, "context_window_source", "") or "") == "fallback"
        and not bool(getattr(settings, "context_window_verified", False))
    ):
        limitations.append("context_window_fallback_unverified")
    return ProviderCapabilities(
        provider=provider,
        model=model,
        wire_api=wire_api,
        provider_id=provider_id,
        base_url=base_url,
        streaming=False if dedicated_image_model else True,
        tool_calling=tool_calling,
        parallel_tool_calls=False if dedicated_image_model else None,
        json_mode=False if dedicated_image_model else None,
        reasoning_effort=False if dedicated_image_model else bool(effort_levels),
        reasoning_effort_levels=() if dedicated_image_model else effort_levels,
        configured_reasoning_effort=configured_effort,
        effective_reasoning_effort="" if dedicated_image_model else effective_effort,
        reasoning_effort_supported=(
            False if dedicated_image_model else bool(effort_levels)
        ),
        context_window=max(0, getattr(settings, "context_window", 0) or 0),
        context_window_source=str(
            getattr(settings, "context_window_source", "") or ""
        ),
        context_window_verified=bool(
            getattr(settings, "context_window_verified", False)
        ),
        max_context_window=max(
            0,
            getattr(settings, "max_context_window", 0) or 0,
        ),
        max_context_window_source=str(
            getattr(settings, "max_context_window_source", "") or ""
        ),
        max_context_window_verified=bool(
            getattr(settings, "max_context_window_verified", False)
        ),
        max_output_tokens=max(
            0,
            getattr(settings, "max_output_tokens", 0) or 0,
        ),
        max_output_tokens_source=str(
            getattr(settings, "max_output_tokens_source", "") or ""
        ),
        max_output_tokens_verified=bool(
            getattr(settings, "max_output_tokens_verified", False)
        ),
        default_reasoning_effort=str(
            getattr(settings, "default_reasoning_effort", "") or ""
        ),
        default_reasoning_summary=str(
            getattr(settings, "default_reasoning_summary", "") or ""
        ),
        vision=False if dedicated_image_model else None,
        native_pdf=False if dedicated_image_model else None,
        image_generation=True if dedicated_image_model else None,
        prompt_caching=False if dedicated_image_model else None,
        confidence=(
            "known"
            if dedicated_image_model
            else "api_contract" if not limitations else "configured"
        ),
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
        provider=str(getattr(adapter, "_provider_id", "anthropic") or "anthropic"),
        model=model,
        wire_api="anthropic",
        provider_id=str(getattr(adapter, "_provider_id", "anthropic") or "anthropic"),
        base_url=base_url,
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        json_mode=False,
        reasoning_effort=bool(getattr(adapter, "_thinking_budget", None)),
        reasoning_effort_levels=(),
        configured_reasoning_effort="",
        effective_reasoning_effort="",
        reasoning_effort_supported=False,
        context_window=max(0, getattr(adapter, "_context_window", 0) or 0),
        context_window_source=(
            "provider" if getattr(adapter, "_context_window", 0) else ""
        ),
        context_window_verified=bool(getattr(adapter, "_context_window", 0)),
        max_context_window=max(
            0,
            getattr(adapter, "_context_window", 0) or 0,
        ),
        max_context_window_source=(
            "provider" if getattr(adapter, "_context_window", 0) else ""
        ),
        max_context_window_verified=bool(getattr(adapter, "_context_window", 0)),
        max_output_tokens=max(0, getattr(adapter, "_max_tokens", 0) or 0),
        max_output_tokens_source="provider",
        max_output_tokens_verified=True,
        vision=None,
        native_pdf=None,
        image_generation=False,
        prompt_caching=True,
        native_cache_editing=cache_editing,
        cache_deleted_usage=cache_editing,
        confidence="known",
    )


def capabilities_for_adapter(adapter: Any) -> ProviderCapabilities:
    explicit = getattr(adapter, "capabilities", None)
    if isinstance(explicit, ProviderCapabilities):
        return explicit

    settings = getattr(adapter, "_settings", None)
    if isinstance(settings, LLMSettings):
        provider = str(getattr(settings, "provider", "custom") or "custom")
        return capabilities_from_openai_settings(settings, provider=provider)

    model_definition = getattr(adapter, "_model", None)
    if model_definition is not None and not isinstance(model_definition, str):
        model_id = str(getattr(model_definition, "id", "") or "")
        provider = str(getattr(model_definition, "provider", "") or "")
        api = str(getattr(model_definition, "api", "") or "")
        return ProviderCapabilities(
            provider=provider or "custom",
            provider_id=provider or "custom",
            model=model_id,
            wire_api=api,
            streaming=True,
            tool_calling=True,
            reasoning_effort=bool(getattr(model_definition, "reasoning", False)),
            reasoning_effort_levels=tuple(
                getattr(model_definition, "reasoning_effort_levels", ()) or ()
            ),
            context_window=max(
                0,
                getattr(model_definition, "context_window", 0) or 0,
            ),
            context_window_source=str(
                getattr(model_definition, "context_window_source", "") or ""
            ),
            context_window_verified=bool(
                getattr(model_definition, "context_window_verified", False)
            ),
            max_context_window=max(
                0,
                getattr(model_definition, "max_context_window", 0) or 0,
            ),
            max_context_window_source=str(
                getattr(model_definition, "max_context_window_source", "") or ""
            ),
            max_context_window_verified=bool(
                getattr(model_definition, "max_context_window_verified", False)
            ),
            max_output_tokens=max(
                0,
                getattr(model_definition, "max_output_tokens", 0)
                or getattr(model_definition, "max_tokens", 0)
                or 0,
            ),
            max_output_tokens_source=str(
                getattr(model_definition, "max_output_tokens_source", "") or ""
            ),
            max_output_tokens_verified=bool(
                getattr(model_definition, "max_output_tokens_verified", False)
            ),
            default_reasoning_effort=str(
                getattr(model_definition, "default_reasoning_effort", "") or ""
            ),
            default_reasoning_summary=str(
                getattr(model_definition, "default_reasoning_summary", "") or ""
            ),
            confidence="configured",
        )

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


def with_limitation(
    capabilities: ProviderCapabilities, limitation: str
) -> ProviderCapabilities:
    if limitation in capabilities.limitations:
        return capabilities
    return replace(capabilities, limitations=(*capabilities.limitations, limitation))
