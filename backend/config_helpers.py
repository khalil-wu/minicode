"""Shared configuration helpers and provider settings readers.

Extracted from ``backend/config.py`` so provider resolution, settings-file IO
and coercion helpers are reusable without importing the full config surface.
"""

from __future__ import annotations

from backend.atomic_io import (
    atomic_write_text,
    file_mutation_locks,
)
from backend.feature_flags import coerce_feature_bool
from backend.llm.model_catalog import responses_model_catalog_entry
from backend.llm.proxy_policy import normalize_provider_proxy_mode
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import (
    Any,
    Mapping,
)
from urllib.parse import (
    urlsplit,
    urlunsplit,
)
import json
import os
import threading


# ── Runtime paths and settings file ─────────────────────────────
from backend.atomic_io import file_mutation_locks

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = Path(os.environ.get("MINICODE_STATE_ROOT") or PROJECT_ROOT).expanduser().resolve()
DATA_ROOT = STATE_ROOT / "data"
SETTINGS_FILE = STATE_ROOT / "settings.json"
_SETTINGS_WRITE_LOCK = threading.RLock()

# MiniCode's Messages API requires max_tokens and uses this capped default;
# OpenAI Responses/Chat APIs leave the field out when the user did not set it.
MINICODE_CAPPED_DEFAULT_MAX_TOKENS = 8_000

# MiniCode's default when a provider does not publish a context window.
MODEL_CONTEXT_WINDOW_DEFAULT = 200_000


_KNOWN_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic Claude 4 / 3.x families
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-3-7", 200_000),
    ("claude-3-5", 200_000),
    ("claude-3-", 200_000),
    ("claude-instant-", 100_000),
    # OpenAI GPT-4 / o-series families
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4-", 128_000),
    ("o1-", 200_000),
    ("o3-", 200_000),
    ("o4-", 200_000),
    # Small local / open models — the ones a 200K default actively breaks
    # compaction for. Longest-prefix-first means a variant id beats the family.
    ("qwen3-32b", 128_000),
    ("qwen-32b", 128_000),
    ("qwen2.5-", 128_000),
    ("deepseek-v3", 128_000),
    ("deepseek-r1", 128_000),
    ("llama-3.3", 128_000),
    ("llama-3.1", 128_000),
    ("llama-3", 8_000),
    ("mistral-", 32_000),
    ("gemma-", 8_000),
    ("phi-3", 128_000),
    ("phi-4", 16_000),
    ("grok-", 131_000),
    # SuperToken's v4flash advertises a one-million-token window. Keep this
    # model-specific record ahead of the conservative unknown-model fallback;
    # a provider response can still override it when the gateway publishes a
    # different limit for a particular deployment.
    ("v4flash", 1_000_000),
)


class SettingsError(RuntimeError):
    """配置加载失败时抛出。"""


@dataclass(frozen=True)
class LLMSettings:
    """LLM 连接配置。"""

    api_key: str
    provider: str = "custom"
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    max_tokens: int | float = 0
    wire_api: str = "chat"
    proxy_mode: str = "inherit"
    prompt_cache_retention: str = ""
    reasoning_effort_levels: tuple[str, ...] = ()
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
    seed: int | None = None
    default_headers: tuple[tuple[str, str], ...] = ()
    auth_header: bool = False
    image_model: str = ""
    image_size: str = "1024x1024"
    image_quality: str = ""


@dataclass(frozen=True)
class ContextWindowResolution:
    tokens: int
    source: str
    verified: bool


