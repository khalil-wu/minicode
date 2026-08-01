"""Pure helper functions for LLM model discovery, normalization, and persistence."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.config import (
    get_llm_settings_payload,
    load_config,
    save_llm_settings,
)

logger = logging.getLogger(__name__)


# ── Normalization / resolution helpers ──


def _normalize_provider_value(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "openai":
        return "openai"
    if normalized == "anthropic":
        return "anthropic"
    if normalized == "custom":
        return "custom"
    # Provider identity is a closed contract. Unknown values must not silently
    # become first-party OpenAI and must use the explicit custom transport.
    return "custom"


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
    """List-compatible discovery result with optional provider-declared capabilities."""

    def __init__(self, models: list[str], reasoning_efforts: dict[str, list[str]] | None = None) -> None:
        super().__init__(models)
        self.reasoning_efforts = reasoning_efforts or {}


def _extract_model_discovery(payload: Any) -> ModelDiscovery:
    models = _extract_model_ids(payload)
    efforts: dict[str, list[str]] = {}
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    for item in _candidate_model_items(payload):
        if not isinstance(item, dict):
            continue
        model_id = _model_id_from_item(item)
        capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        raw_levels = (
            item.get("reasoning_effort_levels")
            or item.get("supported_reasoning_efforts")
            or capabilities.get("reasoning_effort_levels")
            or capabilities.get("supported_reasoning_efforts")
        )
        if not model_id or not isinstance(raw_levels, list):
            continue
        levels = [str(level).strip().lower() for level in raw_levels if str(level).strip().lower() in allowed]
        if levels:
            efforts[model_id] = list(dict.fromkeys(levels))
    return ModelDiscovery(models, efforts)


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
    current = current_model.strip()
    if models:
        return current if current in models else models[0]
    return current


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
    reasoning_effort_levels: list[str] | None = None,
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
    if reasoning_effort_levels:
        next_section["reasoning_effort_levels"] = reasoning_effort_levels

    save_payload = {
        "provider": provider,
        "openai": payload.get("openai", {}),
        "anthropic": payload.get("anthropic", {}),
        "custom": payload.get("custom", {}),
    }
    save_payload[provider_key] = next_section
    save_llm_settings(save_payload)
    return load_config()


# ── HTTP model fetching ──


async def _fetch_openai_compatible_models(base_url: str, api_key: str) -> list[str]:
    if not base_url.strip() or not api_key.strip():
        return []
    models_url = _build_openai_models_url(base_url)
    if not models_url:
        return []
    proxy_url = os.getenv("LLM_PROXY_URL", "").strip() or os.getenv("MINICODE_LLM_PROXY_URL", "").strip() or os.getenv("OPENAI_PROXY_URL", "").strip()
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        proxy=proxy_url or None,
        trust_env=not bool(proxy_url),
    ) as client:
        response = await client.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
        )
        response.raise_for_status()
        return _extract_model_discovery(response.json())


async def _check_openai_compatible_generation(
    base_url: str,
    api_key: str,
    model: str,
    wire_api: str,
) -> None:
    if not base_url.strip() or not api_key.strip() or not model.strip():
        return
    normalized_wire = wire_api.strip().lower() or "chat"
    endpoint = "responses" if normalized_wire == "responses" else "chat/completions"
    url = _build_openai_endpoint_url(base_url, endpoint)
    if not url:
        return
    if normalized_wire == "responses":
        body: dict[str, Any] = {
            "model": model.strip(),
            "input": "ping",
            "stream": False,
            "store": False,
            "max_output_tokens": 16,
        }
    else:
        body = {
            "model": model.strip(),
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 1,
        }
    proxy_url = os.getenv("LLM_PROXY_URL", "").strip() or os.getenv("MINICODE_LLM_PROXY_URL", "").strip() or os.getenv("OPENAI_PROXY_URL", "").strip()
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        proxy=proxy_url or None,
        trust_env=not bool(proxy_url),
    ) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()


async def _check_anthropic_generation(base_url: str, api_key: str, model: str) -> None:
    if not api_key.strip() or not model.strip():
        return
    endpoint = base_url.rstrip("/") if base_url.strip() else "https://api.anthropic.com/v1"
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    proxy_url = os.getenv("LLM_PROXY_URL", "").strip() or os.getenv("MINICODE_LLM_PROXY_URL", "").strip()
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        proxy=proxy_url or None,
        trust_env=not bool(proxy_url),
    ) as client:
        response = await client.post(
            f"{endpoint}/messages",
            headers={
                "x-api-key": api_key.strip(),
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model.strip(),
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        response.raise_for_status()


async def _fetch_anthropic_models(base_url: str, api_key: str) -> list[str]:
    if not api_key.strip():
        return []
    endpoint = base_url.rstrip("/") if base_url.strip() else "https://api.anthropic.com/v1"
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        response = await client.get(
            f"{endpoint}/models",
            headers={
                "x-api-key": api_key.strip(),
                "anthropic-version": "2023-06-01",
            },
        )
        response.raise_for_status()
        return _extract_model_ids(response.json())


# ── Error / status helpers ──


def _status_hint_for_provider(provider_id: str, status_code: int | None, has_api_key: bool) -> str:
    if not has_api_key:
        return "No API key is available for the current provider. Save a key for this provider or switch to one with a configured key."
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
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def _http_error_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        text = exc.response.text.strip()
        if text:
            return text[:500]
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase}"
    return str(exc)
