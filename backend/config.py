"""
MiniCode 统一配置中心。

从环境变量和 settings.json 读取所有配置项，是系统的唯一配置来源。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.agent.policies import (
        GroundedReplyPolicy,
        RealtimeSearchPolicy,
        ReflectionPolicy,
        StreamRetryPolicy,
    )

# ── 项目根目录（backend/ 的父目录）──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

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
SETTINGS_FILE = PROJECT_ROOT / "settings.json"


class SettingsError(RuntimeError):
    """配置加载失败时抛出。"""


@dataclass(frozen=True)
class LLMSettings:
    """LLM 连接配置。"""

    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.4"
    reasoning_effort: str = "high"
    max_tokens: int = 8192
    wire_api: str = "responses"  # "responses", "chat", or "anthropic"


@dataclass(frozen=True)
class TokenBudget:
    """Token 预算分配（基于 128K 窗口）。"""

    total: int = 128_000
    system_prompt: int = 2_000
    active_skills: int = 4_000
    memory_index: int = 1_000
    tool_schemas: int = 6_000
    agent_state: int = 2_000
    rag_chunks: int = 8_000
    response_reserve: int = 8_000

    @property
    def history_budget(self) -> int:
        """对话历史可用预算 = 总量 - 其他组件 - 响应预留。"""
        used = (
            self.system_prompt
            + self.active_skills
            + self.memory_index
            + self.tool_schemas
            + self.agent_state
            + self.rag_chunks
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
            "list_skills",
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
            "load_skill",
            "unload_skill",
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
            "save_*",
        ]
    )
    always_deny: list[str] = field(default_factory=list)
    path_allowlist: list[str] = field(
        default_factory=lambda: ["./src", "./tests", "./backend", "./frontend"]
    )
    path_denylist: list[str] = field(
        default_factory=lambda: [".env", ".mcp.json", "settings.json", ".git/**", "*.key", "*.pem", "secrets/"]
    )


@dataclass(frozen=True)
class AgentSettings:
    """Agent Loop 运行参数。"""

    max_iterations: int = 30
    compaction_threshold: float = 0.75  # token 使用率阈值
    stagnation_limit: int = 3  # 同一工具+参数调用 N 次判定停滞
    history_keep_recent: int = 15  # compaction 后保留最近 N 轮
    fallback_providers: tuple[str, ...] = ()  # 主 LLM 失败时按顺序尝试的备用 provider
    reflection_pass: bool = False  # 完成回复后是否执行一次自我审查和修正
    agent_mode: str = "react"

    # Stream retry fields (match current module-level constants in loop.py)
    stream_timeout_seconds: float = 180.0
    stream_max_attempts: int = 2
    stream_retry_delay_seconds: float = 0.8
    stream_retryable_substrings: tuple[str, ...] = (
        "concurrency limit exceeded",
        "retry later",
        "rate limit",
        "too many requests",
        "429",
    )

    # Policy slots — None means the Loop_Core fills in the default implementation
    realtime_search_policy: "RealtimeSearchPolicy | None" = None
    grounded_reply_policy: "GroundedReplyPolicy | None" = None
    reflection_policy: "ReflectionPolicy | None" = None
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


def _normalize_provider(provider: str) -> str:
    value = provider.strip().lower()
    if value in {"openai", "anthropic", "custom"}:
        return value
    if value in {"deepseek", "openrouter", "groq", "together", "fireworks", "moonshot", "qwen"}:
        return "custom"
    return "openai"


def _set_runtime_api_key(provider: str, api_key: str) -> None:
    if not api_key:
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


def _vault_api_key(name: str) -> str:
    try:
        from backend.vault import EnvVault

        value = EnvVault().get(name)
    except Exception:
        return ""
    return str(value or "").strip()


def _provider_api_key(provider: str) -> str:
    if provider == "anthropic":
        return (os.getenv("ANTHROPIC_API_KEY") or _vault_api_key("ANTHROPIC_API_KEY")).strip()
    if provider == "custom":
        return (
            os.getenv("CUSTOM_API_KEY")
            or _vault_api_key("CUSTOM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or _vault_api_key("OPENAI_API_KEY")
        ).strip()
    return (os.getenv("OPENAI_API_KEY") or _vault_api_key("OPENAI_API_KEY")).strip()


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


def _is_anthropic_model_id(model: str) -> bool:
    return model.strip().lower().startswith("claude-")


def _select_anthropic_model(model: str, available_models: list[str], fallback: str = "claude-sonnet-4-6") -> str:
    if _is_anthropic_model_id(model):
        return model.strip()
    for candidate in available_models:
        if _is_anthropic_model_id(candidate):
            return candidate.strip()
    return fallback


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


def _normalize_custom_model(base_url: str, model: str) -> str:
    """Keep OpenAI-compatible presets usable when the upstream has strict IDs.

    DeepSeek V4 正式模型名: deepseek-v4-pro, deepseek-v4-flash
    过渡别名(2026-07-24 废弃): deepseek-chat -> deepseek-v4-flash
    """
    normalized = model.strip()
    host = urlsplit(base_url).netloc.lower()
    if "deepseek.com" in host:
        # 只有空字符串和旧的废弃模型名才回退到 deepseek-v4-flash
        if normalized in {"", "deepseek-coder"}:
            return "deepseek-v4-flash"
        # 保留所有已知的正式模型名（不转换）
        # deepseek-v4-pro, deepseek-v4-flash, deepseek-chat, deepseek-reasoner 等
    return normalized


def get_openai_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("openai", {})
    provider_data = raw if isinstance(raw, dict) else {}

    api_key = _provider_api_key("openai")
    base_url = str(provider_data.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip()
    model = str(provider_data.get("model") or os.getenv("OPENAI_MODEL", "gpt-5.4")).strip()
    reasoning_effort = str(
        provider_data.get("reasoning_effort") or os.getenv("OPENAI_REASONING_EFFORT", "high")
    ).strip()
    wire_api = str(provider_data.get("wire_api") or os.getenv("OPENAI_WIRE_API", "responses")).strip()
    max_tokens = _coerce_int(provider_data.get("max_tokens", os.getenv("OPENAI_MAX_TOKENS", "8192")), 8192)

    available_models = _coerce_model_list(provider_data.get("available_models"))
    if not available_models:
        available_models = _coerce_model_list(os.getenv("OPENAI_AVAILABLE_MODELS", ""))
    if model and model not in available_models:
        available_models.insert(0, model)

    return {
        "api_key": api_key,
        "base_url": _normalize_openai_base_url(base_url),
        "model": model,
        "available_models": available_models,
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "wire_api": wire_api,
    }


def get_anthropic_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("anthropic", {})
    provider_data = raw if isinstance(raw, dict) else {}

    api_key = _provider_api_key("anthropic")
    base_url = str(provider_data.get("base_url") or os.getenv("ANTHROPIC_BASE_URL", "")).strip()
    model = str(
        provider_data.get("model") or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    ).strip()
    max_tokens = _coerce_int(
        provider_data.get("max_tokens", os.getenv("ANTHROPIC_MAX_TOKENS", "8192")),
        8192,
    )
    thinking_budget = _coerce_int(
        provider_data.get("thinking_budget", os.getenv("ANTHROPIC_THINKING_BUDGET", "0")),
        0,
    )

    available_models = _coerce_model_list(provider_data.get("available_models"))
    if not available_models:
        available_models = _coerce_model_list(os.getenv("ANTHROPIC_AVAILABLE_MODELS", ""))
    if model and model not in available_models:
        available_models.insert(0, model)

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "available_models": available_models,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
    }


def get_custom_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read settings for custom/OpenAI-compatible providers (deepseek, openrouter, etc.)."""
    llm_data = _get_llm_section(settings_data)
    raw = llm_data.get("custom", {})
    provider_data = raw if isinstance(raw, dict) else {}

    api_key = _provider_api_key("custom")
    base_url = str(provider_data.get("base_url") or os.getenv("OPENAI_BASE_URL", "")).strip()
    model = str(provider_data.get("model") or os.getenv("OPENAI_MODEL", "")).strip()
    reasoning_effort = str(
        provider_data.get("reasoning_effort") or os.getenv("OPENAI_REASONING_EFFORT", "high")
    ).strip()
    wire_api = _normalize_wire_api(str(provider_data.get("wire_api", "chat")), "chat")
    max_tokens = _coerce_int(provider_data.get("max_tokens", os.getenv("OPENAI_MAX_TOKENS", "8192")), 8192)
    thinking_budget = _coerce_int(
        provider_data.get("thinking_budget", os.getenv("ANTHROPIC_THINKING_BUDGET", "0")),
        0,
    )

    available_models = _coerce_model_list(provider_data.get("available_models"))
    if wire_api == "anthropic":
        model = _select_anthropic_model(model, available_models)
        available_models = [item for item in available_models if _is_anthropic_model_id(item)]
    else:
        model = _normalize_custom_model(base_url, model)
    if "deepseek.com" in urlsplit(base_url).netloc.lower():
        for preset in ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"):
            if preset not in available_models:
                available_models.append(preset)
    if model and model not in available_models:
        available_models.insert(0, model)

    return {
        "api_key": api_key,
        "base_url": (_normalize_openai_base_url(base_url) if base_url and wire_api != "anthropic" else base_url),
        "model": model,
        "available_models": available_models,
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
        "wire_api": wire_api,
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
            "has_api_key": bool(openai["api_key"]),
            "api_key": "",
            "base_url": openai["base_url"],
            "model": openai["model"],
            "available_models": openai["available_models"],
            "reasoning_effort": openai["reasoning_effort"],
            "max_tokens": openai["max_tokens"],
            "wire_api": openai["wire_api"],
        },
        "anthropic": {
            "has_api_key": bool(anthropic["api_key"]),
            "api_key": "",
            "base_url": anthropic["base_url"],
            "model": anthropic["model"],
            "available_models": anthropic["available_models"],
            "max_tokens": anthropic["max_tokens"],
            "thinking_budget": anthropic["thinking_budget"],
        },
        "custom": {
            "has_api_key": bool(custom["api_key"]),
            "api_key": "",
            "base_url": custom["base_url"],
            "model": custom["model"],
            "available_models": custom["available_models"],
            "reasoning_effort": custom["reasoning_effort"],
            "max_tokens": custom["max_tokens"],
            "thinking_budget": custom["thinking_budget"],
            "wire_api": custom["wire_api"],
        },
        "active_model": active_by_provider.get(provider, openai["model"]),
    }


