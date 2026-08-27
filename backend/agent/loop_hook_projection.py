"""Project preflight hook results into canonical turn context and events."""

from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent


def apply_hook_results(
    *,
    context: Any,
    user_message: str,
    hook_results: tuple[Any | None, ...],
) -> list[AgentEvent]:
    """Apply hook context once and return only public system notices."""
    events: list[AgentEvent] = []
    for hook_result in hook_results:
        if hook_result is None:
            continue
        additional_context = str(
            getattr(hook_result, "additional_context", "") or ""
        ).strip()
        if additional_context:
            context.append_user_context(additional_context)
        system_message = str(
            getattr(hook_result, "system_message", "") or ""
        ).strip()
        if system_message:
            events.append(AgentEvent(type="system_notice", data={"content": system_message}))
        initial_user_message = str(
            getattr(hook_result, "initial_user_message", "") or ""
        ).strip()
        if initial_user_message and initial_user_message != str(user_message or "").strip():
            context.append_user_context(initial_user_message)
    return events


__all__ = ["apply_hook_results"]
