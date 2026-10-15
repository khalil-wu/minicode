"""Stateless compact-hook control effects shared by automatic and manual tasks."""
from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.hooks.manager import HookEvent, HookResult


class ContextCompactionStopped(RuntimeError):
    def __init__(self, event: HookEvent, outcome: HookResult, *, committed: Any = None):
        label = "PreCompact" if event is HookEvent.PRE_COMPACT else "PostCompact"
        super().__init__(outcome.stop_reason or f"{label} hook stopped execution")
        self.event = event
        self.outcome = outcome
        self.committed = committed


async def run_context_compaction_hook(context: Any, event: HookEvent, *, trigger: str) -> HookResult | None:
    manager = context.hook_manager
    if manager is None or not manager.has_hooks(event):
        return None
    if event is HookEvent.PRE_COMPACT:
        return await manager.run_pre_compact(trigger=trigger)
    return await manager.run_post_compact(trigger=trigger)


def context_compaction_hook_events(outcome: HookResult | None) -> list[AgentEvent]:
    if outcome is None:
        return []
    # These are actual hook warning/error entries, never model instructions.
    messages = [outcome.system_message, *outcome.errors]
    if outcome.prevent_continuation and outcome.stop_reason:
        messages.append(outcome.stop_reason)
    return [AgentEvent("system_notice", {"content": message}) for message in dict.fromkeys(messages) if message]


def stop_context_compaction(state: AgentState) -> None:
    state.stopped_reason = "interrupted"
    state.terminal_status = "cancelled"