def save_llm_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings_data = _load_settings_json()
    current_openai = get_openai_settings(settings_data)
    current_anthropic = get_anthropic_settings(settings_data)
    current_custom = get_custom_settings(settings_data)

    raw_provider = payload.get("provider")
    provider = _normalize_provider(str(raw_provider or get_llm_provider(settings_data)))

    raw_openai = payload.get("openai", {})
    openai_updates = raw_openai if isinstance(raw_openai, dict) else {}
    openai_api_key = str(openai_updates.get("api_key", "")).strip()
    _set_runtime_api_key("openai", openai_api_key)
    openai_base_url = str(openai_updates.get("base_url", current_openai["base_url"])).strip()
    openai_model = str(openai_updates.get("model", current_openai["model"])).strip()
    openai_reasoning_effort = str(
        openai_updates.get("reasoning_effort", current_openai["reasoning_effort"])
    ).strip()
    openai_wire_api = str(openai_updates.get("wire_api", current_openai["wire_api"])).strip()
    next_openai = {
        "api_key": "",
        "base_url": _normalize_openai_base_url(openai_base_url),
        "model": openai_model or current_openai["model"],
        "available_models": _coerce_model_list(
            openai_updates.get("available_models", current_openai["available_models"])
        ),
        "reasoning_effort": openai_reasoning_effort or current_openai["reasoning_effort"],
        "max_tokens": _coerce_int(
            openai_updates.get("max_tokens", current_openai["max_tokens"]),
            current_openai["max_tokens"],
        ),
        "wire_api": openai_wire_api or current_openai["wire_api"],
    }
    if next_openai["model"] and next_openai["model"] not in next_openai["available_models"]:
        next_openai["available_models"].insert(0, next_openai["model"])

    raw_anthropic = payload.get("anthropic", {})
    anthropic_updates = raw_anthropic if isinstance(raw_anthropic, dict) else {}
    anthropic_api_key = str(anthropic_updates.get("api_key", "")).strip()
    _set_runtime_api_key("anthropic", anthropic_api_key)
    anthropic_base_url = str(anthropic_updates.get("base_url", current_anthropic["base_url"])).strip()
    anthropic_model = str(anthropic_updates.get("model", current_anthropic["model"])).strip()
    next_anthropic = {
        "api_key": "",
        "base_url": anthropic_base_url,
        "model": anthropic_model or current_anthropic["model"],
        "available_models": _coerce_model_list(
            anthropic_updates.get("available_models", current_anthropic["available_models"])
        ),
        "max_tokens": _coerce_int(
            anthropic_updates.get("max_tokens", current_anthropic["max_tokens"]),
            current_anthropic["max_tokens"],
        ),
        "thinking_budget": _coerce_int(
            anthropic_updates.get("thinking_budget", current_anthropic["thinking_budget"]),
            current_anthropic["thinking_budget"],
        ),
    }
    if next_anthropic["model"] and next_anthropic["model"] not in next_anthropic["available_models"]:
        next_anthropic["available_models"].insert(0, next_anthropic["model"])

    raw_custom = payload.get("custom", {})
    custom_updates = raw_custom if isinstance(raw_custom, dict) else {}
    custom_api_key = str(custom_updates.get("api_key", "")).strip()
    _set_runtime_api_key("custom", custom_api_key)
    custom_base_url = str(custom_updates.get("base_url", current_custom["base_url"])).strip()
    custom_model = str(custom_updates.get("model", current_custom["model"])).strip()
    custom_wire_api = _normalize_wire_api(str(custom_updates.get("wire_api", current_custom["wire_api"])), current_custom["wire_api"])
    next_custom_available = _coerce_model_list(
        custom_updates.get("available_models", current_custom["available_models"])
    )
    if custom_wire_api == "anthropic":
        next_custom_available = [item for item in next_custom_available if _is_anthropic_model_id(item)]
        next_custom_model = _select_anthropic_model(custom_model or current_custom["model"], next_custom_available)
    else:
        next_custom_model = _normalize_custom_model(custom_base_url, custom_model or current_custom["model"])

    next_custom = {
        "api_key": "",
        "base_url": (
            _normalize_openai_base_url(custom_base_url)
            if custom_base_url and custom_wire_api != "anthropic"
            else custom_base_url
        ),
        "model": next_custom_model,
        "available_models": next_custom_available,
        "reasoning_effort": str(custom_updates.get("reasoning_effort", current_custom["reasoning_effort"])).strip(),
        "max_tokens": _coerce_int(
            custom_updates.get("max_tokens", current_custom["max_tokens"]),
            current_custom["max_tokens"],
        ),
        "thinking_budget": _coerce_int(
            custom_updates.get("thinking_budget", current_custom["thinking_budget"]),
            current_custom["thinking_budget"],
        ),
        "wire_api": custom_wire_api or current_custom["wire_api"],
    }
    if next_custom["model"] and next_custom["model"] not in next_custom["available_models"]:
        next_custom["available_models"].insert(0, next_custom["model"])

    settings_data["llm"] = {
        "provider": provider,
        "openai": next_openai,
        "anthropic": next_anthropic,
        "custom": next_custom,
    }
    _write_settings_json(settings_data)
    return get_llm_settings_payload(settings_data)


