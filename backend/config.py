"""MiniCode configuration projection over a MiniCode-style layer stack."""

from __future__ import annotations

import json
import logging
import math
import os
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.feature_flags import FeatureFlags, coerce_feature_bool, load_feature_flags
from backend.llm.model_catalog import responses_model_catalog_entry
from backend.llm.proxy_policy import normalize_provider_proxy_mode

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.agent.policies import (
        StreamRetryPolicy,
    )
    from backend.config_layers import ConfigLayerStack

# ── 项目根目录（backend/ 的父目录）──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = Path(os.environ.get("MINICODE_STATE_ROOT") or PROJECT_ROOT).expanduser().resolve()
DATA_ROOT = STATE_ROOT / "data"

# ── 自动加载 .env ─────────────────────────────────────────────
def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = False
    lexer.commenters = "#"
    try:
        parts = list(lexer)
    except ValueError:
        parts = [raw_value.strip()]
    value = os.path.expandvars("".join(parts).strip())
    return key, value


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            parsed = _parse_env_assignment(line)
            if parsed is None:
                continue
            key, value = parsed
            os.environ.setdefault(key, value)


_load_env_file(PROJECT_ROOT / ".env")
_load_env_file(PROJECT_ROOT / "backend" / ".env")

# ── settings.json 路径 ─────────────────────────────────────────
SETTINGS_FILE = STATE_ROOT / "settings.json"
_SETTINGS_WRITE_LOCK = threading.RLock()


class SettingsError(RuntimeError):
    """配置加载失败时抛出。"""


# MiniCode's Messages API requires max_tokens and uses this capped default;
# OpenAI Responses/Chat APIs leave the field out when the user did not set it.
MINICODE_CAPPED_DEFAULT_MAX_TOKENS = 8_000


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
    wire_api: str = "chat"  # "responses", "chat", or "anthropic"
    proxy_mode: str = "inherit"  # "inherit" or "direct"
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
    # MiniCode ProviderConfig headers are resolved by the session ModelRuntime and
    # carried as immutable adapter defaults. Tuple storage keeps the frozen
    # settings value deterministic for cache identities.
    default_headers: tuple[tuple[str, str], ...] = ()
    auth_header: bool = False
    # When the selected chat model is a dedicated image model, keep its
    # identity in ``model`` so capability/routing checks remain correct while
    # allowing an independent Images endpoint to use a provider-specific model
    # id (for example ``flux-*``) on the wire.
    image_model: str = ""
    image_size: str = "1024x1024"
    image_quality: str = ""


# MiniCode's default when a provider does not publish a context window.
MODEL_CONTEXT_WINDOW_DEFAULT = 200_000


@dataclass(frozen=True)
class ContextWindowResolution:
    tokens: int
    source: str
    verified: bool

# Other established model-family windows, matched longest-prefix-first. OpenAI
# GPT-5 records deliberately stay out of this table because MiniCode publishes
# them as exact catalog entries with materially different default/max windows.
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


def resolve_context_window(model: str) -> int:
    """Backward-compatible numeric context-window resolver."""

    return resolve_context_window_details(model).tokens


@dataclass(frozen=True)
class TokenBudget:
    """Context accounting plus MiniCode's response reserve."""

    total: int = MODEL_CONTEXT_WINDOW_DEFAULT
    system_prompt: int = 2_000
    active_skills: int = 4_000
    memory_index: int = 1_000
    tool_schemas: int = 6_000
    agent_state: int = 2_000
    response_reserve: int = 16_384

    @property
    def history_budget(self) -> int:
        """对话历史可用预算 = 总量 - 其他组件 - 响应预留。"""
        used = (
            self.system_prompt
            + self.active_skills
            + self.memory_index
            + self.tool_schemas
            + self.agent_state
            + self.response_reserve
        )
        return self.total - used


@dataclass(frozen=True)
class PermissionSettings:
    """工具权限配置。"""

    auto_allow: list[str] = field(
        default_factory=lambda: [
            "read_file",
            "list_files",
            "grep_files",
            "glob_files",
            "ask_user",
            "read_artifact",
            "memory_list",
            "memory_read",
            "memory_search",
            "memory_add_ad_hoc_note",
            "tool_search",
            "go_to_definition",
            "find_references",
            "git_status",
            "git_diff",
            "web_search",
        ]
    )
    require_confirm: list[str] = field(
        default_factory=lambda: [
            "run_command",
            "terminal_*",
            "git_commit",
            "git_push",
            "git_stage_*",
            "git_unstage_*",
            "worktree_*",
            "web_fetch",
            "mcp__*",
        ]
    )
    require_diff_review: list[str] = field(
        default_factory=lambda: [
            "write_file",
            "edit_file",
            "apply_patch",
            "save_*",
        ]
    )
    always_deny: list[str] = field(default_factory=list)
    path_allowlist: list[str] = field(default_factory=lambda: ["."])
    path_denylist: list[str] = field(
        default_factory=lambda: [
            ".env",
            # Per-environment dotenv variants (.env.staging, .env.ci, …) carry
            # the same secrets as .env, which the bare pattern does not cover.
            ".env.*",
            # Conventional templates document variable names and are meant to be
            # committed, so they stay readable. Keep the exceptions exact.
            "!.env.example",
            "!.env.sample",
            "!.env.template",
            "!.env.dist",
            ".mcp.json",
            "settings.json",
            ".git/**",
            "*.key",
            "*.pem",
            "secrets/",
        ]
    )
    # Content-level rules in Tool(content) syntax, e.g. run_command(npm run:*), edit_file(src/**).
    # allow rules force AUTO (in non-plan modes); deny rules ALWAYS_DENY in every
    # mode (safety). Parsed by permissions.content_rules.
    content_allow_rules: list[str] = field(default_factory=list)
    content_ask_rules: list[str] = field(default_factory=list)
    content_deny_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentSettings:
    """Agent Loop 运行参数。"""

    # Optional host-owned limits. Zero follows MiniCode session behavior: the
    # agent loop itself does not invent task-size or cost thresholds.
    max_iterations: int = 0
    max_tool_calls: int = 0
    turn_error_budget: int = 0
    max_turn_tokens: int = 0
    max_turn_cost_usd: float = 0.0
    max_turn_seconds: float = 0.0
    compaction_keep_recent_tokens: int = 20_000
    agent_mode: str = "react"

    # Stream assistant text live (token-by-token) instead of buffering and
    # emitting once at turn end. When enabled, agent_message.delta events are
    # yielded during the provider's explicit final-answer phase and the
    # authoritative text arrives in item.completed. Set false for final-only
    # output.
    live_text_streaming: bool = True
    # MiniCode uses a five-minute HTTP/stream idle timeout by default.
    # MiniCode's 90s watchdog is opt-in behind MINICODE_ENABLE_STREAM_WATCHDOG.
    stream_timeout_seconds: float = 300.0
    first_byte_timeout_seconds: float = 0.0
    # MiniCode retries streams 10 times with 500ms base and ±25% jitter
    # (cc/src/services/api/withRetry.ts: DEFAULT_MAX_RETRIES=10, BASE_DELAY_MS=500).
    stream_max_attempts: int = 10
    stream_retry_delay_seconds: float = 0.5
    stream_retryable_substrings: tuple[str, ...] = ()
    # Deprecated compatibility fields. Iteration budgets are explicit and are
    # never inferred from request text, tool signatures, or output heuristics.

    # Policy slots — None means the Loop_Core fills in the default implementation
    stream_retry_policy: "StreamRetryPolicy | None" = None
    # MiniCode's time-based microcompact contract.  It is intentionally
    # opt-in: when enabled, a main-thread request whose previous assistant
    # response is at least the configured cache TTL old clears stale,
    # compactable tool-result bodies while preserving their protocol shape.
    # These fields are appended after the original positional fields so older
    # integrations constructing AgentSettings positionally remain compatible.
    time_based_microcompact_enabled: bool = False
    time_based_microcompact_gap_threshold_minutes: int = 60
    time_based_microcompact_keep_recent: int = 5
    # MiniCode waits indefinitely for approvals and applies no timeout; a
    # numeric value here is an explicit opt-in for deployments that want one.
    approval_timeout_seconds: float | None = None