def _serialized_settings_update(function: Callable[..., Any]) -> Callable[..., Any]:
    """Keep synchronous whole-settings updates from overwriting each other."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _SETTINGS_WRITE_LOCK:
            with file_mutation_locks([SETTINGS_FILE]):
                return function(*args, **kwargs)

    return wrapped


def resolve_context_window_details(
    model: str,
    *,
    provider_context_window: Any = 0,
    provider_max_context_window: Any = 0,
) -> ContextWindowResolution:
    """Resolve context size with MiniCode-style model-metadata precedence.

    The numeric fallback remains necessary for budgeting, but provenance is
    retained so an unknown model is never presented as a provider-verified
    200K model.  An explicit host override wins, followed by live provider
    metadata, a known model-family value, and finally the conservative fallback.
    """

    model_id = str(model or "").strip()
    catalog_entry = responses_model_catalog_entry(model_id)
    try:
        provider_max_value = int(provider_max_context_window or 0)
    except (TypeError, ValueError):
        provider_max_value = 0
    published_max = (
        provider_max_value
        if provider_max_value > 0
        else catalog_entry.max_context_window
        if catalog_entry is not None
        else 0
    )

    override = os.environ.get("MINICODE_MAX_CONTEXT_TOKENS", "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                if published_max > 0:
                    value = min(value, published_max)
                return ContextWindowResolution(value, "host_override", True)
        except ValueError:
            pass

    try:
        provider_value = int(provider_context_window or 0)
    except (TypeError, ValueError):
        provider_value = 0
    if provider_value > 0:
        return ContextWindowResolution(provider_value, "provider", True)

    if not model_id:
        return ContextWindowResolution(
            MODEL_CONTEXT_WINDOW_DEFAULT,
            "fallback",
            False,
        )
    lowered = model_id.lower()
    # Gateways commonly namespace direct-provider ids (for example
    # ``openai/gpt-5.6-sol``). Capability matching is about the terminal model
    # id, not the routing namespace, so consider both without weakening the
    # unknown-model fallback.
    model_candidates = (lowered, lowered.rsplit("/", 1)[-1])
    # Explicit [1m] suffix opt-in wins over the family window (MiniCode's
    # has1mContext behavior). e.g. claude-opus-4-1m → 1M, not the 200K family.
    # cc marks 1M-context models with the "[1m]" suffix (context.ts); the
    # legacy "-1m" spelling stays accepted for older settings.
    if any(
        "[1m]" in candidate or candidate.endswith("-1m") or "-1m-" in candidate
        for candidate in model_candidates
    ):
        return ContextWindowResolution(1_000_000, "known_model", True)
    if catalog_entry is not None:
        return ContextWindowResolution(
            catalog_entry.context_window,
            "known_model",
            True,
        )
    # Longest prefix wins so a specific id beats its family.
    matched = 0
    for prefix, window in _KNOWN_MODEL_CONTEXT_WINDOWS:
        if any(candidate.startswith(prefix) for candidate in model_candidates):
            if len(prefix) > matched:
                matched = len(prefix)
                result = window
    if matched:
        return ContextWindowResolution(result, "known_model", True)
    return ContextWindowResolution(
        MODEL_CONTEXT_WINDOW_DEFAULT,
        "fallback",
        False,
    )



def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    if value in {"openai", "anthropic", "custom"}:
        return value
    raise SettingsError(
        f"Unknown LLM provider '{str(provider or '').strip() or '<empty>'}'. "
        "Choose one of: openai, anthropic, custom."
    )


_RUNTIME_API_KEY_SCOPES: dict[str, str] = {}
_RUNTIME_IMAGE_API_KEY_SCOPES: dict[str, str] = {}


def _normalized_provider_base_url(
    base_url: str,
    *,
    collapse_root_v1: bool,
) -> str:
    """Return a stable credential identity without query/fragment noise.

    Only an origin root and its exact ``/v1`` path are equivalent.  Other
    paths remain part of the identity so an Anthropic bridge, a v2 endpoint,
    and an unrelated custom API on the same host cannot share credentials.
    """

    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        if collapse_root_v1 and path.lower() in {"", "/v1"}:
            path = ""
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                "",
                "",
            )
        )

    # Preserve compatibility for non-URL gateway identifiers while applying
    # the same query/fragment and trailing-slash normalization.
    identity = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if collapse_root_v1 and identity.lower().endswith("/v1"):
        identity = identity[:-3].rstrip("/")
    return identity


def _provider_key_scope(base_url: str) -> str:
    identity = _normalized_provider_base_url(
        base_url,
        collapse_root_v1=True,
    )
    if not identity.strip(":/"):
        return ""
    import hashlib

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def _legacy_provider_key_scope(base_url: str) -> str:
    """Reproduce the pre-normalization scope for transparent migration."""

    parsed = urlsplit(str(base_url or "").strip())
    identity = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    if not identity.strip(":/"):
        return ""
    import hashlib

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def _provider_key_base_url_candidates(base_url: str) -> tuple[str, ...]:
    """Return compatible root/``/v1`` spellings plus the legacy raw value."""

    raw = str(base_url or "").strip()
    if not raw:
        return ()
    exact = _normalized_provider_base_url(raw, collapse_root_v1=False)
    parsed = urlsplit(exact)
    candidates: list[str] = []

    def append(value: str) -> None:
        normalized = str(value or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    if parsed.scheme and parsed.netloc and parsed.path.lower() in {"", "/v1"}:
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        append(origin)
        append(f"{origin}/v1")
    else:
        append(exact)
    append(raw)
    return tuple(candidates)



def _scoped_vault_names(provider: str, base_url: str) -> tuple[str, ...]:
    """Return normalized and legacy vault names for one endpoint family."""

    prefix = {
        "anthropic": "ANTHROPIC_API_KEY",
        "custom": "CUSTOM_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider, "OPENAI_API_KEY")
    names: list[str] = []
    for candidate in _provider_key_base_url_candidates(base_url):
        for scope in (
            _provider_key_scope(candidate),
            _legacy_provider_key_scope(candidate),
        ):
            name = f"{prefix}_{scope}" if scope else ""
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _image_scoped_vault_names(provider: str, base_url: str) -> tuple[str, ...]:
    """Return endpoint-scoped vault names for an optional Images API channel."""

    normalized_provider = _normalize_provider(provider).upper()
    prefix = f"MINICODE_{normalized_provider}_IMAGE_API_KEY"
    names: list[str] = []
    for candidate in _provider_key_base_url_candidates(base_url):
        for scope in (
            _provider_key_scope(candidate),
            _legacy_provider_key_scope(candidate),
        ):
            name = f"{prefix}_{scope}" if scope else ""
            if name and name not in names:
                names.append(name)
    return tuple(names)



def _image_api_key_for_base_url(provider: str, base_url: str) -> str:
    for name in _image_scoped_vault_names(provider, base_url):
        value = os.getenv(name, "").strip() or _vault_api_key(name).strip()
        if _is_api_key_replacement(value):
            return value
    return ""



def _vault_has_scoped_provider_keys(provider: str) -> bool:
    prefix = {
        "anthropic": "ANTHROPIC_API_KEY_",
        "custom": "CUSTOM_API_KEY_",
        "openai": "OPENAI_API_KEY_",
    }.get(provider, "OPENAI_API_KEY_")
    if any(
        str(name).startswith(prefix) and _is_api_key_replacement(value)
        for name, value in os.environ.items()
    ):
        return True
    try:
        from backend.vault import EnvVault

        return any(
            str(entry.get("name") or "").startswith(prefix)
            for entry in EnvVault().list_names()
            if isinstance(entry, dict)
        )
    except Exception:
        return False


def _global_provider_key_matches_base_url(provider: str, base_url: str) -> bool:
    """Allow legacy global keys without letting one saved profile leak to another."""

    requested_scope = _provider_key_scope(base_url)
    if not requested_scope:
        return True
    runtime_scope = _RUNTIME_API_KEY_SCOPES.get(provider, "")
    if runtime_scope:
        return runtime_scope == requested_scope
    base_env = {
        "anthropic": "ANTHROPIC_BASE_URL",
        "custom": "CUSTOM_BASE_URL",
        "openai": "OPENAI_BASE_URL",
    }.get(provider, "OPENAI_BASE_URL")
    configured_base_url = os.getenv(base_env, "").strip()
    if configured_base_url:
        return _provider_key_scope(configured_base_url) == requested_scope
    # A vault containing endpoint-scoped records has already migrated to the
    # safe contract.  Its ambiguous global compatibility value must not be
    # applied to an endpoint that has no matching scoped credential.
    return not _vault_has_scoped_provider_keys(provider)



def _vault_api_key(name: str) -> str:
    try:
        from backend.vault import EnvVault

        value = EnvVault().get(name)
    except Exception:
        return ""
    return str(value or "").strip()


def _provider_api_key_for_base_url(provider: str, base_url: str) -> str:
    provider = _normalize_provider(provider)
    if provider == "custom":
        return _custom_provider_api_key(base_url)

    env_name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    for scoped_name in _scoped_vault_names(provider, base_url):
        scoped = os.getenv(scoped_name, "").strip() or _vault_api_key(scoped_name).strip()
        if _is_api_key_replacement(scoped):
            return scoped

    if _global_provider_key_matches_base_url(provider, base_url):
        direct = os.getenv(env_name, "").strip()
        if _is_api_key_replacement(direct):
            return direct

        saved = _vault_api_key(env_name).strip()
        if _is_api_key_replacement(saved):
            return saved
    return ""


def _custom_provider_api_key(base_url: str, *, allow_scoped: bool = True) -> str:
    if allow_scoped:
        for scoped_name in _scoped_vault_names("custom", base_url):
            scoped = os.getenv(scoped_name, "").strip() or _vault_api_key(scoped_name).strip()
            if _is_api_key_replacement(scoped):
                return scoped

    if _global_provider_key_matches_base_url("custom", base_url):
        direct = os.getenv("CUSTOM_API_KEY", "").strip()
        if _is_api_key_replacement(direct):
            return direct

        direct = _vault_api_key("CUSTOM_API_KEY").strip()
        if _is_api_key_replacement(direct):
            return direct

    return ""



def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default



def _coerce_model_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = []

    models: list[str] = []
    for item in items:
        candidate = str(item).strip()
        if candidate and candidate not in models:
            models.append(candidate)
    return models


def _coerce_reasoning_effort_levels(value: Any) -> list[str]:
    """Normalize only reasoning-effort shapes advertised by reference clients."""

    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []

    levels: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            raw_level = next(
                (
                    item.get(key)
                    for key in ("reasoning_effort", "reasoningEffort", "effort")
                    if item.get(key) is not None
                ),
                "",
            )
        else:
            raw_level = item
        normalized = str(raw_level or "").strip().lower()
        # Codex preserves model-advertised custom effort strings in addition
        # to its built-in levels. Trust the value only because it came from
        # this model's explicit provider metadata.
        if normalized and normalized not in levels:
            levels.append(normalized)
    return levels


def _coerce_model_metadata(value: Any) -> dict[str, dict[str, Any]]:
    """Keep only provider model metadata MiniCode can enforce truthfully."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_model_id, raw_metadata in value.items():
        model_id = str(raw_model_id or "").strip()
        if not model_id or not isinstance(raw_metadata, Mapping):
            continue
        metadata: dict[str, Any] = {}
        context_window = _coerce_int(
            next(
                (
                    raw_metadata.get(key)
                    for key in (
                        "context_window",
                        "contextWindow",
                        "context_length",
                        "contextLength",
                        # Claude Code model capabilities use this exact field.
                        "max_input_tokens",
                    )
                    if raw_metadata.get(key) is not None
                ),
                0,
            ),
            0,
        )
        if context_window > 0:
            metadata["context_window"] = context_window
        max_context_window = _coerce_int(
            next(
                (
                    raw_metadata.get(key)
                    for key in ("max_context_window", "maxContextWindow")
                    if raw_metadata.get(key) is not None
                ),
                0,
            ),
            0,
        )
        if max_context_window > 0:
            metadata["max_context_window"] = max_context_window
        max_output_tokens = _coerce_int(
            next(
                (
                    raw_metadata.get(key)
                    for key in (
                        "max_output_tokens",
                        "maxOutputTokens",
                        "max_tokens",
                        "maxTokens",
                    )
                    if raw_metadata.get(key) is not None
                ),
                0,
            ),
            0,
        )
        if max_output_tokens > 0:
            metadata["max_output_tokens"] = max_output_tokens
        raw_levels = _coerce_reasoning_effort_levels(
            next(
                (
                    raw_metadata.get(key)
                    for key in (
                        "reasoning_effort_levels",
                        "supported_reasoning_efforts",
                        # Codex core ModelInfo serializes this snake_case field.
                        "supported_reasoning_levels",
                        # Codex app-server model/list serializes this camelCase field.
                        "supportedReasoningEfforts",
                    )
                    if raw_metadata.get(key) is not None
                ),
                [],
            )
        )
        if raw_levels:
            metadata["reasoning_effort_levels"] = raw_levels
        default_reasoning_effort = str(
            next(
                (
                    raw_metadata.get(key)
                    for key in (
                        "default_reasoning_effort",
                        "defaultReasoningEffort",
                        "default_reasoning_level",
                        "defaultReasoningLevel",
                    )
                    if raw_metadata.get(key) is not None
                ),
                "",
            )
            or ""
        ).strip().lower()
        if default_reasoning_effort:
            metadata["default_reasoning_effort"] = default_reasoning_effort
        default_reasoning_summary = str(
            next(
                (
                    raw_metadata.get(key)
                    for key in (
                        "default_reasoning_summary",
                        "defaultReasoningSummary",
                    )
                    if raw_metadata.get(key) is not None
                ),
                "",
            )
            or ""
        ).strip().lower()
        if default_reasoning_summary:
            metadata["default_reasoning_summary"] = default_reasoning_summary
        if metadata:
            metadata["source"] = "provider"
            result[model_id] = metadata
    return result