def load_llm_settings(settings_data: dict[str, Any] | None = None) -> LLMSettings:
    """从环境变量与 settings.json 加载 LLM 配置。"""
    active_provider = get_llm_provider(settings_data)

    if active_provider == "anthropic":
        anthropic = get_anthropic_settings(settings_data)
        if not anthropic["api_key"]:
            raise SettingsError("Missing ANTHROPIC_API_KEY")
        return LLMSettings(
            api_key=anthropic["api_key"],
            base_url=anthropic["base_url"],
            model=anthropic["model"],
            reasoning_effort="",
            max_tokens=anthropic["max_tokens"],
            wire_api="responses",
        )

    if active_provider == "custom":
        custom = get_custom_settings(settings_data)
        if not custom["api_key"]:
            raise SettingsError("Missing API key for custom provider")
        return LLMSettings(
            api_key=custom["api_key"],
            base_url=custom["base_url"],
            model=custom["model"],
            reasoning_effort=custom["reasoning_effort"],
            max_tokens=custom["max_tokens"],
            wire_api=custom["wire_api"],
        )

    openai = get_openai_settings(settings_data)
    if not openai["api_key"]:
        raise SettingsError("Missing OPENAI_API_KEY")

    return LLMSettings(
        api_key=openai["api_key"],
        base_url=openai["base_url"],
        model=openai["model"],
        reasoning_effort=openai["reasoning_effort"],
        max_tokens=openai["max_tokens"],
        wire_api=openai["wire_api"],
    )