@dataclass(frozen=True)
class UISettings:
    """UI appearance and behavior settings."""

    default_font_size: str = "base"  # xs, sm, base, md, lg
    default_code_font_size: str = "sm"
    default_sidebar_width: int = 280
    default_compact_mode: bool = False
    enable_animations: bool = True
    enable_sound_effects: bool = False


@dataclass
class AppConfig:
    """应用总配置。"""

    llm: LLMSettings = field(default_factory=LLMSettings)
    token_budget: TokenBudget = field(default_factory=TokenBudget)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    ui: UISettings = field(default_factory=UISettings)
    feature_flags: FeatureFlags = field(default_factory=FeatureFlags)
    config_layer_stack: "ConfigLayerStack | None" = None


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


def _scoped_vault_name(provider: str, base_url: str) -> str:
    scope = _provider_key_scope(base_url)
    if not scope:
        return ""
    prefix = {
        "anthropic": "ANTHROPIC_API_KEY",
        "custom": "CUSTOM_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider, "OPENAI_API_KEY")
    return f"{prefix}_{scope}"


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


def _set_runtime_image_api_key(provider: str, api_key: str, base_url: str) -> None:
    """Persist an independent Images API key without replacing the text key."""

    if not api_key:
        return
    scoped_names = _image_scoped_vault_names(provider, base_url)
    if not scoped_names:
        raise SettingsError("An independent image API key requires an image base URL.")
    runtime_key = _normalize_provider(provider)
    _RUNTIME_IMAGE_API_KEY_SCOPES[runtime_key] = _provider_key_scope(base_url)
    for name in scoped_names:
        os.environ[name] = api_key
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        host = urlsplit(base_url).netloc or base_url
        for name in scoped_names:
            vault.set(
                name,
                api_key,
                description=f"{runtime_key} image provider API key for {host}",
                scope="global",
            )
    except Exception as exc:
        logger.debug("vault image API key write failed for %s: %s", runtime_key, exc)


def _image_api_key_for_base_url(provider: str, base_url: str) -> str:
    for name in _image_scoped_vault_names(provider, base_url):
        value = os.getenv(name, "").strip() or _vault_api_key(name).strip()
        if _is_api_key_replacement(value):
            return value
    return ""


def _clear_scoped_runtime_image_api_key(provider: str, base_url: str) -> None:
    names = _image_scoped_vault_names(provider, base_url)
    for name in names:
        os.environ.pop(name, None)
    runtime_key = _normalize_provider(provider)
    if _RUNTIME_IMAGE_API_KEY_SCOPES.get(runtime_key) == _provider_key_scope(base_url):
        _RUNTIME_IMAGE_API_KEY_SCOPES.pop(runtime_key, None)
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for name in names:
            vault.delete(name)
    except Exception as exc:
        logger.debug("vault image API key clear failed for %s: %s", runtime_key, exc)


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


def _set_runtime_api_key(provider: str, api_key: str, base_url: str = "") -> None:
    if not api_key:
        _clear_runtime_api_key(provider, base_url)
        return
    if provider == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = api_key
        vault_name = "ANTHROPIC_API_KEY"
    elif provider == "custom":
        os.environ["CUSTOM_API_KEY"] = api_key
        vault_name = "CUSTOM_API_KEY"
    else:
        os.environ["OPENAI_API_KEY"] = api_key
        vault_name = "OPENAI_API_KEY"
    try:
        from backend.vault import EnvVault

        EnvVault().set(
            vault_name,
            api_key,
            description=f"{provider} provider API key",
            scope="global",
        )
    except Exception as exc:
        logger.debug("vault API key write failed for %s: %s", vault_name, exc)

    scoped_names = _scoped_vault_names(provider, base_url)
    if not scoped_names:
        return
    _RUNTIME_API_KEY_SCOPES[provider] = _provider_key_scope(base_url)
    for scoped_name in scoped_names:
        os.environ[scoped_name] = api_key
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for scoped_name in scoped_names:
            vault.set(
                scoped_name,
                api_key,
                description=f"{provider} provider API key for {urlsplit(base_url).netloc or base_url}",
                scope="global",
            )
    except Exception as exc:
        logger.debug("vault scoped API key write failed for %s: %s", provider, exc)


def _clear_runtime_api_key(provider: str, base_url: str = "") -> None:
    if provider == "anthropic":
        vault_name = "ANTHROPIC_API_KEY"
    elif provider == "custom":
        vault_name = "CUSTOM_API_KEY"
    else:
        vault_name = "OPENAI_API_KEY"
    os.environ.pop(vault_name, None)
    names = [vault_name]
    scoped_names = _scoped_vault_names(provider, base_url)
    names.extend(name for name in scoped_names if name not in names)
    for scoped_name in scoped_names:
        os.environ.pop(scoped_name, None)
    if not base_url or _RUNTIME_API_KEY_SCOPES.get(provider) == _provider_key_scope(base_url):
        _RUNTIME_API_KEY_SCOPES.pop(provider, None)
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for name in names:
            vault.delete(name)
    except Exception as exc:
        logger.debug("vault API key clear failed for %s: %s", vault_name, exc)


def _clear_scoped_runtime_api_key(provider: str, base_url: str = "") -> None:
    provider = _normalize_provider(provider)
    scoped_names = _scoped_vault_names(provider, base_url)
    if not scoped_names:
        return
    scoped_values = {
        value
        for scoped_name in scoped_names
        if _is_api_key_replacement(
            value := (
                os.getenv(scoped_name, "").strip()
                or _vault_api_key(scoped_name).strip()
            )
        )
    }
    global_name = {
        "anthropic": "ANTHROPIC_API_KEY",
        "custom": "CUSTOM_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(_normalize_provider(provider), "OPENAI_API_KEY")
    global_values = {
        value
        for value in (
            os.getenv(global_name, "").strip(),
            _vault_api_key(global_name).strip(),
        )
        if _is_api_key_replacement(value)
    }
    runtime_scope_matches = (
        _RUNTIME_API_KEY_SCOPES.get(provider) == _provider_key_scope(base_url)
    )
    clear_global_alias = runtime_scope_matches or bool(scoped_values & global_values)
    for scoped_name in scoped_names:
        os.environ.pop(scoped_name, None)
    if runtime_scope_matches:
        _RUNTIME_API_KEY_SCOPES.pop(provider, None)
    if clear_global_alias:
        os.environ.pop(global_name, None)
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for scoped_name in scoped_names:
            vault.delete(scoped_name)
        if clear_global_alias:
            vault.delete(global_name)
    except Exception as exc:
        logger.debug("vault scoped API key clear failed for %s: %s", provider, exc)


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


def _provider_api_key(provider: str) -> str:
    provider = _normalize_provider(provider)
    if provider == "anthropic":
        return _provider_api_key_for_base_url("anthropic", os.getenv("ANTHROPIC_BASE_URL", ""))
    if provider == "custom":
        return _custom_provider_api_key(os.getenv("CUSTOM_BASE_URL", ""))
    return _provider_api_key_for_base_url("openai", os.getenv("OPENAI_BASE_URL", ""))


def _merge_unique(values: list[str], required: list[str]) -> list[str]:
    merged: list[str] = []
    for value in [*values, *required]:
        item = str(value).strip()
        if item and item not in merged:
            merged.append(item)
    return merged


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return max(0, int(default))
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default))