def _coerce_model_labels(value: Any) -> dict[str, str]:
    """Normalize optional Composer labels without changing request model ids."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_model, raw_label in value.items():
        model = str(raw_model or "").strip()
        label = str(raw_label or "").strip()
        if model and label and model != label:
            result[model] = label
    return result


def get_provider_model_metadata(
    section: Mapping[str, Any] | None,
    model: str,
) -> dict[str, Any]:
    """Resolve one model's enforceable metadata and its provenance."""

    data = section if isinstance(section, Mapping) else {}
    model_id = str(model or data.get("model") or "").strip()
    catalog = _coerce_model_metadata(data.get("model_metadata"))
    declared = dict(catalog.get(model_id, {}))
    if not declared:
        terminal_id = model_id.rsplit("/", 1)[-1]
        declared = dict(catalog.get(terminal_id, {}))
    known = responses_model_catalog_entry(model_id)
    levels = _coerce_model_list(declared.get("reasoning_effort_levels"))
    if not levels and model_id == str(data.get("model") or "").strip():
        levels = _coerce_model_list(data.get("reasoning_effort_levels"))
    if not levels and known is not None:
        levels = list(known.reasoning_effort_levels)
    declared_max_context = _coerce_int(declared.get("max_context_window"), 0)
    resolution = resolve_context_window_details(
        model_id,
        provider_context_window=declared.get("context_window", 0),
        provider_max_context_window=declared_max_context,
    )
    if declared_max_context > 0:
        max_context_window = max(declared_max_context, resolution.tokens)
        max_context_window_source = "provider"
        max_context_window_verified = True
    elif known is not None:
        max_context_window = max(known.max_context_window, resolution.tokens)
        max_context_window_source = "known_model"
        max_context_window_verified = True
    else:
        max_context_window = resolution.tokens
        max_context_window_source = resolution.source
        max_context_window_verified = resolution.verified
    max_output_tokens = _coerce_int(declared.get("max_output_tokens"), 0)
    default_reasoning_effort = str(
        declared.get("default_reasoning_effort")
        or (known.default_reasoning_effort if known is not None else "")
    ).strip().lower()
    default_reasoning_summary = str(
        declared.get("default_reasoning_summary")
        or (known.default_reasoning_summary if known is not None else "")
    ).strip().lower()
    return {
        "reasoning_effort_levels": levels,
        "default_reasoning_effort": default_reasoning_effort,
        "default_reasoning_summary": default_reasoning_summary,
        "context_window": resolution.tokens,
        "context_window_source": resolution.source,
        "context_window_verified": resolution.verified,
        "max_context_window": max_context_window,
        "max_context_window_source": max_context_window_source,
        "max_context_window_verified": max_context_window_verified,
        "max_output_tokens": max_output_tokens,
        "max_output_tokens_source": "provider" if max_output_tokens > 0 else "",
        "max_output_tokens_verified": max_output_tokens > 0,
    }



def _normalize_wire_api(value: str, default: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"responses", "chat", "anthropic"}:
        return normalized
    if normalized in {"anthropic_messages", "messages", "claude"}:
        return "anthropic"
    # An unrecognized wire API is a contract error, not a request for the
    # default one: silently returning `default` sends the user's requests over
    # a different protocol than they configured, and makes the fail-closed
    # guard in build_wire_adapter unreachable for real misconfiguration.
    raise SettingsError(
        f"Unknown wire API '{value.strip()}'. "
        "Choose one of: responses, chat, anthropic."
    )


def normalize_custom_wire_api(base_url: str, value: str, default: str = "chat") -> str:
    del base_url
    return _normalize_wire_api(value, default)


def _is_api_key_replacement(value: Any) -> bool:
    """Only a newly entered secret may replace the key stored in the vault."""
    text = str(value or "").strip()
    if not text or text == "••••":
        return False
    # Compatibility with older settings payloads that returned shortened keys.
    if len(text) == 8 and text[3] == "…":
        return False
    return True



def _reasoning_effort_projection(
    *,
    model: str,
    wire_api: str,
    configured: str,
    levels: list[str],
    default_effort: str = "",
) -> dict[str, Any]:
    from backend.llm.reasoning_effort import normalize_reasoning_effort

    normalized_configured = str(configured or "").strip().lower()
    effective = normalize_reasoning_effort(
        model,
        wire_api,
        normalized_configured,
        levels,
        default_effort,
    )
    return {
        "configured_reasoning_effort": normalized_configured,
        "effective_reasoning_effort": effective,
        "reasoning_effort_supported": bool(levels),
    }


def _select_custom_model(model: str, available_models: list[str]) -> str:
    """Select a model without inferring its vendor from the wire protocol.

    Codex keeps the model slug and provider ``wire_api`` as independent
    configuration fields.  Anthropic-compatible gateways may expose non-Claude
    model ids, so a Messages transport must not synthesize a Claude model.
    """
    current = str(model or "").strip()
    if current:
        return current
    return next((str(item).strip() for item in available_models if str(item).strip()), "")


def _write_settings_json(data: dict[str, Any]) -> None:
    with _SETTINGS_WRITE_LOCK:
        with file_mutation_locks([SETTINGS_FILE]):
            atomic_write_text(
                SETTINGS_FILE,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )



