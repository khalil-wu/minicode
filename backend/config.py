"""
MiniCode 统一配置中心。

从环境变量和 settings.json 读取所有配置项，是系统的唯一配置来源。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# ── 项目根目录（backend/ 的父目录）──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 自动加载 .env ─────────────────────────────────────────────
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

# ── settings.json 路径 ─────────────────────────────────────────
SETTINGS_FILE = PROJECT_ROOT / "settings.json"


class SettingsError(RuntimeError):
    """配置加载失败时抛出。"""


@dataclass(frozen=True)
class LLMSettings:
    """LLM 连接配置。"""

    api_key: str
    base_url: str = "https://lucen.cc"
    model: str = "gpt-5.4"
    reasoning_effort: str = "high"
    max_tokens: int = 8192
    wire_api: str = "responses"  # "responses" 或 "chat" (Chat Completions)


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
            "ask_user",
            "read_artifact",
            "load_skill",
            "read_memory",
            "mcp__memory_rag__*",
            "mcp__code_index__search",
        ]
    )
    require_confirm: list[str] = field(
        default_factory=lambda: [
            "run_command",
            "web_fetch",
            "mcp__websearch__*",
        ]
    )
    require_diff_review: list[str] = field(
        default_factory=lambda: [
            "write_file",
            "edit_file",
        ]
    )
    always_deny: list[str] = field(default_factory=list)
    path_allowlist: list[str] = field(
        default_factory=lambda: ["./src", "./tests", "./backend", "./frontend"]
    )
    path_denylist: list[str] = field(
        default_factory=lambda: [".env", "*.key", "*.pem", "secrets/"]
    )


@dataclass(frozen=True)
class AgentSettings:
    """Agent Loop 运行参数。"""

    max_iterations: int = 30
    compaction_threshold: float = 0.75  # token 使用率阈值
    stagnation_limit: int = 3  # 同一工具+参数调用 N 次判定停滞
    history_keep_recent: int = 15  # compaction 后保留最近 N 轮


@dataclass
class AppConfig:
    """应用总配置。"""

    llm: LLMSettings = field(default_factory=LLMSettings)
    token_budget: TokenBudget = field(default_factory=TokenBudget)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)


def _load_settings_json() -> dict:
    """从 settings.json 读取配置，文件不存在则返回空字典。"""
    if not SETTINGS_FILE.exists():
        return {}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_llm_settings() -> LLMSettings:
    """从环境变量加载 LLM 配置。"""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://lucen.cc").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5.4").strip()
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "high").strip()
    wire_api = os.getenv("OPENAI_WIRE_API", "responses").strip()

    if not api_key:
        raise SettingsError("Missing OPENAI_API_KEY")

    return LLMSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=reasoning_effort,
        wire_api=wire_api,
    )


def load_config() -> AppConfig:
    """加载完整应用配置：环境变量 + settings.json。"""
    settings_data = _load_settings_json()

    # LLM 来自环境变量
    try:
        llm = load_llm_settings()
    except SettingsError:
        # 开发模式下允许无 API key 启动
        llm = LLMSettings(api_key="", base_url="", model="", reasoning_effort="")

    # 权限来自 settings.json
    perm_data = settings_data.get("permissions", {})
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
        path_denylist=perm_data.get(
            "path_denylist", PermissionSettings().path_denylist
        ),
    )

    # Agent 参数来自 settings.json
    agent_data = settings_data.get("agent", {})
    agent = AgentSettings(
        max_iterations=agent_data.get("max_iterations", 30),
        compaction_threshold=agent_data.get("compaction_threshold", 0.75),
        stagnation_limit=agent_data.get("stagnation_limit", 3),
        history_keep_recent=agent_data.get("history_keep_recent", 15),
    )

    # Token 预算
    budget_data = settings_data.get("token_budget", {})
    token_budget = TokenBudget(
        total=budget_data.get("total", 128_000),
        system_prompt=budget_data.get("system_prompt", 2_000),
        tool_schemas=budget_data.get("tool_schemas", 6_000),
    )

    return AppConfig(
        llm=llm,
        token_budget=token_budget,
        permissions=permissions,
        agent=agent,
    )
