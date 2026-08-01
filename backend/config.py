"""
MiniCode 统一配置中心。

从环境变量和 settings.json 读取所有配置项，是系统的唯一配置来源。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from backend.feature_flags import FeatureFlags, coerce_feature_bool, load_feature_flags

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.agent.policies import (
        StreamRetryPolicy,
    )

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


class SettingsError(RuntimeError):
    """配置加载失败时抛出。"""


# Claude Code's Messages API requires max_tokens and uses this capped default;
# OpenAI Responses/Chat APIs leave the field out when the user did not set it.
CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS = 8_000


@dataclass(frozen=True)
class LLMSettings:
    """LLM 连接配置。"""

    api_key: str
    provider: str = "custom"
    base_url: str = ""
    model: str = ""
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    max_tokens: int = 0
    wire_api: str = "chat"  # "responses", "chat", or "anthropic"
    responses_stateful_continuation: bool = False
    prompt_cache_retention: str = ""
    reasoning_effort_levels: tuple[str, ...] = ()
    seed: int | None = None


# Claude Code's default when a provider does not publish a context window.
MODEL_CONTEXT_WINDOW_DEFAULT = 200_000

# Well-known model-family context windows, matched by longest-prefix-first so a
# specific id beats a generic family. Mirrors Claude Code's per-model capability
# resolution (getContextWindowForModel) for the models users actually configure;
# unknown/custom ids keep the 200K default (which the env override can raise).
_KNOWN_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic Claude 4 / 3.x families
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-3-7", 200_000),
    ("claude-3-5", 200_000),
    ("claude-3-", 200_000),
    ("claude-instant-", 100_000),
    # OpenAI GPT families
    ("gpt-5", 400_000),
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
)


def resolve_context_window(model: str) -> int:
    """Resolve the effective context window for a model id.

    Precedence: explicit host override > known model family > 200K default.
    Unknown ids are treated as 200K (Claude Code's default for providers that
    do not publish a window); the override exists for custom gateways.
    """
    override = os.environ.get("MINICODE_MAX_CONTEXT_TOKENS", "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            pass
    model_id = str(model or "").strip()
    if not model_id:
        return MODEL_CONTEXT_WINDOW_DEFAULT
    lowered = model_id.lower()
    # Explicit [1m] suffix opt-in wins over the family window (Claude Code's
    # has1mContext behavior). e.g. claude-opus-4-1m → 1M, not the 200K family.
    if lowered.endswith("-1m") or "-1m-" in lowered:
        return 1_000_000
    # Longest prefix wins so a specific id beats its family.
    matched = 0
    for prefix, window in _KNOWN_MODEL_CONTEXT_WINDOWS:
        if lowered.startswith(prefix) and len(prefix) > matched:
            matched = len(prefix)
            result = window
    if matched:
        return result
    return MODEL_CONTEXT_WINDOW_DEFAULT


@dataclass(frozen=True)
class TokenBudget:
    """Context accounting plus Pi's response reserve."""

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
            "read_memory",
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
            "remember_*",
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
    # Content-level rules in Tool(content) syntax, e.g. Bash(npm run:*), Edit(src/**).
    # allow rules force AUTO (in non-plan modes); deny rules ALWAYS_DENY in every
    # mode (safety). Parsed by permissions.content_rules.
    content_allow_rules: list[str] = field(default_factory=list)
    content_deny_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentSettings:
    """Agent Loop 运行参数。"""

    # Optional host-owned limits. Zero follows Pi/Codex session behavior: the
    # agent loop itself does not invent task-size or cost thresholds.
    max_iterations: int = 0
    max_tool_calls: int = 0
    turn_error_budget: int = 0
    max_turn_tokens: int = 0
    max_turn_cost_usd: float = 0.0
    max_turn_seconds: float = 0.0
    compaction_keep_recent_tokens: int = 20_000
    fallback_providers: tuple[str, ...] = ()  # 主 LLM 失败时按顺序尝试的备用 provider
    agent_mode: str = "react"

    # Stream assistant text live (token-by-token) instead of buffering and
    # emitting once at turn end. When enabled, agent_message.delta events are
    # yielded during the provider's explicit final-answer phase and the
    # authoritative text arrives in item.completed. Set false for final-only
    # output.
    live_text_streaming: bool = True
    # Provider retry behavior is opt-in and host-configured.
    stream_timeout_seconds: float = 90.0
    first_byte_timeout_seconds: float = 0.0
    stream_max_attempts: int = 1
    stream_retry_delay_seconds: float = 0.0
    stream_retryable_substrings: tuple[str, ...] = ()
    # Deprecated compatibility fields. Iteration budgets are explicit and are
    # never inferred from request text, tool signatures, or output heuristics.

    # Policy slots — None means the Loop_Core fills in the default implementation
    stream_retry_policy: "StreamRetryPolicy | None" = None

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