def _serialized_settings_update(function: Callable[..., Any]) -> Callable[..., Any]:
    """Keep synchronous whole-settings updates from overwriting each other."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _SETTINGS_WRITE_LOCK:
            with file_mutation_locks([SETTINGS_FILE]):
                return function(*args, **kwargs)

    return wrapped


def _get_llm_section(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = settings_data if settings_data is not None else _load_effective_settings_json()
    llm_data = raw.get("llm", {}) if isinstance(raw, dict) else {}
    return llm_data if isinstance(llm_data, dict) else {}


def get_llm_provider(settings_data: dict[str, Any] | None = None) -> str:
    llm_data = _get_llm_section(settings_data)
    configured = llm_data.get("provider")
    if isinstance(configured, str) and configured.strip():
        return _normalize_provider(configured)
    raw_environment_provider = str(os.getenv("LLM_PROVIDER", "openai") or "").strip()
    return _normalize_provider(raw_environment_provider or "openai")


def _provider_display_name(provider_data: dict[str, Any]) -> str:
    for key in ("display_name", "name", "label"):
        value = str(provider_data.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_prompt_cache_retention(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip().lower()
    if text in {"", "off", "none", "false", "0"}:
        return ""
    if text in {"24h", "in_memory"}:
        return text
    return default


def _normalize_image_mode(value: Any, default: str = "inherit") -> str:
    mode = str(value if value is not None else default).strip().lower()
    if mode in {"disabled", "inherit", "custom"}:
        return mode
    return default


def _normalize_proxy_mode(value: Any, default: str = "inherit") -> str:
    return normalize_provider_proxy_mode(value, default)


def _normalize_image_size(value: Any, default: str = "1024x1024") -> str:
    size = str(value if value is not None else default).strip().lower()
    if size in {"auto", "1024x1024", "1536x1024", "1024x1536"}:
        return size
    return default


def _normalize_image_quality(value: Any) -> str:
    quality = str(value or "").strip().lower()
    if quality in {"", "auto", "low", "medium", "high", "standard", "hd"}:
        return quality
    return ""


def _responses_prompt_cache_retention_default(enabled: bool, provider: str = "openai") -> str:
    if not enabled:
        return ""
    env_name = (
        "CUSTOM_PROMPT_CACHE_RETENTION"
        if _normalize_provider(provider) == "custom"
        else "OPENAI_PROMPT_CACHE_RETENTION"
    )
    return _normalize_prompt_cache_retention(os.getenv(env_name, ""), "")


def _history_profile_identity(
    provider: str,
    base_url: str,
    wire_api: str,
) -> tuple[str, str, str]:
    normalized_provider, normalized_base_url, _ = _history_identity(
        provider,
        base_url,
        wire_api,
    )
    # A provider card represents one configured endpoint.  Chat, Responses,
    # and Anthropic wire selection are transport details of that endpoint, not
    # separate saved credentials/profiles.
    return normalized_provider, normalized_base_url, ""


def _llm_history(
    settings_data: dict[str, Any] | None = None,
    *,
    include_api_keys: bool = False,
) -> list[dict[str, Any]]:
    llm_data = _get_llm_section(settings_data)
    raw_history = llm_data.get("provider_history", [])
    if not isinstance(raw_history, list):
        return []
    normalized_history: list[dict[str, Any]] = []
    for raw in raw_history:
        if not isinstance(raw, dict):
            continue
        provider = _normalize_provider(str(raw.get("provider") or ""))
        base_url = str(raw.get("base_url") or "").strip()
        model = str(raw.get("model") or "").strip()
        wire_api = _history_identity(provider, base_url, str(raw.get("wire_api") or ""))[2]
        responses_defaults_enabled = wire_api == "responses"
        prompt_cache_retention_default = _responses_prompt_cache_retention_default(
            responses_defaults_enabled,
            provider,
        )
        available_models = _coerce_model_list(raw.get("available_models"))
        model_metadata = _coerce_model_metadata(raw.get("model_metadata"))
        resolved_metadata = get_provider_model_metadata(
            {
                "model": model,
                "model_metadata": model_metadata,
                "reasoning_effort_levels": raw.get("reasoning_effort_levels"),
            },
            model,
        )
        reasoning_effort = str(raw.get("reasoning_effort") or "").strip().lower()
        reasoning_projection = (
            _reasoning_effort_projection(
                model=model,
                wire_api=wire_api,
                configured=reasoning_effort,
                levels=resolved_metadata["reasoning_effort_levels"],
                default_effort=resolved_metadata["default_reasoning_effort"],
            )
            if provider != "anthropic"
            else {
                "configured_reasoning_effort": "",
                "effective_reasoning_effort": "",
                "reasoning_effort_supported": False,
            }
        )
        api_key = _provider_api_key_for_base_url(provider, base_url)
        request_material = _provider_request_material(raw)
        image_mode = _normalize_image_mode(raw.get("image_mode"))
        image_base_url = str(raw.get("image_base_url") or "").strip()
        image_key = _image_api_key_for_base_url(provider, image_base_url)
        entry = {
            "provider": provider,
            "provider_id": _provider_id_for_history(provider, base_url, wire_api),
            "display_name": _provider_display_name(raw),
            "base_url": base_url,
            "model": model,
            "small_fast_model": str(raw.get("small_fast_model") or "").strip(),
            "available_models": available_models,
            "models_source": str(raw.get("models_source") or "").strip(),
            "model_metadata": model_metadata,
            "wire_api": wire_api,
            "proxy_mode": _normalize_proxy_mode(raw.get("proxy_mode")),
            "headers": dict(request_material["default_headers"]),
            "auth_header": request_material["auth_header"],
            "reasoning_effort": reasoning_effort,
            **reasoning_projection,
            "responses_reasoning_summary": str(raw.get("responses_reasoning_summary") or "off").strip(),
            "max_tokens": max(0, _coerce_int(raw.get("max_tokens", 0), 0)),
            "prompt_cache_retention": _normalize_prompt_cache_retention(
                raw.get("prompt_cache_retention", prompt_cache_retention_default),
                prompt_cache_retention_default,
            ),
            "reasoning_effort_levels": resolved_metadata["reasoning_effort_levels"],
            "context_window": resolved_metadata["context_window"],
            "context_window_source": resolved_metadata["context_window_source"],
            "context_window_verified": resolved_metadata["context_window_verified"],
            "max_context_window": resolved_metadata["max_context_window"],
            "max_context_window_source": resolved_metadata["max_context_window_source"],
            "max_context_window_verified": resolved_metadata["max_context_window_verified"],
            "max_output_tokens": resolved_metadata["max_output_tokens"],
            "max_output_tokens_source": resolved_metadata["max_output_tokens_source"],
            "max_output_tokens_verified": resolved_metadata["max_output_tokens_verified"],
            "default_reasoning_effort": resolved_metadata["default_reasoning_effort"],
            "default_reasoning_summary": resolved_metadata["default_reasoning_summary"],
            "thinking_budget": _coerce_int(raw.get("thinking_budget", 0), 0),
            "image_mode": image_mode,
            # Never trust the persisted visibility bit as proof that a secret
            # still exists. History cards must reflect the endpoint-scoped
            # image vault after deletion, rotation, or an endpoint change.
            "has_image_api_key": bool(image_key),
            "image_api_key": image_key if include_api_keys else "",
            "image_base_url": image_base_url,
            "image_model": str(raw.get("image_model") or "").strip(),
            "image_size": _normalize_image_size(raw.get("image_size")),
            "image_quality": _normalize_image_quality(raw.get("image_quality")),
            # Credential presence is derived from the endpoint-scoped vault,
            # never from a persisted display bit that can outlive the secret.
            "has_api_key": bool(api_key),
            "api_key": api_key if include_api_keys else "",
            "updated_at": float(raw.get("updated_at") or 0),
        }
        normalized_history.append(entry)
    normalized_history.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    history: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in normalized_history:
        identity = _history_profile_identity(
            str(entry.get("provider") or ""),
            str(entry.get("base_url") or ""),
            str(entry.get("wire_api") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        history.append(entry)
    return history[:16]


def _provider_id_for_history(provider: str, base_url: str, wire_api: str) -> str:
    del base_url
    if provider == "anthropic":
        return "anthropic"
    if provider == "custom" and wire_api == "anthropic":
        return "custom_anthropic"
    return provider



def _history_identity(provider: str, base_url: str, wire_api: str) -> tuple[str, str, str]:
    normalized_provider = _normalize_provider(provider)
    normalized_base_url = _normalized_provider_base_url(
        base_url,
        collapse_root_v1=True,
    ).lower()
    default_wire_api = (
        "anthropic"
        if normalized_provider == "anthropic"
        else "responses"
        if normalized_provider == "openai"
        else "chat"
    )
    raw_wire_api = str(wire_api or default_wire_api).strip()
    if normalized_provider == "custom":
        normalized_wire_api = normalize_custom_wire_api(normalized_base_url, raw_wire_api, "chat")
    elif normalized_provider == "openai":
        normalized_wire_api = normalize_custom_wire_api(
            normalized_base_url,
            raw_wire_api,
            "responses",
        )
    else:
        normalized_wire_api = "anthropic"
    return normalized_provider, normalized_base_url, normalized_wire_api



def _provider_image_fields(
    provider: str,
    provider_data: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_provider(provider)
    env_prefix = normalized.upper()
    image_mode = _normalize_image_mode(
        provider_data.get("image_mode", os.getenv(f"{env_prefix}_IMAGE_MODE", "inherit")),
    )
    raw_base_url = str(
        provider_data.get("image_base_url")
        or os.getenv(f"{env_prefix}_IMAGE_BASE_URL", "")
    ).strip()
    image_base_url = _normalize_openai_base_url(raw_base_url) if raw_base_url else ""
    image_model = str(
        provider_data.get("image_model")
        or os.getenv(f"{env_prefix}_IMAGE_MODEL", "")
    ).strip()
    image_size = _normalize_image_size(
        provider_data.get("image_size", os.getenv(f"{env_prefix}_IMAGE_SIZE", "1024x1024")),
    )
    image_quality = _normalize_image_quality(
        provider_data.get("image_quality", os.getenv(f"{env_prefix}_IMAGE_QUALITY", "")),
    )
    independent_key = _image_api_key_for_base_url(normalized, image_base_url)
    return {
        "image_mode": image_mode,
        "image_api_key": independent_key,
        # This flag describes only the separately stored Images API secret.
        # Inherit mode uses the ordinary provider ``has_api_key`` contract.
        "has_image_api_key": bool(independent_key),
        "image_base_url": image_base_url,
        "image_model": image_model,
        "image_size": image_size,
        "image_quality": image_quality,
    }


def _provider_proxy_mode(
    provider: str,
    provider_data: Mapping[str, Any],
) -> str:
    normalized = _normalize_provider(provider)
    env_name = f"{normalized.upper()}_PROXY_MODE"
    return _normalize_proxy_mode(
        provider_data.get("proxy_mode", os.getenv(env_name, "inherit")),
    )


def _provider_request_material(provider_data: Mapping[str, Any]) -> dict[str, Any]:
    """Project optional provider request material at the config boundary."""
    raw_headers = provider_data.get("headers", provider_data.get("default_headers", {}))
    if not isinstance(raw_headers, Mapping):
        raise SettingsError("provider headers must be an object")
    headers: list[tuple[str, str]] = []
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name).strip()
        value = str(raw_value)
        if (
            not name
            or any(character in name for character in ("\r", "\n", "\0"))
        ):
            raise SettingsError(
                "provider header names must be non-empty and single-line"
            )
        if any(character in value for character in ("\r", "\n", "\0")):
            raise SettingsError(f"provider header contains a control separator: {name}")
        headers.append((name, value))
    return {
        "default_headers": tuple(headers),
        "auth_header": coerce_feature_bool(
            provider_data.get("auth_header", provider_data.get("authHeader")),
            False,
        ),
    }


def get_openai_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("openai", {})
    provider_data = raw if isinstance(raw, dict) else {}

    base_url = str(provider_data.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip()
    api_key = _provider_api_key_for_base_url("openai", base_url)
    model = str(provider_data.get("model") or os.getenv("OPENAI_MODEL", "")).strip()
    small_fast_model = str(
        provider_data.get("small_fast_model")
        or os.getenv("OPENAI_SMALL_FAST_MODEL", "")
        or os.getenv("MINICODE_SMALL_FAST_MODEL", "")
    ).strip()
    reasoning_effort = str(
        provider_data.get("reasoning_effort") or os.getenv("OPENAI_REASONING_EFFORT", "")
    ).strip()
    responses_reasoning_summary = str(
        provider_data.get("responses_reasoning_summary")
        or os.getenv("OPENAI_RESPONSES_REASONING_SUMMARY", "off")
    ).strip()
    wire_api = normalize_custom_wire_api(
        base_url,
        str(provider_data.get("wire_api") or os.getenv("OPENAI_WIRE_API", "responses")),
        "responses",
    )
    responses_defaults_enabled = wire_api == "responses"
    max_tokens = max(
        0,
        _coerce_int(provider_data.get("max_tokens", os.getenv("OPENAI_MAX_TOKENS", "0")), 0),
    )
    prompt_cache_retention = (
        _normalize_prompt_cache_retention(
            provider_data.get(
                "prompt_cache_retention",
                os.getenv("OPENAI_PROMPT_CACHE_RETENTION", ""),
            ),
            _responses_prompt_cache_retention_default(False),
        )
        if responses_defaults_enabled
        else ""
    )

    available_models = _coerce_model_list(provider_data.get("available_models"))
    if not available_models:
        available_models = _coerce_model_list(os.getenv("OPENAI_AVAILABLE_MODELS", ""))
    if model and model not in available_models:
        available_models.insert(0, model)
    models_source = str(provider_data.get("models_source") or "").strip()
    model_metadata = _coerce_model_metadata(provider_data.get("model_metadata"))
    model_labels = _coerce_model_labels(provider_data.get("model_labels"))
    resolved_metadata = get_provider_model_metadata(
        {
            "model": model,
            "model_metadata": model_metadata,
            "reasoning_effort_levels": provider_data.get("reasoning_effort_levels"),
        },
        model,
    )
    from backend.llm.reasoning_effort import reasoning_effort_levels as resolve_effort_levels

    reasoning_effort_levels = list(
        resolve_effort_levels(
            model,
            wire_api,
            resolved_metadata["reasoning_effort_levels"],
        )
    )
    reasoning_projection = _reasoning_effort_projection(
        model=model,
        wire_api=wire_api,
        configured=reasoning_effort,
        levels=reasoning_effort_levels,
        default_effort=resolved_metadata["default_reasoning_effort"],
    )
    image_fields = _provider_image_fields(
        "openai",
        provider_data,
    )

    return {
        "display_name": _provider_display_name(provider_data),
        "api_key": api_key,
        "base_url": _normalize_openai_base_url(base_url),
        "model": model,
        "small_fast_model": small_fast_model,
        "available_models": available_models,
        "models_source": models_source,
        "model_metadata": model_metadata,
        "model_labels": model_labels,
        "reasoning_effort": reasoning_effort,
        **reasoning_projection,
        "responses_reasoning_summary": responses_reasoning_summary,
        "max_tokens": max_tokens,
        "wire_api": wire_api,
        "proxy_mode": _provider_proxy_mode("openai", provider_data),
        "prompt_cache_retention": prompt_cache_retention,
        "reasoning_effort_levels": reasoning_effort_levels,
        "context_window": resolved_metadata["context_window"],
        "context_window_source": resolved_metadata["context_window_source"],
        "context_window_verified": resolved_metadata["context_window_verified"],
        "max_context_window": resolved_metadata["max_context_window"],
        "max_context_window_source": resolved_metadata["max_context_window_source"],
        "max_context_window_verified": resolved_metadata["max_context_window_verified"],
        "max_output_tokens": resolved_metadata["max_output_tokens"],
        "max_output_tokens_source": resolved_metadata["max_output_tokens_source"],
        "max_output_tokens_verified": resolved_metadata["max_output_tokens_verified"],
        "default_reasoning_effort": resolved_metadata["default_reasoning_effort"],
        "default_reasoning_summary": resolved_metadata["default_reasoning_summary"],
        **_provider_request_material(provider_data),
        **image_fields,
    }


def get_anthropic_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("anthropic", {})
    provider_data = raw if isinstance(raw, dict) else {}

    base_url = str(provider_data.get("base_url") or os.getenv("ANTHROPIC_BASE_URL", "")).strip()
    api_key = _provider_api_key_for_base_url("anthropic", base_url)
    model = str(
        provider_data.get("model") or os.getenv("ANTHROPIC_MODEL", "")
    ).strip()
    small_fast_model = str(
        provider_data.get("small_fast_model")
        or os.getenv("ANTHROPIC_SMALL_FAST_MODEL", "")
        or os.getenv("MINICODE_SMALL_FAST_MODEL", "")
    ).strip()
    max_tokens = _coerce_int(
        provider_data.get(
            "max_tokens",
            os.getenv("ANTHROPIC_MAX_TOKENS", str(MINICODE_CAPPED_DEFAULT_MAX_TOKENS)),
        ),
        MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
    )
    if max_tokens <= 0:
        max_tokens = MINICODE_CAPPED_DEFAULT_MAX_TOKENS
    thinking_budget = _coerce_int(
        provider_data.get("thinking_budget", os.getenv("ANTHROPIC_THINKING_BUDGET", "0")),
        0,
    )

    available_models = _coerce_model_list(provider_data.get("available_models"))
    if not available_models:
        available_models = _coerce_model_list(os.getenv("ANTHROPIC_AVAILABLE_MODELS", ""))
    if model and model not in available_models:
        available_models.insert(0, model)
    models_source = str(provider_data.get("models_source") or "").strip()
    model_metadata = _coerce_model_metadata(provider_data.get("model_metadata"))
    model_labels = _coerce_model_labels(provider_data.get("model_labels"))
    resolved_metadata = get_provider_model_metadata(
        {"model": model, "model_metadata": model_metadata},
        model,
    )
    image_fields = _provider_image_fields(
        "anthropic",
        provider_data,
    )

    return {
        "display_name": _provider_display_name(provider_data),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "small_fast_model": small_fast_model,
        "available_models": available_models,
        "models_source": models_source,
        "model_metadata": model_metadata,
        "model_labels": model_labels,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
        "proxy_mode": _provider_proxy_mode("anthropic", provider_data),
        "configured_reasoning_effort": "",
        "effective_reasoning_effort": "",
        "reasoning_effort_supported": False,
        "context_window": resolved_metadata["context_window"],
        "context_window_source": resolved_metadata["context_window_source"],
        "context_window_verified": resolved_metadata["context_window_verified"],
        "max_context_window": resolved_metadata["max_context_window"],
        "max_context_window_source": resolved_metadata["max_context_window_source"],
        "max_context_window_verified": resolved_metadata["max_context_window_verified"],
        "max_output_tokens": resolved_metadata["max_output_tokens"],
        "max_output_tokens_source": resolved_metadata["max_output_tokens_source"],
        "max_output_tokens_verified": resolved_metadata["max_output_tokens_verified"],
        "default_reasoning_effort": resolved_metadata["default_reasoning_effort"],
        "default_reasoning_summary": resolved_metadata["default_reasoning_summary"],
        **_provider_request_material(provider_data),
        **image_fields,
    }


def get_custom_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the explicitly configured custom provider transport."""
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("custom", {})
    provider_data = raw if isinstance(raw, dict) else {}

    base_url = str(provider_data.get("base_url") or os.getenv("CUSTOM_BASE_URL", "")).strip()
    api_key = _custom_provider_api_key(base_url)
    model = str(provider_data.get("model") or os.getenv("CUSTOM_MODEL", "")).strip()
    small_fast_model = str(
        provider_data.get("small_fast_model")
        or os.getenv("CUSTOM_SMALL_FAST_MODEL", "")
        or os.getenv("MINICODE_SMALL_FAST_MODEL", "")
    ).strip()
    reasoning_effort = str(
        provider_data.get("reasoning_effort") or os.getenv("CUSTOM_REASONING_EFFORT", "")
    ).strip()
    wire_api = normalize_custom_wire_api(
        base_url,
        str(provider_data.get("wire_api") or os.getenv("CUSTOM_WIRE_API", "chat")),
        "chat",
    )
    responses_reasoning_summary = str(
        provider_data.get("responses_reasoning_summary")
        or os.getenv("CUSTOM_RESPONSES_REASONING_SUMMARY", "off")
    ).strip()
    custom_default_max_tokens = (
        MINICODE_CAPPED_DEFAULT_MAX_TOKENS if wire_api == "anthropic" else 0
    )
    max_tokens = max(
        0,
        _coerce_int(
            provider_data.get(
                "max_tokens",
                os.getenv("CUSTOM_MAX_TOKENS", str(custom_default_max_tokens)),
            ),
            custom_default_max_tokens,
        ),
    )
    if wire_api == "anthropic" and max_tokens <= 0:
        max_tokens = MINICODE_CAPPED_DEFAULT_MAX_TOKENS
    responses_defaults_enabled = wire_api == "responses"
    custom_prompt_cache_retention_default = _responses_prompt_cache_retention_default(
        responses_defaults_enabled,
        "custom",
    )
    prompt_cache_retention = _normalize_prompt_cache_retention(
        provider_data.get(
            "prompt_cache_retention",
            custom_prompt_cache_retention_default,
        ),
        custom_prompt_cache_retention_default,
    )
    thinking_budget = _coerce_int(
        provider_data.get("thinking_budget", os.getenv("CUSTOM_THINKING_BUDGET", "0")),
        0,
    )

    available_models = _coerce_model_list(provider_data.get("available_models"))
    model = _select_custom_model(model, available_models)
    if model and model not in available_models:
        available_models.insert(0, model)
    models_source = str(provider_data.get("models_source") or "").strip()
    model_metadata = _coerce_model_metadata(provider_data.get("model_metadata"))
    model_labels = _coerce_model_labels(provider_data.get("model_labels"))
    resolved_metadata = get_provider_model_metadata(
        {
            "model": model,
            "model_metadata": model_metadata,
            "reasoning_effort_levels": provider_data.get("reasoning_effort_levels"),
        },
        model,
    )
    from backend.llm.reasoning_effort import reasoning_effort_levels as resolve_effort_levels

    reasoning_effort_levels = list(
        resolve_effort_levels(
            model,
            wire_api,
            resolved_metadata["reasoning_effort_levels"],
        )
    )
    reasoning_projection = _reasoning_effort_projection(
        model=model,
        wire_api=wire_api,
        configured=reasoning_effort,
        levels=reasoning_effort_levels,
        default_effort=resolved_metadata["default_reasoning_effort"],
    )
    image_fields = _provider_image_fields(
        "custom",
        provider_data,
    )

    return {
        "display_name": _provider_display_name(provider_data),
        "api_key": api_key,
        "base_url": (_normalize_openai_base_url(base_url) if base_url and wire_api != "anthropic" else base_url),
        "model": model,
        "small_fast_model": small_fast_model,
        "available_models": available_models,
        "models_source": models_source,
        "model_metadata": model_metadata,
        "model_labels": model_labels,
        "reasoning_effort": reasoning_effort,
        **reasoning_projection,
        "responses_reasoning_summary": responses_reasoning_summary,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
        "wire_api": wire_api,
        "proxy_mode": _provider_proxy_mode("custom", provider_data),
        "reasoning_effort_levels": reasoning_effort_levels,
        "prompt_cache_retention": prompt_cache_retention,
        "context_window": resolved_metadata["context_window"],
        "context_window_source": resolved_metadata["context_window_source"],
        "context_window_verified": resolved_metadata["context_window_verified"],
        "max_context_window": resolved_metadata["max_context_window"],
        "max_context_window_source": resolved_metadata["max_context_window_source"],
        "max_context_window_verified": resolved_metadata["max_context_window_verified"],
        "max_output_tokens": resolved_metadata["max_output_tokens"],
        "max_output_tokens_source": resolved_metadata["max_output_tokens_source"],
        "max_output_tokens_verified": resolved_metadata["max_output_tokens_verified"],
        "default_reasoning_effort": resolved_metadata["default_reasoning_effort"],
        "default_reasoning_summary": resolved_metadata["default_reasoning_summary"],
        **_provider_request_material(provider_data),
        **image_fields,
    }



