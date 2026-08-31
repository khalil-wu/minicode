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

from backend.config_helpers import (
    ContextWindowResolution,
    DATA_ROOT,
    LLMSettings,
    MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
    MODEL_CONTEXT_WINDOW_DEFAULT,
    PROJECT_ROOT,
    SETTINGS_FILE,
    STATE_ROOT,
    SettingsError,
    _KNOWN_MODEL_CONTEXT_WINDOWS,
    _RUNTIME_API_KEY_SCOPES,
    _RUNTIME_IMAGE_API_KEY_SCOPES,
    _SETTINGS_WRITE_LOCK,
    _coerce_int,
    _coerce_model_labels,
    _coerce_model_list,
    _coerce_model_metadata,
    _coerce_reasoning_effort_levels,
    _custom_provider_api_key,
    _get_llm_section,
    _global_provider_key_matches_base_url,
    _history_identity,
    _history_profile_identity,
    _image_api_key_for_base_url,
    _image_scoped_vault_names,
    _is_api_key_replacement,
    _legacy_provider_key_scope,
    _llm_history,
    _load_effective_settings_json,
    _load_settings_json,
    _normalize_image_mode,
    _normalize_image_quality,
    _normalize_image_size,
    _normalize_openai_base_url,
    _normalize_prompt_cache_retention,
    _normalize_provider,
    _normalize_proxy_mode,
    _normalize_wire_api,
    _normalized_provider_base_url,
    _provider_api_key_for_base_url,
    _provider_display_name,
    _provider_id_for_history,
    _provider_image_fields,
    _provider_key_base_url_candidates,
    _provider_key_scope,
    _provider_proxy_mode,
    _provider_request_material,
    _reasoning_effort_projection,
    _responses_prompt_cache_retention_default,
    _scoped_vault_names,
    _select_custom_model,
    _serialized_settings_update,
    _vault_api_key,
    _vault_has_scoped_provider_keys,
    _write_settings_json,
    get_anthropic_settings,
    get_custom_settings,
    get_image_generation_settings,
    get_llm_provider,
    get_llm_settings_payload,
    get_openai_settings,
    get_provider_model_metadata,
    load_config_layer_stack,
    load_llm_settings,
    normalize_custom_wire_api,
    resolve_context_window_details,
)
from backend.config_providers import (
    _clear_runtime_api_key,
    _next_image_settings,
    _next_model_metadata,
    _set_runtime_api_key,
    _set_runtime_image_api_key,
    _upsert_llm_history,
    get_available_models,
    get_models_source,
    save_llm_settings,
)
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.agent.policies import (
        StreamRetryPolicy,
    )
    from backend.config_layers import ConfigLayerStack

# ── 项目根目录（backend/ 的父目录）──────────────────────────────
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

# MiniCode's Messages API requires max_tokens and uses this capped default;
# OpenAI Responses/Chat APIs leave the field out when the user did not set it.


# MiniCode's default when a provider does not publish a context window.

# Other established model-family windows, matched longest-prefix-first. OpenAI
# GPT-5 records deliberately stay out of this table because MiniCode publishes
# them as exact catalog entries with materially different default/max windows.

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


def _update_settings_json(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply one read-modify-write transaction to the user settings layer."""

    with _SETTINGS_WRITE_LOCK:
        with file_mutation_locks([SETTINGS_FILE]):
            settings_data = _load_settings_json()
            mutator(settings_data)
            _write_settings_json(settings_data)
            return settings_data




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


def get_config_requirements():
    return load_config_layer_stack().requirements


@_serialized_settings_update

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