def _normalize_provider(provider: str) -> str:
    value = provider.strip().lower()
    if value in {"openai", "anthropic", "custom"}:
        return value
    return "custom"


def _provider_key_scope(base_url: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    identity = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    if not identity.strip(":/"):
        return ""
    import hashlib

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def _provider_key_base_url_candidates(base_url: str) -> tuple[str, ...]:
    """Return the exact endpoint scope for a provider credential."""
    raw = str(base_url or "").strip()
    return (raw,) if raw else ()


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

    scoped_name = _scoped_vault_name(provider, base_url)
    if not scoped_name:
        return
    try:
        from backend.vault import EnvVault

        EnvVault().set(
            scoped_name,
            api_key,
            description=f"{provider} provider API key for {urlsplit(base_url).netloc or base_url}",
            scope="global",
        )
    except Exception as exc:
        logger.debug("vault scoped API key write failed for %s: %s", scoped_name, exc)


def _clear_runtime_api_key(provider: str, base_url: str = "") -> None:
    if provider == "anthropic":
        vault_name = "ANTHROPIC_API_KEY"
    elif provider == "custom":
        vault_name = "CUSTOM_API_KEY"
    else:
        vault_name = "OPENAI_API_KEY"
    os.environ.pop(vault_name, None)
    names = [vault_name]
    scoped_name = _scoped_vault_name(provider, base_url)
    if scoped_name:
        names.append(scoped_name)
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for name in names:
            vault.delete(name)
    except Exception as exc:
        logger.debug("vault API key clear failed for %s: %s", vault_name, exc)


def _clear_scoped_runtime_api_key(provider: str, base_url: str = "") -> None:
    scoped_name = _scoped_vault_name(provider, base_url)
    if not scoped_name:
        return
    os.environ.pop(scoped_name, None)
    try:
        from backend.vault import EnvVault

        EnvVault().delete(scoped_name)
    except Exception as exc:
        logger.debug("vault scoped API key clear failed for %s: %s", scoped_name, exc)


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
    for candidate in _provider_key_base_url_candidates(base_url):
        scoped_name = _scoped_vault_name(provider, candidate)
        if scoped_name:
            scoped = _vault_api_key(scoped_name).strip()
            if _is_api_key_replacement(scoped):
                return scoped

    direct = os.getenv(env_name, "").strip()
    if _is_api_key_replacement(direct):
        return direct

    saved = _vault_api_key(env_name).strip()
    return saved if _is_api_key_replacement(saved) else ""


def _custom_provider_api_key(base_url: str, *, allow_scoped: bool = True) -> str:
    if allow_scoped:
        for candidate in _provider_key_base_url_candidates(base_url):
            scoped_name = _scoped_vault_name("custom", candidate)
            if scoped_name:
                scoped = _vault_api_key(scoped_name).strip()
                if _is_api_key_replacement(scoped):
                    return scoped

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
        return _custom_provider_api_key(os.getenv("OPENAI_BASE_URL", ""))
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


def _normalize_wire_api(value: str, default: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"responses", "chat", "anthropic"}:
        return normalized
    if normalized in {"anthropic_messages", "messages", "claude"}:
        return "anthropic"
    return default


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


def provider_supports_reasoning_effort(provider: str, section: dict[str, Any] | None) -> bool:
    """Return True when the selected model declares reasoning-effort support."""
    normalized = _normalize_provider(provider)
    if normalized == "anthropic":
        return False
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

    return bool(reasoning_effort_levels(
        str(data.get("model") or ""),
        wire_api,
        data.get("reasoning_effort_levels"),
    ))


def active_provider_supports_reasoning_effort(payload: dict[str, Any] | None = None) -> bool:
    settings_payload = payload if isinstance(payload, dict) else get_llm_settings_payload()
    provider = str(settings_payload.get("provider") or get_llm_provider()).strip()
    section = settings_payload.get(_normalize_provider(provider))
    return provider_supports_reasoning_effort(provider, section if isinstance(section, dict) else None)


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
    import tempfile
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=SETTINGS_FILE.parent, suffix=".tmp", prefix=".settings_"
    )
    try:
        os.write(tmp_fd, content.encode("utf-8"))
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_path, str(SETTINGS_FILE))
    except BaseException:
        if tmp_fd >= 0:
            os.close(tmp_fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _get_llm_section(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = settings_data if settings_data is not None else _load_settings_json()
    llm_data = raw.get("llm", {}) if isinstance(raw, dict) else {}
    return llm_data if isinstance(llm_data, dict) else {}


def get_llm_provider(settings_data: dict[str, Any] | None = None) -> str:
    llm_data = _get_llm_section(settings_data)
    configured = llm_data.get("provider")
    if isinstance(configured, str) and configured.strip():
        return _normalize_provider(configured)
    return _normalize_provider(os.getenv("LLM_PROVIDER", "openai"))


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


def _responses_stateful_default(enabled: bool) -> bool:
    return coerce_feature_bool(os.getenv("OPENAI_RESPONSES_STATEFUL"), False) if enabled else False


def _responses_prompt_cache_retention_default(enabled: bool) -> str:
    if not enabled:
        return ""
    return _normalize_prompt_cache_retention(os.getenv("OPENAI_PROMPT_CACHE_RETENTION", ""), "")


def _history_profile_identity(
    provider: str,
    base_url: str,
    wire_api: str,
) -> tuple[str, str, str]:
    return _history_identity(provider, base_url, wire_api)


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
        responses_stateful_default = _responses_stateful_default(responses_defaults_enabled)
        prompt_cache_retention_default = _responses_prompt_cache_retention_default(responses_defaults_enabled)
        available_models = _coerce_model_list(raw.get("available_models"))
        api_key = _provider_api_key_for_base_url(provider, base_url)
        has_api_key = bool(raw.get("has_api_key")) or bool(api_key)
        entry = {
            "provider": provider,
            "provider_id": _provider_id_for_history(provider, base_url, wire_api),
            "display_name": _provider_display_name(raw),
            "base_url": base_url,
            "model": model,
            "available_models": available_models,
            "models_source": str(raw.get("models_source") or "").strip(),
            "wire_api": wire_api,
            "responses_reasoning_summary": str(raw.get("responses_reasoning_summary") or "off").strip(),
            "responses_stateful_continuation": coerce_feature_bool(
                raw.get("responses_stateful_continuation", responses_stateful_default),
                responses_stateful_default,
            ),
            "prompt_cache_retention": _normalize_prompt_cache_retention(
                raw.get("prompt_cache_retention", prompt_cache_retention_default),
                prompt_cache_retention_default,
            ),
            "reasoning_effort_levels": _coerce_model_list(raw.get("reasoning_effort_levels")),
            "thinking_budget": _coerce_int(raw.get("thinking_budget", 0), 0),
            "has_api_key": has_api_key,
            "updated_at": float(raw.get("updated_at") or 0),
        }
        if include_api_keys:
            entry["api_key"] = api_key
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
    responses_stateful_default = _responses_stateful_default(responses_defaults_enabled)
    prompt_cache_retention_default = _responses_prompt_cache_retention_default(responses_defaults_enabled)
    next_entry = {
        "provider": provider,
        "provider_id": provider_id,
        "display_name": _provider_display_name(section),
        "base_url": base_url,
        "model": model,
        "available_models": _coerce_model_list(section.get("available_models")),
        "models_source": str(section.get("models_source") or "").strip(),
        "wire_api": wire_api,
        "responses_reasoning_summary": str(section.get("responses_reasoning_summary") or "auto").strip(),
        "responses_stateful_continuation": coerce_feature_bool(
            section.get("responses_stateful_continuation", responses_stateful_default),
            responses_stateful_default,
        ),
        "prompt_cache_retention": _normalize_prompt_cache_retention(
            section.get("prompt_cache_retention", prompt_cache_retention_default),
            prompt_cache_retention_default,
        ),
        "reasoning_effort_levels": _coerce_model_list(section.get("reasoning_effort_levels")),
        "thinking_budget": _coerce_int(section.get("thinking_budget", 0), 0),
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
    normalized_base_url = str(base_url or "").strip().lower().rstrip("/")
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

    _write_settings_json(settings_data)
    return get_llm_settings_payload(settings_data)


def get_openai_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("openai", {})
    provider_data = raw if isinstance(raw, dict) else {}

    base_url = str(provider_data.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip()
    api_key = _provider_api_key_for_base_url("openai", base_url)
    model = str(provider_data.get("model") or os.getenv("OPENAI_MODEL", "")).strip()
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
    responses_stateful_continuation = (
        coerce_feature_bool(
            provider_data.get(
                "responses_stateful_continuation",
                os.getenv("OPENAI_RESPONSES_STATEFUL", "false"),
            ),
            _responses_stateful_default(False),
        )
        if responses_defaults_enabled
        else False
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
    reasoning_effort_levels = _coerce_model_list(provider_data.get("reasoning_effort_levels"))

    return {
        "display_name": _provider_display_name(provider_data),
        "api_key": api_key,
        "base_url": _normalize_openai_base_url(base_url),
        "model": model,
        "available_models": available_models,
        "models_source": models_source,
        "reasoning_effort": reasoning_effort,
        "responses_reasoning_summary": responses_reasoning_summary,
        "max_tokens": max_tokens,
        "wire_api": wire_api,
        "responses_stateful_continuation": responses_stateful_continuation,
        "prompt_cache_retention": prompt_cache_retention,
        "reasoning_effort_levels": reasoning_effort_levels,
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
    max_tokens = _coerce_int(
        provider_data.get(
            "max_tokens",
            os.getenv("ANTHROPIC_MAX_TOKENS", str(CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS)),
        ),
        CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS,
    )
    if max_tokens <= 0:
        max_tokens = CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS
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

    return {
        "display_name": _provider_display_name(provider_data),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "available_models": available_models,
        "models_source": models_source,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
    }


def get_custom_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the explicitly configured custom provider transport."""
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("custom", {})
    provider_data = raw if isinstance(raw, dict) else {}

    base_url = str(provider_data.get("base_url") or os.getenv("CUSTOM_BASE_URL", "")).strip()
    api_key = _custom_provider_api_key(base_url)
    model = str(provider_data.get("model") or os.getenv("CUSTOM_MODEL", "")).strip()
    reasoning_effort = str(
        provider_data.get("reasoning_effort") or os.getenv("CUSTOM_REASONING_EFFORT", "")
    ).strip()
    wire_api = normalize_custom_wire_api(base_url, str(provider_data.get("wire_api", "chat")), "chat")
    responses_reasoning_summary = str(
        provider_data.get("responses_reasoning_summary")
        or os.getenv("CUSTOM_RESPONSES_REASONING_SUMMARY", "off")
    ).strip()
    custom_default_max_tokens = (
        CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS if wire_api == "anthropic" else 0
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
        max_tokens = CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS
    responses_defaults_enabled = wire_api == "responses"
    responses_stateful_continuation = coerce_feature_bool(
        provider_data.get(
            "responses_stateful_continuation",
            os.getenv(
                "CUSTOM_RESPONSES_STATEFUL",
                "true" if _responses_stateful_default(responses_defaults_enabled) else "false",
            ),
        ),
        _responses_stateful_default(responses_defaults_enabled),
    )
    prompt_cache_retention = _normalize_prompt_cache_retention(
        provider_data.get(
            "prompt_cache_retention",
            os.getenv(
                "CUSTOM_PROMPT_CACHE_RETENTION",
                _responses_prompt_cache_retention_default(responses_defaults_enabled),
            ),
        ),
        _responses_prompt_cache_retention_default(responses_defaults_enabled),
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
    reasoning_effort_levels = _coerce_model_list(provider_data.get("reasoning_effort_levels"))

    return {
        "display_name": _provider_display_name(provider_data),
        "api_key": api_key,
        "base_url": (_normalize_openai_base_url(base_url) if base_url and wire_api != "anthropic" else base_url),
        "model": model,
        "available_models": available_models,
        "models_source": models_source,
        "reasoning_effort": reasoning_effort,
        "responses_reasoning_summary": responses_reasoning_summary,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
        "wire_api": wire_api,
        "responses_stateful_continuation": responses_stateful_continuation,
        "reasoning_effort_levels": reasoning_effort_levels,
        "prompt_cache_retention": prompt_cache_retention,
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


def resolve_provider_api_key_for_base_url(provider: str, base_url: str) -> str:
    normalized = _normalize_provider(provider)
    return _provider_api_key_for_base_url(normalized, base_url)


def _load_settings_json() -> dict:
    """从 settings.json 读取配置，文件不存在则返回空字典。"""
    if not SETTINGS_FILE.exists():
        return {}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_openai_base_url(base_url: str) -> str:
    """Normalize OpenAI-compatible gateway URLs to include a version path."""
    if not base_url:
        return base_url

    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if path:
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))


def get_llm_settings_payload(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "api_key": openai["api_key"],
            "base_url": openai["base_url"],
            "model": openai["model"],
            "available_models": openai["available_models"],
            "models_source": openai.get("models_source", ""),
            "reasoning_effort": openai["reasoning_effort"],
            "responses_reasoning_summary": openai["responses_reasoning_summary"],
            "max_tokens": openai["max_tokens"],
            "wire_api": openai["wire_api"],
            "responses_stateful_continuation": openai["responses_stateful_continuation"],
            "prompt_cache_retention": openai["prompt_cache_retention"],
            "reasoning_effort_levels": openai["reasoning_effort_levels"],
        },
        "anthropic": {
            "display_name": anthropic["display_name"],
            "has_api_key": bool(anthropic["api_key"]),
            "api_key": anthropic["api_key"],
            "base_url": anthropic["base_url"],
            "model": anthropic["model"],
            "available_models": anthropic["available_models"],
            "models_source": anthropic.get("models_source", ""),
            "max_tokens": anthropic["max_tokens"],
            "thinking_budget": anthropic["thinking_budget"],
        },
        "custom": {
            "display_name": custom["display_name"],
            "has_api_key": bool(custom["api_key"]),
            "api_key": custom["api_key"],
            "base_url": custom["base_url"],
            "model": custom["model"],
            "available_models": custom["available_models"],
            "models_source": custom.get("models_source", ""),
            "reasoning_effort": custom["reasoning_effort"],
            "responses_reasoning_summary": custom["responses_reasoning_summary"],
            "max_tokens": custom["max_tokens"],
            "thinking_budget": custom["thinking_budget"],
            "wire_api": custom["wire_api"],
            "responses_stateful_continuation": custom["responses_stateful_continuation"],
            "prompt_cache_retention": custom["prompt_cache_retention"],
            "reasoning_effort_levels": custom["reasoning_effort_levels"],
        },
        "provider_history": _llm_history(settings_data, include_api_keys=True),
        "active_model": active_by_provider.get(provider, openai["model"]),
    }


def save_llm_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings_data = _load_settings_json()
    settings_data.pop("prompt_persona", None)
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
        openai_model == current_openai["model"]
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
    openai_responses_stateful_default = (
        _responses_stateful_default(True)
        if openai_switched_to_responses
        else current_openai["responses_stateful_continuation"] if next_openai_wire_api == "responses" else False
    )
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
    next_openai = {
        "display_name": str(openai_updates.get("display_name", current_openai["display_name"])).strip(),
        "api_key": "",
        "base_url": _normalize_openai_base_url(openai_base_url),
        "model": openai_model or current_openai["model"],
        "available_models": _coerce_model_list(
            openai_updates.get("available_models", current_openai["available_models"])
        ),
        "models_source": str(openai_updates.get("models_source", current_openai.get("models_source", ""))).strip(),
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
        "responses_stateful_continuation": (
            coerce_feature_bool(
                openai_updates.get(
                    "responses_stateful_continuation",
                    openai_responses_stateful_default,
                ),
                bool(openai_responses_stateful_default),
            )
            if next_openai_wire_api == "responses"
            else False
        ),
        "prompt_cache_retention": openai_prompt_cache_retention,
        "reasoning_effort_levels": _coerce_model_list(
            openai_updates.get(
                "reasoning_effort_levels",
                current_openai["reasoning_effort_levels"] if openai_capabilities_match else [],
            )
        ),
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
    next_anthropic = {
        "display_name": str(anthropic_updates.get("display_name", current_anthropic["display_name"])).strip(),
        "api_key": "",
        "base_url": anthropic_base_url,
        "model": anthropic_model or current_anthropic["model"],
        "available_models": _coerce_model_list(
            anthropic_updates.get("available_models", current_anthropic["available_models"])
        ),
        "models_source": str(anthropic_updates.get("models_source", current_anthropic.get("models_source", ""))).strip(),
        "max_tokens": _coerce_int(
            anthropic_updates.get("max_tokens", current_anthropic["max_tokens"]),
            current_anthropic["max_tokens"],
        ),
        "thinking_budget": _coerce_int(
            anthropic_updates.get("thinking_budget", current_anthropic["thinking_budget"]),
            current_anthropic["thinking_budget"],
        ),
    }
    if next_anthropic["max_tokens"] <= 0:
        next_anthropic["max_tokens"] = CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS
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
    custom_responses_stateful_default = (
        _responses_stateful_default(True)
        if custom_switched_to_responses
        else current_custom["responses_stateful_continuation"] if custom_wire_api == "responses" else False
    )
    custom_prompt_cache_retention_default = (
        _responses_prompt_cache_retention_default(True)
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
    custom_max_tokens_default = (
        CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS if custom_wire_api == "anthropic" else 0
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
        custom_max_tokens = CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS

    next_custom = {
        "display_name": str(custom_updates.get("display_name", current_custom["display_name"])).strip(),
        "api_key": "",
        "base_url": (
            _normalize_openai_base_url(custom_base_url)
            if custom_base_url and custom_wire_api != "anthropic"
            else custom_base_url
        ),
        "model": next_custom_model,
        "available_models": next_custom_available,
        "models_source": str(custom_updates.get("models_source", current_custom.get("models_source", ""))).strip(),
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
        "responses_stateful_continuation": (
            coerce_feature_bool(
                custom_updates.get(
                    "responses_stateful_continuation",
                    custom_responses_stateful_default,
                ),
                bool(custom_responses_stateful_default),
            )
            if custom_wire_api == "responses"
            else False
        ),
        "prompt_cache_retention": (
            _normalize_prompt_cache_retention(
                custom_updates.get("prompt_cache_retention", custom_prompt_cache_retention_default),
                custom_prompt_cache_retention_default,
            )
            if custom_wire_api == "responses"
            else ""
        ),
        "reasoning_effort_levels": _coerce_model_list(
            custom_updates.get(
                "reasoning_effort_levels",
                current_custom["reasoning_effort_levels"] if custom_capabilities_match else [],
            )
        ),
    }
    if next_custom["model"] and next_custom["model"] not in next_custom["available_models"]:
        next_custom["available_models"].insert(0, next_custom["model"])

    settings_data["llm"] = {
        "provider": provider,
        "openai": next_openai,
        "anthropic": next_anthropic,
        "custom": next_custom,
        "provider_history": _llm_history(settings_data),
    }
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
    return get_llm_settings_payload(settings_data)


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
        if not anthropic["api_key"]:
            raise SettingsError("Missing ANTHROPIC_API_KEY")
        return LLMSettings(
            api_key=anthropic["api_key"],
            provider="anthropic",
            base_url=anthropic["base_url"],
            model=anthropic["model"],
            reasoning_effort="",
            responses_reasoning_summary="",
            max_tokens=anthropic["max_tokens"],
            wire_api="anthropic",
            responses_stateful_continuation=False,
            prompt_cache_retention="",
            reasoning_effort_levels=(),
        )

    if active_provider == "custom":
        custom = get_custom_settings(settings_data)
        if not custom["api_key"]:
            raise SettingsError("Missing API key for custom provider")
        return LLMSettings(
            api_key=custom["api_key"],
            provider="custom",
            base_url=custom["base_url"],
            model=custom["model"],
            reasoning_effort=custom["reasoning_effort"],
            responses_reasoning_summary=custom["responses_reasoning_summary"],
            max_tokens=custom["max_tokens"],
            wire_api=custom["wire_api"],
            responses_stateful_continuation=custom["responses_stateful_continuation"],
            prompt_cache_retention=custom["prompt_cache_retention"],
            reasoning_effort_levels=tuple(custom.get("reasoning_effort_levels") or ()),
        )

    openai = get_openai_settings(settings_data)
    if not openai["api_key"]:
        raise SettingsError("Missing OPENAI_API_KEY")

    return LLMSettings(
        api_key=openai["api_key"],
        provider="openai",
        base_url=openai["base_url"],
        model=openai["model"],
        reasoning_effort=openai["reasoning_effort"],
        responses_reasoning_summary=openai["responses_reasoning_summary"],
        max_tokens=openai["max_tokens"],
        wire_api=openai["wire_api"],
        responses_stateful_continuation=openai["responses_stateful_continuation"],
        prompt_cache_retention=openai["prompt_cache_retention"],
        reasoning_effort_levels=tuple(openai.get("reasoning_effort_levels") or ()),
    )


def load_config() -> AppConfig:
    """加载完整应用配置：环境变量 + settings.json。"""
    settings_data = _load_settings_json()
    feature_flags = load_feature_flags(settings_data)

    # LLM 来自环境变量
    try:
        llm = load_llm_settings(settings_data)
    except SettingsError:
        # 开发模式下允许无 API key 启动
        llm = LLMSettings(api_key="", base_url="", model="", reasoning_effort="")

    # 权限来自 settings.json
    perm_data = settings_data.get("permissions", {})
    default_permissions = PermissionSettings()
    permissions = PermissionSettings(
        auto_allow=perm_data.get("auto_allow", PermissionSettings().auto_allow),
        require_confirm=perm_data.get(
            "require_confirm", PermissionSettings().require_confirm
        ),
        require_diff_review=perm_data.get(
            "require_diff_review", PermissionSettings().require_diff_review
        ),
        always_deny=perm_data.get("always_deny", []),
        path_allowlist=perm_data.get(
            "path_allowlist", PermissionSettings().path_allowlist
        ),
        path_denylist=_merge_unique(
            list(perm_data.get("path_denylist", default_permissions.path_denylist)),
            default_permissions.path_denylist,
        ),
        content_allow_rules=_merge_unique(
            list(perm_data.get("content_allow_rules", [])),
            [],
        ),
        content_deny_rules=_merge_unique(
            list(perm_data.get("content_deny_rules", [])),
            [],
        ),
    )

    # Agent 参数来自 settings.json
    agent_data = settings_data.get("agent", {})
    raw_fallbacks = agent_data.get("fallback_providers", [])
    if isinstance(raw_fallbacks, str):
        raw_fallbacks = [item.strip() for item in raw_fallbacks.split(",")]
    fallback_providers: list[str] = []
    for item in raw_fallbacks or []:
        value = str(item).strip().lower()
        if value and value not in fallback_providers:
            fallback_providers.append(value)
    raw_stream_retryable = agent_data.get(
        "stream_retryable_substrings",
        AgentSettings.stream_retryable_substrings,
    )
    if isinstance(raw_stream_retryable, str):
        raw_stream_retryable = [item.strip() for item in raw_stream_retryable.split(",")]

    max_iterations = max(0, int(agent_data.get("max_iterations", AgentSettings.max_iterations)))
    max_tool_calls = max(0, int(agent_data.get("max_tool_calls", AgentSettings.max_tool_calls)))
    max_turn_tokens = max(0, int(agent_data.get("max_turn_tokens", AgentSettings.max_turn_tokens)))
    max_turn_cost_usd = max(0.0, float(agent_data.get("max_turn_cost_usd", AgentSettings.max_turn_cost_usd)))
    max_turn_seconds = max(0.0, float(agent_data.get("max_turn_seconds", AgentSettings.max_turn_seconds)))
    agent = AgentSettings(
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        turn_error_budget=int(agent_data.get("turn_error_budget", AgentSettings.turn_error_budget)),
        max_turn_tokens=max_turn_tokens,
        max_turn_cost_usd=max_turn_cost_usd,
        max_turn_seconds=max_turn_seconds,
        compaction_keep_recent_tokens=int(
            agent_data.get("compaction_keep_recent_tokens", 20_000)
        ),
        fallback_providers=tuple(fallback_providers),
        agent_mode=_normalize_agent_mode(agent_data.get("agent_mode", "react")),
        live_text_streaming=coerce_feature_bool(agent_data.get("live_text_streaming", True), True),
        stream_timeout_seconds=float(agent_data.get("stream_timeout_seconds", AgentSettings.stream_timeout_seconds)),
        first_byte_timeout_seconds=float(agent_data.get("first_byte_timeout_seconds", 0.0)),
        stream_max_attempts=int(agent_data.get("stream_max_attempts", AgentSettings.stream_max_attempts)),
        stream_retry_delay_seconds=float(agent_data.get("stream_retry_delay_seconds", AgentSettings.stream_retry_delay_seconds)),
        stream_retryable_substrings=tuple(
            str(item).strip()
            for item in raw_stream_retryable
            if str(item).strip()
        ),
    )

    # Token 预算
    budget_data = settings_data.get("token_budget", {})
    # Unknown providers use Claude Code's 200K default unless the host overrides it.
    default_total = resolve_context_window(getattr(llm, "model", ""))
    token_budget = TokenBudget(
        total=budget_data.get("total", default_total),
        system_prompt=budget_data.get("system_prompt", 2_000),
        active_skills=budget_data.get("active_skills", 4_000),
        memory_index=budget_data.get("memory_index", 1_000),
        tool_schemas=budget_data.get("tool_schemas", 6_000),
        agent_state=budget_data.get("agent_state", 2_000),
        response_reserve=budget_data.get("response_reserve", 16_384),
    )

    return AppConfig(
        llm=llm,
        token_budget=token_budget,
        permissions=permissions,
        agent=agent,
        feature_flags=feature_flags,
    )