def get_image_generation_settings(
    provider: str | None = None,
    settings_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve image capability from the active Provider profile.

    New profiles use the Provider's own URL and key. The legacy ``custom``
    branch remains readable so existing settings keep working, but the UI no
    longer creates a second image endpoint.
    """

    selected_provider = _normalize_provider(provider or get_llm_provider(settings_data))
    if selected_provider == "anthropic":
        section = get_anthropic_settings(settings_data)
        inherited_wire_api = "anthropic"
    elif selected_provider == "custom":
        section = get_custom_settings(settings_data)
        inherited_wire_api = str(section.get("wire_api") or "chat").strip().lower()
    else:
        section = get_openai_settings(settings_data)
        inherited_wire_api = str(section.get("wire_api") or "responses").strip().lower()

    mode = _normalize_image_mode(section.get("image_mode"))
    configured_model = str(section.get("image_model") or "").strip()
    primary_model = str(section.get("model") or "").strip()
    if not configured_model:
        from backend.llm.capabilities import is_gpt_image_model

        configured_model = primary_model if is_gpt_image_model(primary_model) else ""
        if not configured_model:
            for candidate in _coerce_model_list(section.get("available_models")):
                if is_gpt_image_model(candidate):
                    configured_model = candidate
                    break

    if mode == "disabled":
        return {
            "enabled": False,
            "mode": mode,
            "provider": selected_provider,
            "proxy_mode": _normalize_proxy_mode(section.get("proxy_mode")),
            "reason": "Image generation is disabled for this provider profile.",
            "base_url": "",
            "api_key": "",
            "model": configured_model,
            "size": str(section.get("image_size") or "1024x1024"),
            "quality": str(section.get("image_quality") or ""),
            "default_headers": tuple(section.get("default_headers") or ()),
            "auth_header": bool(section.get("auth_header", False)),
        }

    if mode == "custom":
        base_url = str(section.get("image_base_url") or "").strip()
        api_key = str(section.get("image_api_key") or "").strip()
        auth_header = bool(section.get("auth_header", False))
        reason = ""
        if not base_url:
            reason = "Independent image generation requires an image base URL."
        elif auth_header and not api_key:
            reason = "auth_header=true requires an image API key."
        elif not configured_model:
            reason = "Independent image generation requires an image model."
    else:
        base_url = str(section.get("base_url") or "").strip()
        api_key = str(section.get("api_key") or "").strip()
        auth_header = bool(section.get("auth_header", False))
        reason = ""
        if inherited_wire_api not in {"chat", "responses"}:
            reason = (
                "The current provider uses Anthropic Messages; configure an "
                "independent OpenAI-compatible image channel."
            )
        elif not base_url:
            reason = "Inherited image generation requires the provider base URL."
        elif auth_header and not api_key:
            reason = "auth_header=true requires the provider API key."
        elif not configured_model:
            reason = "Set an image model before using image generation."

    return {
        "enabled": not reason,
        "mode": mode,
        "provider": selected_provider,
        "proxy_mode": _normalize_proxy_mode(section.get("proxy_mode")),
        "reason": reason,
        "base_url": base_url,
        "api_key": api_key,
        "model": configured_model,
        "size": _normalize_image_size(section.get("image_size")),
        "quality": _normalize_image_quality(section.get("image_quality")),
        "default_headers": tuple(section.get("default_headers") or ()),
        "auth_header": auth_header,
    }



def _load_settings_json() -> dict:
    """Read the writable user layer without merging project or managed data."""
    with file_mutation_locks([SETTINGS_FILE]):
        if not SETTINGS_FILE.exists():
            return {}
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise SettingsError(f"{SETTINGS_FILE} must contain a JSON object")
        return value


def load_config_layer_stack(
    *,
    session_flags: Mapping[str, Any] | None = None,
    cwd: Path | None = None,
    requirements_path: Path | None = None,
    managed_settings_dir: Path | None = None,
    managed_settings_result: Any | None = None,
    remote_managed_settings: Mapping[str, Any] | None = None,
) -> "ConfigLayerStack":
    """Materialize MiniCode config precedence and managed constraints."""
    from backend.config_layers import ConfigLayer, ConfigLayerSource, load_config_layers_state
    from backend.config_requirements import RequirementSource, RequirementsLayerEntry
    from backend.managed_settings import (
        load_minicode_managed_settings,
        normalize_minicode_policy_requirements,
    )
    from backend.workspace.state import get_explicit_active_workspace_root

    workspace_root = cwd if cwd is not None else get_explicit_active_workspace_root()
    requirements_override = str(os.environ.get("MINICODE_REQUIREMENTS_FILE") or "").strip()
    managed_dir_override = str(
        os.environ.get("MINICODE_MANAGED_SETTINGS_DIR") or ""
    ).strip()
    managed = managed_settings_result or load_minicode_managed_settings(
        managed_settings_dir
        or (Path(managed_dir_override) if managed_dir_override else None),
        remote_settings=remote_managed_settings,
    )
    policy_layers: list[ConfigLayer] = []
    managed_requirement_layers: list[RequirementsLayerEntry] = []
    if managed.configured:
        source_id = managed.source_kind or "minicode-policy"
        policy_layers.append(
            ConfigLayer(
                ConfigLayerSource(
                    "policy",
                    file=managed.source_location,
                    source_id=source_id,
                    name="MiniCode managed settings",
                ),
                managed.settings,
            )
        )
        normalized_requirements = normalize_minicode_policy_requirements(managed.settings)
        if normalized_requirements:
            managed_requirement_layers.append(
                RequirementsLayerEntry(
                    RequirementSource(
                        "enterprise_managed",
                        location=managed.source_location,
                        name="MiniCode managed settings",
                        source_id=source_id,
                    ),
                    normalized_requirements,
                    (
                        Path(managed.source_location.split(";", 1)[0]).parent
                        if managed.source_kind == "managed_files"
                        and managed.source_location
                        else None
                    ),
                )
            )
    return load_config_layers_state(
        state_root=STATE_ROOT,
        user_config_file=SETTINGS_FILE,
        cwd=workspace_root,
        session_flags=session_flags,
        requirements_path=(
            requirements_path
            or (Path(requirements_override) if requirements_override else None)
        ),
        policy_config_layers=policy_layers,
        enterprise_requirements_layers=managed_requirement_layers,
        startup_warnings=managed.validation_errors,
        managed_policy_errors=managed.validation_errors,
    )


def _load_effective_settings_json(
    *,
    session_flags: Mapping[str, Any] | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    return load_config_layer_stack(session_flags=session_flags, cwd=cwd).effective_config()



def _normalize_openai_base_url(base_url: str) -> str:
    """Normalize OpenAI-compatible gateway URLs to include a version path."""
    if not base_url:
        return base_url

    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if path:
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))


def get_llm_settings_payload(
    settings_data: dict[str, Any] | None = None,
    *,
    include_api_keys: bool = False,
) -> dict[str, Any]:
    openai = get_openai_settings(settings_data)
    anthropic = get_anthropic_settings(settings_data)
    custom = get_custom_settings(settings_data)
    provider = get_llm_provider(settings_data)
    active_by_provider = {
        "anthropic": anthropic["model"],
        "custom": custom["model"],
        "openai": openai["model"],
    }

    return {
        "provider": provider,
        "providers": ["openai", "anthropic", "custom"],
        "openai": {
            "display_name": openai["display_name"],
            "has_api_key": bool(openai["api_key"]),
            "api_key": openai["api_key"] if include_api_keys else "",
            "headers": dict(openai["default_headers"]),
            "auth_header": openai["auth_header"],
            "base_url": openai["base_url"],
            "model": openai["model"],
            "small_fast_model": openai["small_fast_model"],
            "available_models": openai["available_models"],
            "models_source": openai.get("models_source", ""),
            "model_metadata": openai.get("model_metadata", {}),
            "model_labels": openai.get("model_labels", {}),
            "reasoning_effort": openai["reasoning_effort"],
            "configured_reasoning_effort": openai["configured_reasoning_effort"],
            "effective_reasoning_effort": openai["effective_reasoning_effort"],
            "reasoning_effort_supported": openai["reasoning_effort_supported"],
            "responses_reasoning_summary": openai["responses_reasoning_summary"],
            "max_tokens": openai["max_tokens"],
            "wire_api": openai["wire_api"],
            "proxy_mode": openai["proxy_mode"],
            "prompt_cache_retention": openai["prompt_cache_retention"],
            "reasoning_effort_levels": openai["reasoning_effort_levels"],
            "context_window": openai["context_window"],
            "context_window_source": openai["context_window_source"],
            "context_window_verified": openai["context_window_verified"],
            "max_context_window": openai["max_context_window"],
            "max_context_window_source": openai["max_context_window_source"],
            "max_context_window_verified": openai["max_context_window_verified"],
            "max_output_tokens": openai["max_output_tokens"],
            "max_output_tokens_source": openai["max_output_tokens_source"],
            "max_output_tokens_verified": openai["max_output_tokens_verified"],
            "default_reasoning_effort": openai["default_reasoning_effort"],
            "default_reasoning_summary": openai["default_reasoning_summary"],
            "image_mode": openai["image_mode"],
            "has_image_api_key": bool(openai["has_image_api_key"]),
            "image_api_key": openai.get("image_api_key", "") if include_api_keys else "",
            "image_base_url": openai["image_base_url"],
            "image_model": openai["image_model"],
            "image_size": openai["image_size"],
            "image_quality": openai["image_quality"],
        },
        "anthropic": {
            "display_name": anthropic["display_name"],
            "has_api_key": bool(anthropic["api_key"]),
            "api_key": anthropic["api_key"] if include_api_keys else "",
            "headers": dict(anthropic["default_headers"]),
            "auth_header": anthropic["auth_header"],
            "base_url": anthropic["base_url"],
            "model": anthropic["model"],
            "small_fast_model": anthropic["small_fast_model"],
            "available_models": anthropic["available_models"],
            "models_source": anthropic.get("models_source", ""),
            "model_metadata": anthropic.get("model_metadata", {}),
            "model_labels": anthropic.get("model_labels", {}),
            "max_tokens": anthropic["max_tokens"],
            "thinking_budget": anthropic["thinking_budget"],
            "proxy_mode": anthropic["proxy_mode"],
            "configured_reasoning_effort": "",
            "effective_reasoning_effort": "",
            "reasoning_effort_supported": False,
            "context_window": anthropic["context_window"],
            "context_window_source": anthropic["context_window_source"],
            "context_window_verified": anthropic["context_window_verified"],
            "max_context_window": anthropic["max_context_window"],
            "max_context_window_source": anthropic["max_context_window_source"],
            "max_context_window_verified": anthropic["max_context_window_verified"],
            "max_output_tokens": anthropic["max_output_tokens"],
            "max_output_tokens_source": anthropic["max_output_tokens_source"],
            "max_output_tokens_verified": anthropic["max_output_tokens_verified"],
            "default_reasoning_effort": anthropic["default_reasoning_effort"],
            "default_reasoning_summary": anthropic["default_reasoning_summary"],
            "image_mode": anthropic["image_mode"],
            "has_image_api_key": bool(anthropic["has_image_api_key"]),
            "image_api_key": anthropic.get("image_api_key", "") if include_api_keys else "",
            "image_base_url": anthropic["image_base_url"],
            "image_model": anthropic["image_model"],
            "image_size": anthropic["image_size"],
            "image_quality": anthropic["image_quality"],
        },
        "custom": {
            "display_name": custom["display_name"],
            "has_api_key": bool(custom["api_key"]),
            "api_key": custom["api_key"] if include_api_keys else "",
            "headers": dict(custom["default_headers"]),
            "auth_header": custom["auth_header"],
            "base_url": custom["base_url"],
            "model": custom["model"],
            "small_fast_model": custom["small_fast_model"],
            "available_models": custom["available_models"],
            "models_source": custom.get("models_source", ""),
            "model_metadata": custom.get("model_metadata", {}),
            "model_labels": custom.get("model_labels", {}),
            "reasoning_effort": custom["reasoning_effort"],
            "configured_reasoning_effort": custom["configured_reasoning_effort"],
            "effective_reasoning_effort": custom["effective_reasoning_effort"],
            "reasoning_effort_supported": custom["reasoning_effort_supported"],
            "responses_reasoning_summary": custom["responses_reasoning_summary"],
            "max_tokens": custom["max_tokens"],
            "thinking_budget": custom["thinking_budget"],
            "wire_api": custom["wire_api"],
            "proxy_mode": custom["proxy_mode"],
            "prompt_cache_retention": custom["prompt_cache_retention"],
            "reasoning_effort_levels": custom["reasoning_effort_levels"],
            "context_window": custom["context_window"],
            "context_window_source": custom["context_window_source"],
            "context_window_verified": custom["context_window_verified"],
            "max_context_window": custom["max_context_window"],
            "max_context_window_source": custom["max_context_window_source"],
            "max_context_window_verified": custom["max_context_window_verified"],
            "max_output_tokens": custom["max_output_tokens"],
            "max_output_tokens_source": custom["max_output_tokens_source"],
            "max_output_tokens_verified": custom["max_output_tokens_verified"],
            "default_reasoning_effort": custom["default_reasoning_effort"],
            "default_reasoning_summary": custom["default_reasoning_summary"],
            "image_mode": custom["image_mode"],
            "has_image_api_key": bool(custom["has_image_api_key"]),
            "image_api_key": custom.get("image_api_key", "") if include_api_keys else "",
            "image_base_url": custom["image_base_url"],
            "image_model": custom["image_model"],
            "image_size": custom["image_size"],
            "image_quality": custom["image_quality"],
        },
        "provider_history": _llm_history(settings_data, include_api_keys=include_api_keys),
        "active_model": active_by_provider.get(provider, openai["model"]),
    }


@_serialized_settings_update

def load_llm_settings(settings_data: dict[str, Any] | None = None) -> LLMSettings:
    """从环境变量与 settings.json 加载 LLM 配置。"""
    active_provider = get_llm_provider(settings_data)

    if active_provider == "anthropic":
        anthropic = get_anthropic_settings(settings_data)
        if anthropic["auth_header"] and not anthropic["api_key"]:
            raise SettingsError(
                "auth_header=true requires an API key for the selected Anthropic provider"
            )
        return LLMSettings(
            api_key=anthropic["api_key"],
            provider="anthropic",
            base_url=anthropic["base_url"],
            model=anthropic["model"],
            small_fast_model=anthropic["small_fast_model"],
            reasoning_effort="",
            responses_reasoning_summary="",
            max_tokens=anthropic["max_tokens"],
            wire_api="anthropic",
            proxy_mode=anthropic["proxy_mode"],
            prompt_cache_retention="",
            reasoning_effort_levels=(),
            context_window=anthropic["context_window"],
            context_window_source=anthropic["context_window_source"],
            context_window_verified=anthropic["context_window_verified"],
            max_context_window=anthropic["max_context_window"],
            max_context_window_source=anthropic["max_context_window_source"],
            max_context_window_verified=anthropic["max_context_window_verified"],
            max_output_tokens=anthropic["max_output_tokens"],
            max_output_tokens_source=anthropic["max_output_tokens_source"],
            max_output_tokens_verified=anthropic["max_output_tokens_verified"],
            default_reasoning_effort=anthropic["default_reasoning_effort"],
            default_reasoning_summary=anthropic["default_reasoning_summary"],
            default_headers=tuple(anthropic["default_headers"]),
            auth_header=bool(anthropic["auth_header"]),
        )

    if active_provider == "custom":
        custom = get_custom_settings(settings_data)
        from backend.llm.capabilities import is_gpt_image_model

        image_config = (
            get_image_generation_settings("custom", settings_data)
            if is_gpt_image_model(str(custom.get("model") or ""))
            else None
        )
        if custom["auth_header"] and not custom["api_key"]:
            raise SettingsError(
                "auth_header=true requires an API key for the selected custom provider"
            )
        if image_config is not None and not image_config["enabled"]:
            raise SettingsError(str(image_config["reason"]))
        return LLMSettings(
            api_key=str(image_config["api_key"] if image_config else custom["api_key"]),
            provider="custom",
            base_url=str(image_config["base_url"] if image_config else custom["base_url"]),
            model=str(custom["model"]),
            small_fast_model=custom["small_fast_model"],
            reasoning_effort=custom["reasoning_effort"],
            responses_reasoning_summary=custom["responses_reasoning_summary"],
            max_tokens=custom["max_tokens"],
            wire_api="chat" if image_config else custom["wire_api"],
            proxy_mode=custom["proxy_mode"],
            prompt_cache_retention=custom["prompt_cache_retention"],
            reasoning_effort_levels=tuple(custom.get("reasoning_effort_levels") or ()),
            context_window=custom["context_window"],
            context_window_source=custom["context_window_source"],
            context_window_verified=custom["context_window_verified"],
            max_context_window=custom["max_context_window"],
            max_context_window_source=custom["max_context_window_source"],
            max_context_window_verified=custom["max_context_window_verified"],
            max_output_tokens=custom["max_output_tokens"],
            max_output_tokens_source=custom["max_output_tokens_source"],
            max_output_tokens_verified=custom["max_output_tokens_verified"],
            default_reasoning_effort=custom["default_reasoning_effort"],
            default_reasoning_summary=custom["default_reasoning_summary"],
            default_headers=tuple(custom["default_headers"]),
            auth_header=bool(custom["auth_header"]),
            image_model=str(image_config["model"] if image_config else ""),
            image_size=str(image_config["size"] if image_config else custom["image_size"]),
            image_quality=str(image_config["quality"] if image_config else custom["image_quality"]),
        )

    openai = get_openai_settings(settings_data)
    from backend.llm.capabilities import is_gpt_image_model

    image_config = (
        get_image_generation_settings("openai", settings_data)
        if is_gpt_image_model(str(openai.get("model") or ""))
        else None
    )
    if openai["auth_header"] and not openai["api_key"]:
        raise SettingsError(
            "auth_header=true requires an API key for the selected OpenAI provider"
        )
    if image_config is not None and not image_config["enabled"]:
        raise SettingsError(str(image_config["reason"]))
    return LLMSettings(
        api_key=str(image_config["api_key"] if image_config else openai["api_key"]),
        provider="openai",
        base_url=str(image_config["base_url"] if image_config else openai["base_url"]),
        model=str(openai["model"]),
        small_fast_model=openai["small_fast_model"],
        reasoning_effort=openai["reasoning_effort"],
        responses_reasoning_summary=openai["responses_reasoning_summary"],
        max_tokens=openai["max_tokens"],
        wire_api=openai["wire_api"],
        proxy_mode=openai["proxy_mode"],
        prompt_cache_retention=openai["prompt_cache_retention"],
        reasoning_effort_levels=tuple(openai.get("reasoning_effort_levels") or ()),
        context_window=openai["context_window"],
        context_window_source=openai["context_window_source"],
        context_window_verified=openai["context_window_verified"],
        max_context_window=openai["max_context_window"],
        max_context_window_source=openai["max_context_window_source"],
        max_context_window_verified=openai["max_context_window_verified"],
        max_output_tokens=openai["max_output_tokens"],
        max_output_tokens_source=openai["max_output_tokens_source"],
        max_output_tokens_verified=openai["max_output_tokens_verified"],
        default_reasoning_effort=openai["default_reasoning_effort"],
        default_reasoning_summary=openai["default_reasoning_summary"],
        default_headers=tuple(openai["default_headers"]),
        auth_header=bool(openai["auth_header"]),
        image_model=str(image_config["model"] if image_config else ""),
        image_size=str(image_config["size"] if image_config else openai["image_size"]),
        image_quality=str(image_config["quality"] if image_config else openai["image_quality"]),
    )



