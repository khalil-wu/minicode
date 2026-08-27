"""Pure helper functions for LLM model discovery, normalization, and persistence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.config import (
    get_provider_model_metadata,
    get_llm_settings_payload,
    load_config,
    save_llm_settings,
)
from backend.secret_redaction import redact_secrets
from backend.llm.capabilities import is_gpt_image_model
from backend.llm.proxy_policy import provider_httpx_proxy_kwargs

logger = logging.getLogger(__name__)

_GENERATION_CHECK_MAX_TOKENS = 64
# The Chat probe below bounds itself through httpx's ``timeout=15.0``. The
# Responses probe drives a full adapter whose client has ``timeout=None``, so it
# needs the same ceiling explicitly or the settings dialog spins forever against
# a half-open endpoint.
_GENERATION_CHECK_TIMEOUT_SECONDS = 15.0


# ── Normalization / resolution helpers ──


def _normalize_provider_value(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"openai", "anthropic", "custom"}:
        return normalized
    raise ValueError(
        f"Unknown LLM provider '{str(value or '').strip() or '<empty>'}'. "
        "Choose one of: openai, anthropic, custom."
    )


def _model_id_from_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("id", "model", "name", "value"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _model_created_from_item(item: Any) -> float | None:
    if not isinstance(item, dict):
        return None
    raw_created = item.get("created", item.get("created_at"))
    if isinstance(raw_created, (int, float)):
        return float(raw_created)
    return None


def _candidate_model_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "available_models"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for key in ("result", "response"):
        nested = payload.get(key)
        if isinstance(nested, (dict, list)):
            items = _candidate_model_items(nested)
            if items:
                return items
    return []


def _extract_model_ids(payload: Any) -> list[str]:
    raw_items = _candidate_model_items(payload)
    model_items: list[tuple[str, float | None, int]] = []
    for index, item in enumerate(raw_items):
        model_id = _model_id_from_item(item)
        created_at = _model_created_from_item(item)
        if model_id:
            model_items.append((model_id, created_at, index))

    if any(created_at is not None for _, created_at, _ in model_items):
        model_items.sort(
            key=lambda entry: (entry[1] is not None, entry[1] or 0.0, -entry[2]),
            reverse=True,
        )

    models: list[str] = []
    for model_id, _, _ in model_items:
        if model_id not in models:
            models.append(model_id)
    return models


class ModelDiscovery(list[str]):
    """List-compatible discovery result with provider-declared model metadata."""

    def __init__(
        self,
        models: list[str],
        model_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(models)
        self.model_metadata = model_metadata or {}
        # Compatibility for older callers while the persisted source of truth
        # is the per-model metadata catalog.
        self.reasoning_efforts = {
            model_id: list(metadata.get("reasoning_effort_levels") or [])
            for model_id, metadata in self.model_metadata.items()
            if metadata.get("reasoning_effort_levels")
        }


def _reasoning_effort_from_item(item: Any) -> str:
    if isinstance(item, str):
        candidate = item.strip().lower()
    elif isinstance(item, dict):
        candidate = ""
        for key in (
            "reasoning_effort",
            # MiniCode app-server ReasoningEffortOption uses this exact field.
            "reasoningEffort",
            "effort",
            "id",
            "value",
            "name",
        ):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value.strip().lower()
                break
    else:
        candidate = ""
    # MiniCode's catalog protocol deliberately preserves future/model-defined
    # effort strings. The value is still gated later against this exact
    # model's provider-declared list before MiniCode sends it on the wire.
    return candidate


def _model_reasoning_efforts(item: dict[str, Any]) -> list[str]:
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    raw_levels = (
        item.get("reasoning_effort_levels")
        or item.get("supported_reasoning_efforts")
        or item.get("supported_reasoning_levels")
        or item.get("supportedReasoningEfforts")
        or capabilities.get("reasoning_effort_levels")
        or capabilities.get("supported_reasoning_efforts")
        or capabilities.get("supported_reasoning_levels")
        or capabilities.get("supportedReasoningEfforts")
    )
    if not isinstance(raw_levels, list):
        return []
    levels = [
        effort
        for raw_level in raw_levels
        if (effort := _reasoning_effort_from_item(raw_level))
    ]
    return list(dict.fromkeys(levels))


def _positive_model_integer(item: dict[str, Any], keys: tuple[str, ...]) -> int:
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    for source in (item, capabilities):
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return 0


def _extract_model_discovery(payload: Any) -> ModelDiscovery:
    models = _extract_model_ids(payload)
    model_metadata: dict[str, dict[str, Any]] = {}
    for item in _candidate_model_items(payload):
        if not isinstance(item, dict):
            continue
        model_id = _model_id_from_item(item)
        if not model_id:
            continue
        metadata: dict[str, Any] = {}
        context_window = _positive_model_integer(
            item,
            (
                "context_window",
                "contextWindow",
                "context_length",
                "contextLength",
                # MiniCode model capabilities use this exact field.
                "max_input_tokens",
            ),
        )
        if context_window:
            metadata["context_window"] = context_window
        max_context_window = _positive_model_integer(
            item,
            ("max_context_window", "maxContextWindow"),
        )
        if max_context_window:
            metadata["max_context_window"] = max_context_window
        max_output_tokens = _positive_model_integer(
            item,
            (
                "max_output_tokens",
                "maxOutputTokens",
                "max_tokens",
                "maxTokens",
            ),
        )
        if max_output_tokens:
            metadata["max_output_tokens"] = max_output_tokens
        levels = _model_reasoning_efforts(item)
        if levels:
            metadata["reasoning_effort_levels"] = levels
        for source in (
            item,
            item.get("capabilities")
            if isinstance(item.get("capabilities"), dict)
            else {},
        ):
            default_effort = str(
                source.get("default_reasoning_effort")
                or source.get("defaultReasoningEffort")
                or source.get("default_reasoning_level")
                or source.get("defaultReasoningLevel")
                or ""
            ).strip().lower()
            if default_effort:
                metadata["default_reasoning_effort"] = default_effort
                break
        for source in (
            item,
            item.get("capabilities")
            if isinstance(item.get("capabilities"), dict)
            else {},
        ):
            default_summary = str(
                source.get("default_reasoning_summary")
                or source.get("defaultReasoningSummary")
                or ""
            ).strip().lower()
            if default_summary:
                metadata["default_reasoning_summary"] = default_summary
                break
        if metadata:
            metadata["source"] = "provider"
            model_metadata[model_id] = metadata
    return ModelDiscovery(models, model_metadata)


def _build_openai_models_url(base_url: str) -> str:
    root = base_url.strip().rstrip("/")
    if not root:
        return ""

    parsed = urlsplit(root)
    if not parsed.scheme or not parsed.netloc:
        return f"{root}/models"

    path = parsed.path.rstrip("/")
    if path.endswith("/models"):
        next_path = path
    elif not path:
        next_path = "/v1/models"
    else:
        next_path = f"{path}/models"

    return urlunsplit((parsed.scheme, parsed.netloc, next_path, "", ""))


def _build_openai_endpoint_url(base_url: str, endpoint: str) -> str:
    root = base_url.strip().rstrip("/")
    suffix = endpoint.strip("/")
    if not root or not suffix:
        return ""

    parsed = urlsplit(root)
    if not parsed.scheme or not parsed.netloc:
        return f"{root}/{suffix}"

    path = parsed.path.rstrip("/")
    if path.endswith(f"/{suffix}"):
        next_path = path
    elif not path:
        next_path = f"/v1/{suffix}"
    else:
        next_path = f"{path}/{suffix}"

    return urlunsplit((parsed.scheme, parsed.netloc, next_path, "", ""))


def _merge_models(models: list[str], current_model: str) -> list[str]:
    merged: list[str] = []
    current = current_model.strip()
    for model in models:
        if model and model not in merged:
            merged.append(model)
    if current and current not in merged:
        merged.append(current)
    return merged


def _select_refreshed_model(provider_id: str, models: list[str], current_model: str) -> str:
    del provider_id
    current = str(current_model or "").strip()
    # A configured model is an explicit selection. Keep it visible when live
    # discovery no longer advertises it so the subsequent adapter boundary can
    # report the mismatch instead of silently switching to another model.
    if current:
        return current
    return next((str(item).strip() for item in models if str(item).strip()), "")


def _manual_models_from_payload(payload: Any) -> list[str]:
    available = getattr(payload, "available_models", [])
    return [model.strip() for model in available if isinstance(model, str) and model.strip()]


def _merge_model_sources(*sources: list[str]) -> list[str]:
    merged: list[str] = []
    for source in sources:
        for model in source:
            value = model.strip() if isinstance(model, str) else ""
            if value and value not in merged:
                merged.append(value)
    return merged


def _persist_refreshed_models(
    provider: str,
    models: list[str],
    current_model: str,
    model_metadata: dict[str, dict[str, Any]] | None = None,
) -> Any | None:
    if not models:
        return None

    payload = get_llm_settings_payload()
    provider_key = "custom" if provider == "custom" else provider
    section = payload.get(provider_key)
    if not isinstance(section, dict):
        return None

    next_section = dict(section)
    next_section["available_models"] = _merge_models(models, current_model)
    next_section["models_source"] = "live"
    if current_model:
        next_section["model"] = current_model
    # A successful live refresh replaces the capability catalog.  An empty
    # catalog is meaningful: the provider returned models but did not declare
    # context/reasoning metadata, so stale capabilities must be cleared.
    next_section["model_metadata"] = dict(model_metadata or {})
    selected_metadata = get_provider_model_metadata(next_section, current_model)
    next_section["reasoning_effort_levels"] = selected_metadata[
        "reasoning_effort_levels"
    ]

    # Model discovery edits one saved profile; it must not activate that
    # provider or replay every effective provider section. Replaying the full
    # settings payload used to turn environment-only endpoints (for example a
    # temporary localhost Anthropic proxy) into unrelated saved history cards.
    save_llm_settings({provider_key: next_section})
    return load_config()


# ── HTTP model fetching ──


def _provider_request_headers(
    *,
    api_key: str,
    headers: Mapping[str, str],
    auth_header: bool,
    wire_api: str,
    include_content_type: bool = False,
) -> dict[str, str]:
    request_headers = {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).strip()
    }
    key = api_key.strip()
    if auth_header and not key:
        raise ValueError("auth_header=true requires an API key")
    if key:
        if auth_header or wire_api != "anthropic":
            request_headers["Authorization"] = f"Bearer {key}"
        else:
            request_headers["x-api-key"] = key
    if wire_api == "anthropic":
        request_headers.setdefault("anthropic-version", "2023-06-01")
    if include_content_type:
        request_headers.setdefault("Content-Type", "application/json")
    return request_headers


async def _fetch_openai_compatible_models(
    base_url: str,
    api_key: str,
    *,
    headers: Mapping[str, str],
    auth_header: bool,
    proxy_mode: str = "inherit",
) -> list[str]:
    if not base_url.strip():
        raise ValueError("Provider base URL is required for model discovery")
    models_url = _build_openai_models_url(base_url)
    if not models_url:
        raise ValueError("Provider models endpoint could not be resolved")
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=False,
        **provider_httpx_proxy_kwargs(
            models_url,
            proxy_mode=proxy_mode,
        ),
    ) as client:
        response = await client.get(
            models_url,
            headers=_provider_request_headers(
                api_key=api_key,
                headers=headers,
                auth_header=auth_header,
                wire_api="openai",
            ),
        )
        response.raise_for_status()
        return _extract_model_discovery(response.json())


async def _check_openai_compatible_generation(
    base_url: str,
    api_key: str,
    model: str,
    wire_api: str,
    *,
    headers: Mapping[str, str],
    auth_header: bool,
    proxy_mode: str = "inherit",
) -> None:
    if not base_url.strip() or not model.strip():
        raise ValueError("Provider base URL and model are required")
    normalized_wire = wire_api.strip().lower() or "chat"
    if is_gpt_image_model(model):
        await _check_openai_compatible_image_generation(
            base_url,
            api_key,
            model,
            "1024x1024",
            "",
            proxy_mode=proxy_mode,
            headers=headers,
            auth_header=auth_header,
        )
        return
    endpoint = "responses" if normalized_wire == "responses" else "chat/completions"
    url = _build_openai_endpoint_url(base_url, endpoint)
    if not url:
        return
    if normalized_wire == "responses":
        # Exercise the same MiniCode-aligned Responses transport used by a real
        # MiniCode turn. A bare non-streaming probe can pass model discovery yet
        # be rejected by MiniCode gateways that require prompt_cache_key and
        # client_metadata on generation requests.
        from backend.config import LLMSettings
        from backend.llm.base import LLMMessage, StreamEventType
        from backend.llm.openai_adapter import OpenAIAdapter

        adapter = OpenAIAdapter(
            settings=LLMSettings(
                api_key=api_key.strip(),
                provider="custom",
                base_url=base_url.strip(),
                model=model.strip(),
                wire_api="responses",
                proxy_mode=proxy_mode,
                default_headers=tuple(
                    (str(name), str(value)) for name, value in headers.items()
                ),
                auth_header=auth_header,
            )
        )
        saw_done = False

        async def _drain_responses_probe() -> None:
            nonlocal saw_done
            async for event in adapter.stream_chat(
                [
                    LLMMessage(
                        role="system",
                        content="You are a coding agent. Follow the user request.",
                    ),
                    LLMMessage(role="user", content="Reply with pong."),
                ],
                tools=[],
                metadata={
                    "minicode_source": "provider_check",
                    "minicode_session_id": "minicode-provider-check",
                    "minicode_app_session_id": "minicode-provider-check",
                    "minicode_task_id": "minicode-provider-check",
                    "run_id": "minicode-provider-check",
                },
            ):
                if event.type == StreamEventType.ERROR:
                    raise RuntimeError(event.content or "Responses generation check failed")
                if event.type == StreamEventType.DONE:
                    saw_done = True

        try:
            try:
                await asyncio.wait_for(
                    _drain_responses_probe(),
                    timeout=_GENERATION_CHECK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Responses generation check timed out after "
                    f"{int(_GENERATION_CHECK_TIMEOUT_SECONDS)}s"
                ) from exc
            if not saw_done:
                raise RuntimeError("Responses generation check ended without a terminal event")
        finally:
            await adapter.aclose()
        return

    body: dict[str, Any] = {
        "model": model.strip(),
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        # A one-token probe can be consumed entirely by reasoning and some
        # compatible gateways reject such tiny limits. Keep the check cheap
        # while still exercising a usable generation request.
        "max_tokens": _GENERATION_CHECK_MAX_TOKENS,
    }
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=False,
        **provider_httpx_proxy_kwargs(
            url,
            proxy_mode=proxy_mode,
        ),
    ) as client:
        response = await client.post(
            url,
            headers=_provider_request_headers(
                api_key=api_key,
                headers=headers,
                auth_header=auth_header,
                wire_api="openai",
                include_content_type=True,
            ),
            json=body,
        )
        response.raise_for_status()


async def _check_openai_compatible_image_generation(
    base_url: str,
    api_key: str,
    model: str,
    size: str = "1024x1024",
    quality: str = "",
    *,
    headers: Mapping[str, str],
    auth_header: bool,
    proxy_mode: str = "inherit",
) -> None:
    """Exercise an OpenAI-compatible Images API with a bounded real request.

    This probe is intentionally independent of model-name heuristics: relay
    providers may expose image models under names such as ``flux-*`` or
    ``image-2``.  Connection checks therefore route explicitly to
    ``/images/generations`` whenever the profile declares an image channel.
    """

    if not base_url.strip() or not model.strip():
        raise ValueError("Image provider base URL and model are required")
    url = _build_openai_endpoint_url(base_url, "images/generations")
    if not url:
        return
    body: dict[str, Any] = {
        "model": model.strip(),
        "prompt": "Generate a simple solid blue square for a provider connection check.",
        "n": 1,
        "size": str(size or "1024x1024").strip() or "1024x1024",
        "response_format": "b64_json",
    }
    clean_quality = str(quality or "").strip()
    if clean_quality:
        body["quality"] = clean_quality
    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=False,
        **provider_httpx_proxy_kwargs(
            url,
            proxy_mode=proxy_mode,
        ),
    ) as client:
        for attempt in range(2):
            response = await client.post(
                url,
                headers=_provider_request_headers(
                    api_key=api_key,
                    headers=headers,
                    auth_header=auth_header,
                    wire_api="openai",
                    include_content_type=True,
                ),
                json=body,
            )
            if (
                attempt == 0
                and response.status_code in {400, 422}
                and "response_format" in response.text.lower()
            ):
                body.pop("response_format", None)
                continue
            response.raise_for_status()
            try:
                response_payload = response.json()
            except ValueError as exc:
                raise ValueError(
                    "Images API returned a successful status with invalid JSON."
                ) from exc
            if not _images_response_contains_result(response_payload):
                raise ValueError(
                    "Images API returned a successful status without image data."
                )
            return


def _images_response_contains_result(payload: Any, *, depth: int = 0) -> bool:
    """Return whether a successful Images response contains a usable result.

    OpenAI-compatible relays vary between ``data``, ``images``, ``output``, and
    nested ``image_data`` wrappers.  The connection probe does not decode or
    download the image, but it must not report success for an empty/HTML-shaped
    200 response either.
    """

    if depth > 6:
        return False
    if isinstance(payload, str):
        return bool(payload.strip())
    if isinstance(payload, list):
        return any(
            _images_response_contains_result(item, depth=depth + 1)
            for item in payload
        )
    if not isinstance(payload, dict):
        return False

    for key in ("b64_json", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in ("image_data", "data", "images", "output", "result"):
        if key in payload and _images_response_contains_result(
            payload.get(key),
            depth=depth + 1,
        ):
            return True
    return False


async def _check_anthropic_generation(
    base_url: str,
    api_key: str,
    model: str,
    *,
    headers: Mapping[str, str],
    auth_header: bool,
    proxy_mode: str = "inherit",
) -> None:
    if not model.strip():
        raise ValueError("Provider model is required")
    endpoint = base_url.rstrip("/") if base_url.strip() else "https://api.anthropic.com/v1"
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=False,
        **provider_httpx_proxy_kwargs(
            f"{endpoint}/messages",
            proxy_mode=proxy_mode,
        ),
    ) as client:
        response = await client.post(
            f"{endpoint}/messages",
            headers=_provider_request_headers(
                api_key=api_key,
                headers=headers,
                auth_header=auth_header,
                wire_api="anthropic",
                include_content_type=True,
            ),
            json={
                "model": model.strip(),
                "max_tokens": _GENERATION_CHECK_MAX_TOKENS,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        response.raise_for_status()


async def _fetch_anthropic_models(
    base_url: str,
    api_key: str,
    *,
    headers: Mapping[str, str],
    auth_header: bool,
    proxy_mode: str = "inherit",
) -> list[str]:
    endpoint = base_url.rstrip("/") if base_url.strip() else "https://api.anthropic.com/v1"
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=False,
        **provider_httpx_proxy_kwargs(
            f"{endpoint}/models",
            proxy_mode=proxy_mode,
        ),
    ) as client:
        response = await client.get(
            f"{endpoint}/models",
            headers=_provider_request_headers(
                api_key=api_key,
                headers=headers,
                auth_header=auth_header,
                wire_api="anthropic",
            ),
        )
        response.raise_for_status()
        return _extract_model_discovery(response.json())


# ── Error / status helpers ──


def _status_hint_for_provider(provider_id: str, status_code: int | None, has_api_key: bool) -> str:
    if not has_api_key:
        return (
            "No API key was sent. Keyless endpoints are supported; if this endpoint "
            "requires authentication, configure its key or custom headers."
        )
    if status_code in {401, 403}:
        return "Authentication failed. Check that API key, base URL, and provider match."
    if status_code == 404:
        return "The selected model or API endpoint was not found. Check Base URL, API format, and model."
    if status_code == 429:
        return "The provider is rate limited. Retry later or switch model/key."
    if status_code and status_code >= 500:
        return "The upstream provider is temporarily unavailable. Retry later or switch provider."
    return ""


def _http_error_status(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _is_network_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.RequestError,
            TimeoutError,
            ConnectionError,
        ),
    )


def _http_error_message(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is not None and _http_error_status(exc) is not None:
        text = redact_secrets(str(getattr(response, "text", "") or "").strip())
        if text:
            return text[:500]
        reason_phrase = str(getattr(response, "reason_phrase", "") or "").strip()
        status_code = _http_error_status(exc)
        return f"HTTP {status_code}{f' {reason_phrase}' if reason_phrase else ''}"
    return redact_secrets(str(exc))[:500]
