"""Provider/model capability helpers.

The matrix is intentionally conservative: unknown OpenAI-compatible gateways
stay permissive, while known unsupported model/API combinations fail before the
agent enters a tool-dependent loop.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from backend.config import LLMSettings, normalize_custom_wire_api
from backend.services.llm_provider_helpers import _resolve_openai_provider_id
from backend.llm.reasoning_effort import reasoning_effort_levels


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str = "unknown"
    model: str = ""
    wire_api: str = ""
    provider_id: str = ""
    base_url: str = ""
    streaming: bool = True
    tool_calling: bool = True
    parallel_tool_calls: bool = True
    json_mode: bool = False
    reasoning_effort: bool = False
    reasoning_effort_levels: tuple[str, ...] = ()
    vision: bool = True
    native_pdf: bool = False
    image_generation: bool = False
    stateful_continuation: bool = False
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


_VISION_MODEL_MARKERS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4",
    "claude",
    "gemini",
    "vision",
    "visual",
    "-vl",
    "vl-",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "omni",
    "qvq",
    "glm-4v",
    "glm-4.5v",
    "doubao-vision",
    "doubao-seed-vision",
    "pixtral",
    "llava",
    "internvl",
    "minicpm-v",
    "grok-vision",
)


def _normalized_model(model: str) -> str:
    return str(model or "").strip().replace("_", "-").lower()


def _host(settings: LLMSettings) -> str:
    return urlsplit(str(settings.base_url or "")).netloc.lower()


def _normalized_base_url(settings: LLMSettings, wire_api: str) -> str:
    base_url = str(settings.base_url or "").strip()
    if not base_url:
        return ""
    if wire_api == "anthropic":
        return base_url
    return base_url.rstrip("/")


def _provider_id(provider: str, settings: LLMSettings, wire_api: str) -> str:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider == "anthropic":
        return "anthropic_off"
    if normalized_provider == "custom" and wire_api == "anthropic":
        return "custom_anthropic"
    return _resolve_openai_provider_id(str(settings.base_url or ""))


def _is_image_generation_model(model: str) -> bool:
    normalized = _normalized_model(model)
    return normalized.startswith("gpt-image-") or normalized in {"image-2", "image2"}


def _is_gpt_like_model(model: str) -> bool:
    normalized = _normalized_model(model)
    return (
        normalized.startswith("gpt-")
        or "/gpt-" in normalized
        or normalized.startswith("codex")
        or "/codex" in normalized
    )


def _model_declares_vision_support(model: str) -> bool:
    normalized = _normalized_model(model)
    return any(marker in normalized for marker in _VISION_MODEL_MARKERS)


def _is_known_text_only_image_provider(host: str, model: str) -> bool:
    normalized = _normalized_model(model)
    if "deepseek" in host or "deepseek" in normalized:
        return True
    if ("dashscope" in host or "aliyuncs.com" in host or "qwen" in normalized) and not _model_declares_vision_support(normalized):
        return True
    if "siliconflow" in host and not _model_declares_vision_support(normalized):
        return True
    return False


def capabilities_from_openai_settings(settings: LLMSettings, *, provider: str = "openai") -> ProviderCapabilities:
    model = str(settings.model or "").strip()
    raw_wire_api = str(settings.wire_api or "chat").strip().lower() or "chat"
    if str(provider or "").strip().lower() == "custom":
        wire_api = normalize_custom_wire_api(str(settings.base_url or ""), raw_wire_api, "chat")
    else:
        wire_api = raw_wire_api
    host = _host(settings)
    base_url = _normalized_base_url(settings, wire_api)
    provider_id = _provider_id(provider, settings, wire_api)
    effort_levels = reasoning_effort_levels(
        model,
        wire_api,
        getattr(settings, "reasoning_effort_levels", ()),
    )
    image_generation = wire_api == "responses" and _is_image_generation_model(model)
    limitations: list[str] = []

    tool_calling = wire_api in {"chat", "responses"}
    if _is_image_generation_model(model) and wire_api != "responses":
        tool_calling = False
        limitations.append("image_generation_model_requires_responses_api")

    if wire_api not in {"chat", "responses"}:
        tool_calling = False
        limitations.append(f"unsupported_openai_wire_api:{wire_api}")
    if bool(settings.responses_stateful_continuation) and wire_api != "responses":
        limitations.append("stateful_continuation_requires_responses_api")
    if _is_gpt_like_model(model) and wire_api == "chat":
        limitations.append("gpt_like_chat_completions_no_stateful_continuation")

    if _is_known_text_only_image_provider(host, model):
        vision = False
        limitations.append("known_text_only_image_provider")
    elif host.endswith("api.openai.com") or "api.openai.com" in host:
        vision = True
    else:
        # Unknown compatible gateways are allowed by default. If they reject
        # images, the adapter reports the provider error instead of silently
        # dropping pixels.
        vision = _model_declares_vision_support(model) or wire_api in {"chat", "responses"}

    return ProviderCapabilities(
        provider=provider,
        model=model,
        wire_api=wire_api,
        provider_id=provider_id,
        base_url=base_url,
        streaming=True,
        tool_calling=tool_calling,
        parallel_tool_calls=tool_calling and wire_api == "chat",
        json_mode=wire_api == "chat",
        reasoning_effort=bool(effort_levels),
        reasoning_effort_levels=effort_levels,
        vision=vision,
        native_pdf=wire_api == "responses",
        image_generation=image_generation,
        stateful_continuation=wire_api == "responses" and bool(settings.responses_stateful_continuation),
        confidence="known" if provider == "openai" or limitations else "assumed",
        limitations=tuple(limitations),
    )


def capabilities_from_anthropic_adapter(adapter: Any) -> ProviderCapabilities:
    model = str(getattr(adapter, "_model", "") or "").strip()
    base_url = str(getattr(adapter, "_base_url", "") or "").strip()
    return ProviderCapabilities(
        provider="anthropic",
        model=model,
        wire_api="anthropic",
        provider_id="anthropic_off",
        base_url=base_url,
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        json_mode=False,
        reasoning_effort=bool(getattr(adapter, "_thinking_budget", None)),
        reasoning_effort_levels=(),
        vision=True,
        native_pdf=True,
        image_generation=False,
        confidence="known",
    )


def combine_fallback_capabilities(adapters: list[Any]) -> ProviderCapabilities:
    children = tuple(capabilities_for_adapter(adapter) for adapter in adapters if adapter is not None)
    if not children:
        return ProviderCapabilities(provider="fallback", tool_calling=False, confidence="unknown", limitations=("no_adapters",))
    return ProviderCapabilities(
        provider="fallback",
        model=children[0].model,
        wire_api=children[0].wire_api,
        streaming=any(child.streaming for child in children),
        tool_calling=any(child.tool_calling for child in children),
        parallel_tool_calls=any(child.parallel_tool_calls for child in children),
        json_mode=any(child.json_mode for child in children),
        reasoning_effort=any(child.reasoning_effort for child in children),
        reasoning_effort_levels=next((child.reasoning_effort_levels for child in children if child.reasoning_effort_levels), ()),
        vision=any(child.vision for child in children),
        native_pdf=any(child.native_pdf for child in children),
        image_generation=any(child.image_generation for child in children),
        stateful_continuation=any(child.stateful_continuation for child in children),
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
        provider = "custom"
        host = _host(settings)
        if "api.openai.com" in host:
            provider = "openai"
        return capabilities_from_openai_settings(settings, provider=provider)

    class_name = adapter.__class__.__name__.lower() if adapter is not None else ""
    if "anthropic" in class_name:
        return capabilities_from_anthropic_adapter(adapter)

    return ProviderCapabilities(provider=class_name or "unknown", confidence="unknown")


def require_tool_calling(adapter: Any, *, tool_count: int) -> CapabilityCheck:
    capabilities = capabilities_for_adapter(adapter)
    if tool_count <= 0 or capabilities.tool_calling:
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
