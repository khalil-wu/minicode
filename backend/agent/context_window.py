"""Codex TokenBudget window state, separate from the UI's prompt budget."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from backend.llm.capabilities import capabilities_for_adapter


TOKEN_BUDGET_REMINDER_TEMPLATE = (
    "Your context window is nearly exhausted (only {n_remaining} tokens remaining) "
    "and will be automatically reset for you soon. Once reset, message items in "
    "current context window will be cleared in the new window, but notes and "
    "history items will be persistent across windows."
)
NEW_CONTEXT_MESSAGE = "A new context window will start without summarizing conversation history."
CONTEXT_TOOL_NAMES = frozenset({"new_context", "get_context_remaining"})


def is_context_window_rollover(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """An empty summary succeeds only for an actual archived window advance."""
    window = after.get("context_window_state")
    archives = after.get("inactive_context_windows", [])
    if window is None or not archives:
        return False
    previous = archives[-1]["context_window_state"]
    before_window = before.get("context_window_state")
    return (
        window["window_number"] == previous["window_number"] + 1
        and window["first_window_id"] == previous["first_window_id"]
        and window["previous_window_id"] == previous["window_id"]
        and window["window_id"] != previous["window_id"]
        and (before_window is None or (
            before_window["window_id"] == previous["window_id"]
            and before_window["window_number"] == previous["window_number"]
        ))
    )


def new_window_id() -> str:
    """UUID v7, matching AutoCompactWindowIds::new_initial/advance."""
    timestamp = time.time_ns() // 1_000_000
    return str(UUID(int=(timestamp << 80) | (7 << 76) | (secrets.randbits(12) << 64)
                    | (2 << 62) | secrets.randbits(62)))


@dataclass(frozen=True)
class ContextWindowConfig:
    model_context_window: int | None = None
    model_auto_compact_token_limit: int | None = None
    model_auto_compact_token_limit_scope: Literal["total", "body_after_prefix"] = "total"
    use_history_notes_extension: bool = False
    reminder_threshold_tokens: int | None = None
    reminder_message_template: str = TOKEN_BUDGET_REMINDER_TEMPLATE
    guidance_message: str | None = None
    auto_compact_fallback_prompt: str | None = None
    auto_compact_fallback_buffer_tokens: int | None = None

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "ContextWindowConfig":
        features = settings.get("features", {})
        token_budget = features.get("token_budget", False)
        options = {key: value for key, value in token_budget.items() if key != "enabled"} if isinstance(token_budget, dict) else {}
        config = cls(**{
            key: settings[key] for key in (
                "model_context_window", "model_auto_compact_token_limit",
                "model_auto_compact_token_limit_scope",
            ) if key in settings
        }, **options)
        config.validate()
        return config

    def validate(self) -> None:
        # These are Source configuration errors, not speculative recovery.
        if self.model_auto_compact_token_limit_scope not in {"total", "body_after_prefix"}:
            raise ValueError("model_auto_compact_token_limit_scope must be total or body_after_prefix")
        for name in ("reminder_threshold_tokens", "auto_compact_fallback_buffer_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"features.token_budget.{name} must be positive")
        if not isinstance(self.use_history_notes_extension, bool):
            raise ValueError("features.token_budget.use_history_notes_extension must be boolean")
        for name in ("reminder_message_template", "guidance_message", "auto_compact_fallback_prompt"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"features.token_budget.{name} must be a string")
        if not self.reminder_message_template.strip():
            raise ValueError("features.token_budget.reminder_message_template must not be empty")
        for name in ("reminder_message_template", "guidance_message", "auto_compact_fallback_prompt"):
            value = getattr(self, name)
            if value is not None and len(value.encode("utf-8")) > 2_000:
                raise ValueError(f"features.token_budget.{name} must not exceed 2000 bytes")
        if self.auto_compact_fallback_prompt is not None and self.auto_compact_fallback_buffer_tokens is None:
            raise ValueError("features.token_budget.auto_compact_fallback_buffer_tokens is required when auto_compact_fallback_prompt is set")

    def limits(self, llm: Any) -> tuple[int | None, int | None]:
        capabilities = capabilities_for_adapter(llm)
        model_window = self.model_context_window
        if model_window is not None and capabilities.max_context_window_verified and capabilities.max_context_window:
            model_window = min(model_window, int(capabilities.max_context_window))
        if model_window is None:
            model_window = int(capabilities.context_window or capabilities.max_context_window) or None
        default_limit = model_window * 9 // 10 if model_window is not None else None
        if self.model_auto_compact_token_limit_scope == "body_after_prefix":
            auto_limit = self.model_auto_compact_token_limit if self.model_auto_compact_token_limit is not None else default_limit
        else:
            auto_limit = min(default_limit, self.model_auto_compact_token_limit) if default_limit is not None and self.model_auto_compact_token_limit is not None else default_limit if default_limit is not None else self.model_auto_compact_token_limit
        full_limit = model_window * 95 // 100 if model_window is not None else None
        return auto_limit, full_limit

    @property
    def fallback_buffer_tokens(self) -> int:
        return self.auto_compact_fallback_buffer_tokens if self.auto_compact_fallback_prompt is not None else 0


@dataclass
class ContextWindowState:
    first_window_id: str = field(default_factory=new_window_id)
    window_id: str = ""
    previous_window_id: str | None = None
    window_number: int = 0
    new_context_requested: bool = False
    last_response_total_tokens: int | None = None
    prefill_input_tokens: int | None = None
    prefill_server_observed: bool = False
    reminder_delivered: bool = False
    fallback_delivered: bool = False

    def __post_init__(self) -> None:
        if not self.window_id:
            self.window_id = self.first_window_id

    def advance(self) -> None:
        self.previous_window_id = self.window_id
        self.window_id = new_window_id()
        self.window_number += 1
        self.new_context_requested = False
        self.last_response_total_tokens = None
        self.prefill_input_tokens = None
        self.prefill_server_observed = False
        self.reminder_delivered = False
        self.fallback_delivered = False

    def status(self, config: ContextWindowConfig, llm: Any, active_tokens: int) -> dict[str, Any]:
        auto_limit, full_limit = config.limits(llm)
        baseline = self.prefill_input_tokens if self.prefill_input_tokens is not None else active_tokens
        scoped_tokens = max(0, active_tokens - baseline) if config.model_auto_compact_token_limit_scope == "body_after_prefix" else active_tokens
        remaining = [max(0, limit - used) for limit, used in (
            (auto_limit, scoped_tokens), (full_limit, active_tokens),
        ) if limit is not None]
        return {
            "active_context_tokens": active_tokens,
            "auto_compact_scope_tokens": scoped_tokens,
            "auto_compact_scope_limit": auto_limit,
            "full_context_window_limit": full_limit,
            "tokens_left": min(remaining) if remaining else None,
            "token_limit_reached": (
                auto_limit is not None and scoped_tokens >= auto_limit + config.fallback_buffer_tokens
                or full_limit is not None and active_tokens >= full_limit
            ),
        }

    def render(self, agent_path: str, thread_hint: str | None = None) -> str:
        lines = [
            f"Agent name: {agent_path}",
            f"First context window id: {self.first_window_id}",
            f"Current context window id: {self.window_id}",
        ]
        if self.previous_window_id is not None:
            lines.append(f"Previous context window id: {self.previous_window_id}")
        if thread_hint:
            lines.append(thread_hint)
        return "<context_window>\n" + "\n".join(lines) + "\n</context_window>"