def load_config() -> AppConfig:
    """加载完整应用配置：环境变量 + settings.json。"""
    settings_data = _load_settings_json()

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

    agent = AgentSettings(
        max_iterations=agent_data.get("max_iterations", 30),
        compaction_threshold=agent_data.get("compaction_threshold", 0.75),
        stagnation_limit=agent_data.get("stagnation_limit", 3),
        history_keep_recent=agent_data.get("history_keep_recent", 15),
        fallback_providers=tuple(fallback_providers),
        reflection_pass=bool(agent_data.get("reflection_pass", False)),
        agent_mode=_normalize_agent_mode(agent_data.get("agent_mode", "react")),
    )

    # Token 预算
    budget_data = settings_data.get("token_budget", {})
    token_budget = TokenBudget(
        total=budget_data.get("total", 128_000),
        system_prompt=budget_data.get("system_prompt", 2_000),
        active_skills=budget_data.get("active_skills", 4_000),
        memory_index=budget_data.get("memory_index", 1_000),
        tool_schemas=budget_data.get("tool_schemas", 6_000),
        agent_state=budget_data.get("agent_state", 2_000),
        rag_chunks=budget_data.get("rag_chunks", 8_000),
        response_reserve=budget_data.get("response_reserve", 8_000),
    )

    return AppConfig(
        llm=llm,
        token_budget=token_budget,
        permissions=permissions,
        agent=agent,
    )