def _coerce_nonnegative_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return max(0.0, float(default))
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return max(0.0, float(default))
    if not math.isfinite(parsed):
        return max(0.0, float(default))
    return max(0.0, parsed)


def _coerce_string_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = list(default)
    return [item for item in (str(raw).strip() for raw in values) if item]


_SUPPORTED_AGENT_MODES = {"react", "auto"}


def _normalize_agent_mode(value: Any) -> str:
    mode = str(value or "react").strip().lower() or "react"
    return mode if mode in _SUPPORTED_AGENT_MODES else "react"


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


def _next_model_metadata(
    updates: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    capabilities_match: bool,
) -> dict[str, dict[str, Any]]:
    """Persist submitted metadata, otherwise retain it only for the same target.

    Provider metadata belongs to one endpoint/model selection.  A model or
    endpoint change without a fresh catalog must not inherit the previous
    model's capabilities.
    """

    if "model_metadata" in updates:
        return _coerce_model_metadata(updates.get("model_metadata"))
    if capabilities_match:
        return _coerce_model_metadata(current.get("model_metadata"))
    return {}


def _next_image_settings(
    provider: str,
    updates: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    mode = _normalize_image_mode(
        updates.get("image_mode", current.get("image_mode", "inherit")),
    )
    base_url = str(
        updates.get("image_base_url", current.get("image_base_url", "")) or ""
    ).strip()
    normalized_base_url = _normalize_openai_base_url(base_url) if base_url else ""
    image_key_provided = "image_api_key" in updates
    image_key = str(updates.get("image_api_key") or "").strip()
    if image_key_provided and _is_api_key_replacement(image_key):
        _set_runtime_image_api_key(provider, image_key, normalized_base_url)
    return {
        "image_mode": mode,
        # Secrets live only in the endpoint-scoped vault.
        "image_api_key": "",
        "image_base_url": normalized_base_url,
        "image_model": str(
            updates.get("image_model", current.get("image_model", "")) or ""
        ).strip(),
        "image_size": _normalize_image_size(
            updates.get("image_size", current.get("image_size", "1024x1024")),
        ),
        "image_quality": _normalize_image_quality(
            updates.get("image_quality", current.get("image_quality", "")),
        ),
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


def provider_reasoning_effort_levels(
    provider: str,
    section: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Return the selected model's provider-declared efforts in catalog order."""

    normalized = _normalize_provider(provider)
    if normalized == "anthropic":
        return ()
    data = section if isinstance(section, dict) else {}
    default_wire_api = "responses" if normalized == "openai" else "chat"
    wire_api = str(data.get("wire_api") or default_wire_api).strip().lower()
    if normalized != "anthropic":
        wire_api = normalize_custom_wire_api(
            str(data.get("base_url") or ""),
            wire_api,
            default_wire_api,
        )
    else:
        wire_api = _normalize_wire_api(wire_api, default_wire_api)
    from backend.llm.reasoning_effort import reasoning_effort_levels

    model = str(data.get("model") or "")
    declared = get_provider_model_metadata(data, model)["reasoning_effort_levels"]

    return reasoning_effort_levels(
        model,
        wire_api,
        declared,
    )


def provider_supports_reasoning_effort(provider: str, section: dict[str, Any] | None) -> bool:
    """Return True when the selected model declares reasoning-effort support."""

    return bool(provider_reasoning_effort_levels(provider, section))


def active_provider_reasoning_effort_levels(
    payload: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    settings_payload = payload if isinstance(payload, dict) else get_llm_settings_payload()
    provider = str(settings_payload.get("provider") or get_llm_provider()).strip()
    section = settings_payload.get(_normalize_provider(provider))
    return provider_reasoning_effort_levels(
        provider,
        section if isinstance(section, dict) else None,
    )


def active_provider_supports_reasoning_effort(payload: dict[str, Any] | None = None) -> bool:
    return bool(active_provider_reasoning_effort_levels(payload))


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


def _update_settings_json(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply one read-modify-write transaction to the user settings layer."""

    with _SETTINGS_WRITE_LOCK:
        with file_mutation_locks([SETTINGS_FILE]):
            settings_data = _load_settings_json()
            mutator(settings_data)
            _write_settings_json(settings_data)
            return settings_data


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


def _upsert_llm_history(
    settings_data: dict[str, Any],
    provider: str,
    section: dict[str, Any],
) -> list[dict[str, Any]]:
    llm_data = settings_data.setdefault("llm", {})
    base_url = str(section.get("base_url") or "").strip()
    model = str(section.get("model") or "").strip()
    default_wire_api = "anthropic" if provider == "anthropic" else "responses" if provider == "openai" else "chat"
    wire_api = _history_identity(provider, base_url, str(section.get("wire_api") or default_wire_api))[2]
    if not (base_url or model):
        return _llm_history(settings_data)

    provider_id = _provider_id_for_history(provider, base_url, wire_api)
    key = _history_profile_identity(provider, base_url, wire_api)
    responses_defaults_enabled = wire_api == "responses"
    prompt_cache_retention_default = _responses_prompt_cache_retention_default(
        responses_defaults_enabled,
        provider,
    )
    next_entry = {
        "provider": provider,
        "provider_id": provider_id,
        "display_name": _provider_display_name(section),
        "base_url": base_url,
        "model": model,
        "small_fast_model": str(section.get("small_fast_model") or "").strip(),
        "available_models": _coerce_model_list(section.get("available_models")),
        "models_source": str(section.get("models_source") or "").strip(),
        "model_metadata": _coerce_model_metadata(section.get("model_metadata")),
        "wire_api": wire_api,
        "proxy_mode": _normalize_proxy_mode(section.get("proxy_mode")),
        "reasoning_effort": str(section.get("reasoning_effort") or "").strip().lower(),
        "responses_reasoning_summary": str(section.get("responses_reasoning_summary") or "off").strip(),
        "max_tokens": max(0, _coerce_int(section.get("max_tokens", 0), 0)),
        "prompt_cache_retention": _normalize_prompt_cache_retention(
            section.get("prompt_cache_retention", prompt_cache_retention_default),
            prompt_cache_retention_default,
        ),
        "reasoning_effort_levels": _coerce_model_list(section.get("reasoning_effort_levels")),
        "thinking_budget": _coerce_int(section.get("thinking_budget", 0), 0),
        "image_mode": _normalize_image_mode(section.get("image_mode")),
        "has_image_api_key": bool(
            _image_api_key_for_base_url(
                provider,
                str(section.get("image_base_url") or "").strip(),
            )
        ),
        "image_api_key": "",
        "image_base_url": str(section.get("image_base_url") or "").strip(),
        "image_model": str(section.get("image_model") or "").strip(),
        "image_size": _normalize_image_size(section.get("image_size")),
        "image_quality": _normalize_image_quality(section.get("image_quality")),
        "has_api_key": bool(_provider_api_key_for_base_url(provider, base_url)),
        "updated_at": time.time(),
    }

    merged: list[dict[str, Any]] = [next_entry]
    for entry in _llm_history(settings_data):
        entry_key = _history_profile_identity(
            str(entry.get("provider") or ""),
            str(entry.get("base_url") or ""),
            str(entry.get("wire_api") or ""),
        )
        if entry_key == key:
            continue
        merged.append(entry)
    llm_data["provider_history"] = merged[:16]
    return llm_data["provider_history"]


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


def _history_entry_matches_delete(entry: dict[str, Any], target: dict[str, Any]) -> bool:
    entry_identity = _history_profile_identity(
        str(entry.get("provider") or ""),
        str(entry.get("base_url") or ""),
        str(entry.get("wire_api") or ""),
    )
    target_identity = _history_profile_identity(
        str(target.get("provider") or ""),
        str(target.get("base_url") or ""),
        str(target.get("wire_api") or ""),
    )
    return entry_identity == target_identity


@_serialized_settings_update
def delete_llm_provider_history(payload: dict[str, Any]) -> dict[str, Any]:
    settings_data = _load_settings_json()
    llm_data = settings_data.setdefault("llm", {})
    raw_history = llm_data.get("provider_history", [])
    if not isinstance(raw_history, list):
        raw_history = []

    provider = _normalize_provider(str(payload.get("provider") or "custom"))
    base_url = str(payload.get("base_url") or "").strip()
    provider_id = str(payload.get("provider_id") or "").strip()
    model = str(payload.get("model") or "").strip()
    wire_api = str(
        payload.get("wire_api")
        or ("anthropic" if provider == "anthropic" else "responses" if provider == "openai" else "chat")
    ).strip()
    if not (base_url or provider_id or model):
        raise SettingsError("Provider history deletion requires base_url, provider_id, or model.")

    target = {
        "provider": provider,
        "base_url": base_url,
        "wire_api": wire_api,
        "provider_id": provider_id,
        "model": model,
    }
    next_history: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for entry in raw_history:
        if isinstance(entry, dict) and _history_entry_matches_delete(entry, target):
            removed.append(entry)
        else:
            next_history.append(entry)

    if not removed:
        raise SettingsError("Saved provider configuration was not found.")

    llm_data["provider_history"] = next_history[:16]
    if bool(payload.get("clear_api_key", True)):
        for entry in removed:
            entry_provider = _normalize_provider(str(entry.get("provider") or provider))
            entry_base_url = str(entry.get("base_url") or base_url).strip()
            _clear_scoped_runtime_api_key(entry_provider, entry_base_url)
            image_base_url = str(entry.get("image_base_url") or "").strip()
            if image_base_url:
                _clear_scoped_runtime_image_api_key(entry_provider, image_base_url)

    _write_settings_json(settings_data)
    return get_llm_settings_payload(settings_data, include_api_keys=True)


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


def get_available_models(
    provider: str | None = None,
    settings_data: dict[str, Any] | None = None,
) -> list[str]:
    active_provider = _normalize_provider(provider or get_llm_provider(settings_data))
    if active_provider == "anthropic":
        return get_anthropic_settings(settings_data)["available_models"]
    if active_provider == "custom":
        return get_custom_settings(settings_data)["available_models"]
    return get_openai_settings(settings_data)["available_models"]


def get_models_source(
    provider: str | None = None,
    settings_data: dict[str, Any] | None = None,
) -> str:
    """Return the persisted model list source ('live' or '') for the given provider."""
    active_provider = _normalize_provider(provider or get_llm_provider(settings_data))
    if active_provider == "anthropic":
        return get_anthropic_settings(settings_data).get("models_source", "")
    if active_provider == "custom":
        return get_custom_settings(settings_data).get("models_source", "")
    return get_openai_settings(settings_data).get("models_source", "")


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


def resolve_provider_api_key_for_base_url(provider: str, base_url: str) -> str:
    normalized = _normalize_provider(provider)
    return _provider_api_key_for_base_url(normalized, base_url)


def resolve_provider_image_api_key_for_base_url(provider: str, base_url: str) -> str:
    """Resolve only the independent Images API key for one endpoint scope.

    Image credentials deliberately use a separate vault namespace from the
    text provider key.  Exposing this narrow resolver lets unsaved connection
    checks use an already stored image key without ever borrowing a key from a
    different provider profile or returning the secret to the frontend.
    """

    normalized = _normalize_provider(provider)
    return _image_api_key_for_base_url(normalized, base_url)


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


def get_config_requirements():
    return load_config_layer_stack().requirements


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
def save_llm_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings_data = _load_settings_json()
    settings_data.pop("prompt_persona", None)
    raw_llm = settings_data.get("llm")
    stored_llm = dict(raw_llm) if isinstance(raw_llm, dict) else {}
    current_openai = get_openai_settings(settings_data)
    current_anthropic = get_anthropic_settings(settings_data)
    current_custom = get_custom_settings(settings_data)

    raw_provider = payload.get("provider")
    provider = _normalize_provider(str(raw_provider or get_llm_provider(settings_data)))

    raw_openai = payload.get("openai", {})
    openai_updates = raw_openai if isinstance(raw_openai, dict) else {}
    openai_api_key_provided = "api_key" in openai_updates
    openai_api_key = str(openai_updates.get("api_key", "")).strip()
    openai_base_url = str(openai_updates.get("base_url", current_openai["base_url"])).strip()
    if openai_api_key_provided and _is_api_key_replacement(openai_api_key):
        _set_runtime_api_key("openai", openai_api_key, openai_base_url)
    openai_model = str(openai_updates.get("model", current_openai["model"])).strip()
    openai_capabilities_match = (
        (openai_model or current_openai["model"]) == current_openai["model"]
        and _normalize_openai_base_url(openai_base_url) == current_openai["base_url"]
    )
    openai_reasoning_effort = str(
        openai_updates.get("reasoning_effort", current_openai["reasoning_effort"])
    ).strip()
    openai_responses_reasoning_summary = str(
        openai_updates.get(
            "responses_reasoning_summary",
            current_openai["responses_reasoning_summary"],
        )
    ).strip()
    openai_wire_api = str(openai_updates.get("wire_api", current_openai["wire_api"])).strip()
    current_openai_wire_api = normalize_custom_wire_api(
        str(current_openai.get("base_url") or openai_base_url),
        str(current_openai["wire_api"]),
        "responses",
    )
    next_openai_wire_api = normalize_custom_wire_api(
        openai_base_url,
        openai_wire_api or current_openai["wire_api"],
        current_openai["wire_api"],
    )
    openai_switched_to_responses = current_openai_wire_api != "responses" and next_openai_wire_api == "responses"
    openai_prompt_cache_retention_default = (
        _responses_prompt_cache_retention_default(True)
        if openai_switched_to_responses
        else current_openai["prompt_cache_retention"] if next_openai_wire_api == "responses" else ""
    )
    openai_prompt_cache_retention = (
        _normalize_prompt_cache_retention(
            openai_updates.get("prompt_cache_retention", openai_prompt_cache_retention_default),
            openai_prompt_cache_retention_default,
        )
        if next_openai_wire_api == "responses"
        else ""
    )
    next_openai_model = openai_model or current_openai["model"]
    next_openai_metadata = _next_model_metadata(
        openai_updates,
        current_openai,
        capabilities_match=openai_capabilities_match,
    )
    next_openai_resolved_metadata = get_provider_model_metadata(
        {
            "model": next_openai_model,
            "model_metadata": next_openai_metadata,
            "reasoning_effort_levels": openai_updates.get(
                "reasoning_effort_levels",
                current_openai["reasoning_effort_levels"]
                if openai_capabilities_match
                else [],
            ),
        },
        next_openai_model,
    )
    next_openai_image = _next_image_settings(
        "openai",
        openai_updates,
        current_openai,
    )
    next_openai = {
        "display_name": str(openai_updates.get("display_name", current_openai["display_name"])).strip(),
        "api_key": "",
        "base_url": _normalize_openai_base_url(openai_base_url),
        "model": next_openai_model,
        "small_fast_model": str(
            openai_updates.get("small_fast_model", current_openai["small_fast_model"])
        ).strip(),
        "available_models": _coerce_model_list(
            openai_updates.get("available_models", current_openai["available_models"])
        ),
        "models_source": str(openai_updates.get("models_source", current_openai.get("models_source", ""))).strip(),
        "model_metadata": next_openai_metadata,
        "model_labels": _coerce_model_labels(
            openai_updates.get("model_labels", current_openai.get("model_labels", {}))
        ),
        "reasoning_effort": openai_reasoning_effort or current_openai["reasoning_effort"],
        "responses_reasoning_summary": (
            openai_responses_reasoning_summary
            or current_openai["responses_reasoning_summary"]
        ),
        "max_tokens": max(
            0,
            _coerce_int(
                openai_updates.get("max_tokens", current_openai["max_tokens"]),
                current_openai["max_tokens"],
            ),
        ),
        "wire_api": next_openai_wire_api,
        "proxy_mode": _normalize_proxy_mode(
            openai_updates.get("proxy_mode", current_openai["proxy_mode"]),
        ),
        "headers": dict(
            openai_updates.get("headers", current_openai["default_headers"])
        ),
        "auth_header": bool(
            openai_updates.get("auth_header", current_openai["auth_header"])
        ),
        "prompt_cache_retention": openai_prompt_cache_retention,
        "reasoning_effort_levels": next_openai_resolved_metadata[
            "reasoning_effort_levels"
        ],
        **next_openai_image,
    }
    if next_openai["model"] and next_openai["model"] not in next_openai["available_models"]:
        next_openai["available_models"].insert(0, next_openai["model"])

    raw_anthropic = payload.get("anthropic", {})
    anthropic_updates = raw_anthropic if isinstance(raw_anthropic, dict) else {}
    anthropic_api_key_provided = "api_key" in anthropic_updates
    anthropic_api_key = str(anthropic_updates.get("api_key", "")).strip()
    anthropic_base_url = str(anthropic_updates.get("base_url", current_anthropic["base_url"])).strip()
    if anthropic_api_key_provided and _is_api_key_replacement(anthropic_api_key):
        _set_runtime_api_key("anthropic", anthropic_api_key, anthropic_base_url)
    anthropic_model = str(anthropic_updates.get("model", current_anthropic["model"])).strip()
    next_anthropic_model = anthropic_model or current_anthropic["model"]
    anthropic_capabilities_match = (
        next_anthropic_model == current_anthropic["model"]
        and anthropic_base_url.rstrip("/")
        == str(current_anthropic["base_url"]).rstrip("/")
    )
    next_anthropic_metadata = _next_model_metadata(
        anthropic_updates,
        current_anthropic,
        capabilities_match=anthropic_capabilities_match,
    )
    next_anthropic_image = _next_image_settings(
        "anthropic",
        anthropic_updates,
        current_anthropic,
    )
    next_anthropic = {
        "display_name": str(anthropic_updates.get("display_name", current_anthropic["display_name"])).strip(),
        "api_key": "",
        "base_url": anthropic_base_url,
        "model": next_anthropic_model,
        "small_fast_model": str(
            anthropic_updates.get(
                "small_fast_model", current_anthropic["small_fast_model"]
            )
        ).strip(),
        "available_models": _coerce_model_list(
            anthropic_updates.get("available_models", current_anthropic["available_models"])
        ),
        "models_source": str(anthropic_updates.get("models_source", current_anthropic.get("models_source", ""))).strip(),
        "model_metadata": next_anthropic_metadata,
        "model_labels": _coerce_model_labels(
            anthropic_updates.get("model_labels", current_anthropic.get("model_labels", {}))
        ),
        "max_tokens": _coerce_int(
            anthropic_updates.get("max_tokens", current_anthropic["max_tokens"]),
            current_anthropic["max_tokens"],
        ),
        "thinking_budget": _coerce_int(
            anthropic_updates.get("thinking_budget", current_anthropic["thinking_budget"]),
            current_anthropic["thinking_budget"],
        ),
        "proxy_mode": _normalize_proxy_mode(
            anthropic_updates.get("proxy_mode", current_anthropic["proxy_mode"]),
        ),
        "headers": dict(
            anthropic_updates.get("headers", current_anthropic["default_headers"])
        ),
        "auth_header": bool(
            anthropic_updates.get("auth_header", current_anthropic["auth_header"])
        ),
        **next_anthropic_image,
    }
    if next_anthropic["max_tokens"] <= 0:
        next_anthropic["max_tokens"] = MINICODE_CAPPED_DEFAULT_MAX_TOKENS
    if next_anthropic["model"] and next_anthropic["model"] not in next_anthropic["available_models"]:
        next_anthropic["available_models"].insert(0, next_anthropic["model"])

    raw_custom = payload.get("custom", {})
    custom_updates = raw_custom if isinstance(raw_custom, dict) else {}
    custom_api_key_provided = "api_key" in custom_updates
    custom_api_key = str(custom_updates.get("api_key", "")).strip()
    custom_base_url = str(custom_updates.get("base_url", current_custom["base_url"])).strip()
    if custom_api_key_provided and _is_api_key_replacement(custom_api_key):
        _set_runtime_api_key("custom", custom_api_key, custom_base_url)
    custom_model = str(custom_updates.get("model", current_custom["model"])).strip()
    custom_wire_api = normalize_custom_wire_api(
        custom_base_url,
        str(custom_updates.get("wire_api", current_custom["wire_api"])),
        current_custom["wire_api"],
    )
    current_custom_wire_api = normalize_custom_wire_api(
        str(current_custom.get("base_url") or custom_base_url),
        str(current_custom["wire_api"]),
        "chat",
    )
    custom_switched_to_responses = current_custom_wire_api != "responses" and custom_wire_api == "responses"
    custom_prompt_cache_retention_default = (
        _responses_prompt_cache_retention_default(True, "custom")
        if custom_switched_to_responses
        else current_custom["prompt_cache_retention"] if custom_wire_api == "responses" else ""
    )
    next_custom_available = _coerce_model_list(
        custom_updates.get("available_models", current_custom["available_models"])
    )
    next_custom_model = _select_custom_model(
        custom_model or current_custom["model"],
        next_custom_available,
    )
    custom_capabilities_match = (
        next_custom_model == current_custom["model"]
        and custom_base_url.rstrip("/") == str(current_custom["base_url"]).rstrip("/")
    )
    next_custom_metadata = _next_model_metadata(
        custom_updates,
        current_custom,
        capabilities_match=custom_capabilities_match,
    )
    next_custom_resolved_metadata = get_provider_model_metadata(
        {
            "model": next_custom_model,
            "model_metadata": next_custom_metadata,
            "reasoning_effort_levels": custom_updates.get(
                "reasoning_effort_levels",
                current_custom["reasoning_effort_levels"]
                if custom_capabilities_match
                else [],
            ),
        },
        next_custom_model,
    )
    next_custom_image = _next_image_settings(
        "custom",
        custom_updates,
        current_custom,
    )
    custom_max_tokens_default = (
        MINICODE_CAPPED_DEFAULT_MAX_TOKENS if custom_wire_api == "anthropic" else 0
    )
    if "max_tokens" in custom_updates:
        custom_max_tokens = _coerce_int(
            custom_updates.get("max_tokens"),
            custom_max_tokens_default,
        )
    elif custom_wire_api == current_custom_wire_api:
        custom_max_tokens = _coerce_int(
            current_custom.get("max_tokens"),
            custom_max_tokens_default,
        )
    else:
        custom_max_tokens = custom_max_tokens_default
    if custom_wire_api == "anthropic" and custom_max_tokens <= 0:
        custom_max_tokens = MINICODE_CAPPED_DEFAULT_MAX_TOKENS

    next_custom = {
        "display_name": str(custom_updates.get("display_name", current_custom["display_name"])).strip(),
        "api_key": "",
        "base_url": (
            _normalize_openai_base_url(custom_base_url)
            if custom_base_url and custom_wire_api != "anthropic"
            else custom_base_url
        ),
        "model": next_custom_model,
        "small_fast_model": str(
            custom_updates.get("small_fast_model", current_custom["small_fast_model"])
        ).strip(),
        "available_models": next_custom_available,
        "models_source": str(custom_updates.get("models_source", current_custom.get("models_source", ""))).strip(),
        "model_metadata": next_custom_metadata,
        "model_labels": _coerce_model_labels(
            custom_updates.get("model_labels", current_custom.get("model_labels", {}))
        ),
        "reasoning_effort": str(custom_updates.get("reasoning_effort", current_custom["reasoning_effort"])).strip(),
        "responses_reasoning_summary": str(
            custom_updates.get(
                "responses_reasoning_summary",
                current_custom["responses_reasoning_summary"],
            )
        ).strip(),
        "max_tokens": max(0, custom_max_tokens),
        "thinking_budget": _coerce_int(
            custom_updates.get("thinking_budget", current_custom["thinking_budget"]),
            current_custom["thinking_budget"],
        ),
        "wire_api": custom_wire_api or current_custom["wire_api"],
        "proxy_mode": _normalize_proxy_mode(
            custom_updates.get("proxy_mode", current_custom["proxy_mode"]),
        ),
        "headers": dict(
            custom_updates.get("headers", current_custom["default_headers"])
        ),
        "auth_header": bool(
            custom_updates.get("auth_header", current_custom["auth_header"])
        ),
        "prompt_cache_retention": (
            _normalize_prompt_cache_retention(
                custom_updates.get("prompt_cache_retention", custom_prompt_cache_retention_default),
                custom_prompt_cache_retention_default,
            )
            if custom_wire_api == "responses"
            else ""
        ),
        "reasoning_effort_levels": next_custom_resolved_metadata[
            "reasoning_effort_levels"
        ],
        **next_custom_image,
    }
    if next_custom["model"] and next_custom["model"] not in next_custom["available_models"]:
        next_custom["available_models"].insert(0, next_custom["model"])

    # Preserve untouched provider sections exactly as writable user data
    # instead of materializing environment/config-layer fallbacks into
    # settings.json. This distinction is important for desktop launches from
    # development agents, which may expose temporary localhost model proxies.
    # Secrets are still scrubbed from any legacy plaintext provider section.
    next_llm = dict(stored_llm)
    next_llm["provider"] = provider
    next_llm["provider_history"] = _llm_history(settings_data)
    for section_provider, updates, section in (
        ("openai", openai_updates, next_openai),
        ("anthropic", anthropic_updates, next_anthropic),
        ("custom", custom_updates, next_custom),
    ):
        if updates:
            next_llm[section_provider] = section
            continue
        stored_section = next_llm.get(section_provider)
        if isinstance(stored_section, dict):
            preserved_section = dict(stored_section)
            preserved_section["api_key"] = ""
            preserved_section["image_api_key"] = ""
            next_llm[section_provider] = preserved_section
        else:
            next_llm.pop(section_provider, None)
    settings_data["llm"] = next_llm
    upserted_providers: set[str] = set()
    for section_provider, updates, section in (
        ("openai", openai_updates, next_openai),
        ("anthropic", anthropic_updates, next_anthropic),
        ("custom", custom_updates, next_custom),
    ):
        if updates:
            _upsert_llm_history(settings_data, section_provider, section)
            upserted_providers.add(section_provider)
    active_section = {
        "openai": next_openai,
        "anthropic": next_anthropic,
        "custom": next_custom,
    }.get(provider)
    # Only create a history card when the caller explicitly selected a
    # provider. Persona-only and other unrelated settings saves must not turn
    # built-in/default provider sections into user-configured profiles.
    if active_section is not None and not upserted_providers and raw_provider is not None:
        _upsert_llm_history(settings_data, provider, active_section)
    _write_settings_json(settings_data)
    return get_llm_settings_payload(settings_data, include_api_keys=True)


@_serialized_settings_update
def add_permission_content_rule(rule: str, *, deny: bool = False) -> list[str]:
    """Append a Tool(content) permission rule to settings.json and persist it.

    Used by the approval dialog's "always allow/deny this" action. Because the
    runtime PermissionChecker is rebuilt from ``load_config().permissions`` on
    each use, the saved rule takes effect on the next tool call. Returns the
    updated rule list.
    """
    rule = str(rule or "").strip()
    if not rule:
        return []
    # Never persist a broad shell-wrapper rule generated by a client.  The
    # content matcher is intentionally conservative, but rejecting it here
    # also protects older clients/runtimes that may not share that matcher.
    from backend.permissions.content_rules import (
        command_prefix_uses_unsafe_wrapper,
        parse_content_rule,
    )

    parsed_rule = parse_content_rule(rule)
    if parsed_rule is None:
        raise ValueError("Invalid permission content rule")
    if parsed_rule.tool_glob in {"run_command", "terminal_*"} and parsed_rule.content:
        command_prefix = parsed_rule.content[:-2].strip() if parsed_rule.content.endswith(":*") else ""
        if command_prefix_uses_unsafe_wrapper(command_prefix):
            raise ValueError("Refusing a permanent permission rule for a shell or command wrapper")
    settings_data = _load_settings_json()
    perms = settings_data.setdefault("permissions", {})
    key = "content_deny_rules" if deny else "content_allow_rules"
    rules = [str(r) for r in perms.get(key, []) if str(r).strip()]
    if rule in rules:
        return rules  # already present — no write needed
    rules.append(rule)
    perms[key] = rules
    _write_settings_json(settings_data)
    return rules


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


def load_config(*, cwd: Path | None = None) -> AppConfig:
    """Load the effective application config and retain its provenance stack."""
    config_layer_stack = load_config_layer_stack(cwd=cwd)
    settings_data = config_layer_stack.effective_config()
    feature_flags = load_feature_flags(
        settings_data,
        managed_requirements=config_layer_stack.requirements.feature_requirements,
    )

    # LLM provider credentials and request headers are optional request material.
    # load_llm_settings raises only for contradictory provider configuration;
    # those errors must remain visible instead of being replaced with an empty
    # provider that silently changes the runtime boundary.
    llm = load_llm_settings(settings_data)

    permissions = permission_settings_from_config(settings_data)

    # Agent 参数来自 settings.json
    raw_agent_data = settings_data.get("agent", {})
    agent_data = raw_agent_data if isinstance(raw_agent_data, Mapping) else {}
    if "fallback_providers" in agent_data:
        raise SettingsError(
            "agent.fallback_providers is no longer supported; MiniCode runs "
            "one explicitly selected provider per agent session"
        )
    raw_stream_retryable = agent_data.get(
        "stream_retryable_substrings",
        AgentSettings.stream_retryable_substrings,
    )
    if isinstance(raw_stream_retryable, str):
        raw_stream_retryable = [item.strip() for item in raw_stream_retryable.split(",")]
    elif not isinstance(raw_stream_retryable, (list, tuple, set)):
        raw_stream_retryable = []

    max_iterations = _coerce_nonnegative_int(agent_data.get("max_iterations"), AgentSettings.max_iterations)
    max_tool_calls = _coerce_nonnegative_int(agent_data.get("max_tool_calls"), AgentSettings.max_tool_calls)
    max_turn_tokens = _coerce_nonnegative_int(agent_data.get("max_turn_tokens"), AgentSettings.max_turn_tokens)
    max_turn_cost_usd = _coerce_nonnegative_float(agent_data.get("max_turn_cost_usd"), AgentSettings.max_turn_cost_usd)
    max_turn_seconds = _coerce_nonnegative_float(agent_data.get("max_turn_seconds"), AgentSettings.max_turn_seconds)
    agent = AgentSettings(
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        turn_error_budget=_coerce_nonnegative_int(
            agent_data.get("turn_error_budget"), AgentSettings.turn_error_budget
        ),
        max_turn_tokens=max_turn_tokens,
        max_turn_cost_usd=max_turn_cost_usd,
        max_turn_seconds=max_turn_seconds,
        compaction_keep_recent_tokens=_coerce_nonnegative_int(
            agent_data.get("compaction_keep_recent_tokens"), 20_000
        ),
        time_based_microcompact_enabled=coerce_feature_bool(
            agent_data.get("time_based_microcompact_enabled"), False
        ),
        time_based_microcompact_gap_threshold_minutes=max(
            1,
            _coerce_nonnegative_int(
                agent_data.get(
                    "time_based_microcompact_gap_threshold_minutes",
                    agent_data.get("time_based_microcompact_gap_minutes"),
                ),
                60,
            ),
        ),
        time_based_microcompact_keep_recent=max(
            1,
            _coerce_nonnegative_int(
                agent_data.get("time_based_microcompact_keep_recent"), 5
            ),
        ),
        agent_mode=_normalize_agent_mode(agent_data.get("agent_mode", "react")),
        live_text_streaming=coerce_feature_bool(agent_data.get("live_text_streaming", True), True),
        stream_timeout_seconds=_coerce_nonnegative_float(
            agent_data.get("stream_timeout_seconds"), AgentSettings.stream_timeout_seconds
        ),
        first_byte_timeout_seconds=_coerce_nonnegative_float(
            agent_data.get("first_byte_timeout_seconds"), AgentSettings.first_byte_timeout_seconds
        ),
        stream_max_attempts=_coerce_nonnegative_int(
            agent_data.get("stream_max_attempts"), AgentSettings.stream_max_attempts
        ),
        stream_retry_delay_seconds=_coerce_nonnegative_float(
            agent_data.get("stream_retry_delay_seconds"), AgentSettings.stream_retry_delay_seconds
        ),
        stream_retryable_substrings=tuple(
            str(item).strip()
            for item in raw_stream_retryable
            if str(item).strip()
        ),
        approval_timeout_seconds=(
            max(1.0, _coerce_nonnegative_float(agent_data.get("approval_timeout_seconds"), 0.0))
            if agent_data.get("approval_timeout_seconds") is not None
            else None
        ),
    )

    # Token 预算
    raw_budget_data = settings_data.get("token_budget", {})
    budget_data = raw_budget_data if isinstance(raw_budget_data, Mapping) else {}
    # Provider metadata is resolved when LLMSettings is built.  Keep the
    # numeric fallback for older/manual settings while preserving its
    # unverified provenance on the LLM settings payload.
    default_total = int(getattr(llm, "context_window", 0) or 0)
    if default_total <= 0:
        default_total = resolve_context_window(getattr(llm, "model", ""))
    total = _coerce_nonnegative_int(budget_data.get("total"), default_total)
    if total < 2:
        total = default_total
    response_reserve = _coerce_nonnegative_int(
        budget_data.get("response_reserve"), TokenBudget.response_reserve
    )
    if response_reserve >= total:
        response_reserve = max(1, total - 1)
    token_budget = TokenBudget(
        total=total,
        system_prompt=_coerce_nonnegative_int(budget_data.get("system_prompt"), TokenBudget.system_prompt),
        active_skills=_coerce_nonnegative_int(budget_data.get("active_skills"), TokenBudget.active_skills),
        memory_index=_coerce_nonnegative_int(budget_data.get("memory_index"), TokenBudget.memory_index),
        tool_schemas=_coerce_nonnegative_int(budget_data.get("tool_schemas"), TokenBudget.tool_schemas),
        agent_state=_coerce_nonnegative_int(budget_data.get("agent_state"), TokenBudget.agent_state),
        response_reserve=response_reserve,
    )

    return AppConfig(
        llm=llm,
        token_budget=token_budget,
        permissions=permissions,
        agent=agent,
        feature_flags=feature_flags,
        config_layer_stack=config_layer_stack,
    )


def permission_settings_from_config(settings_data: Mapping[str, Any]) -> PermissionSettings:
    """Project one immutable config snapshot into the permission checker model."""

    raw_perm_data = settings_data.get("permissions", {})
    perm_data = raw_perm_data if isinstance(raw_perm_data, Mapping) else {}
    default_permissions = PermissionSettings()
    return PermissionSettings(
        auto_allow=_coerce_string_list(
            perm_data.get("auto_allow"), default_permissions.auto_allow
        ),
        require_confirm=_coerce_string_list(
            perm_data.get("require_confirm"), default_permissions.require_confirm
        ),
        require_diff_review=_coerce_string_list(
            perm_data.get("require_diff_review"), default_permissions.require_diff_review
        ),
        always_deny=_coerce_string_list(perm_data.get("always_deny"), []),
        path_allowlist=_coerce_string_list(
            perm_data.get("path_allowlist"), default_permissions.path_allowlist
        ),
        path_denylist=_merge_unique(
            _coerce_string_list(
                perm_data.get("path_denylist"), default_permissions.path_denylist
            ),
            default_permissions.path_denylist,
        ),
        content_allow_rules=_merge_unique(
            _coerce_string_list(perm_data.get("content_allow_rules"), [])
            + _coerce_string_list(perm_data.get("allow"), []),
            [],
        ),
        content_ask_rules=_merge_unique(
            _coerce_string_list(perm_data.get("content_ask_rules"), [])
            + _coerce_string_list(perm_data.get("ask"), []),
            [],
        ),
        content_deny_rules=_merge_unique(
            _coerce_string_list(perm_data.get("content_deny_rules"), [])
            + _coerce_string_list(perm_data.get("deny"), []),
            [],
        ),
    )
