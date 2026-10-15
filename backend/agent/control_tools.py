from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_user_tools import RequestUserInputTool
from backend.tools.base import ToolResult, validate_tool_input
from backend.tools.subagent_context import resolve_agent_execution_profile

CONTROL_TOOL_NAMES = {"request_user_input"}


@dataclass
class RoutedToolResult:
    result: ToolResult
    events: list[AgentEvent] = field(default_factory=list)


class ControlToolRouter:
    """Run the builtin root-thread user-input control protocol."""

    def __init__(self, *, state: AgentState, approval_handler: Callable | None,
                 skill_manager: Any | None, tool_context: ToolExecutionContext,
                 hook_manager: Any | None = None,
                 await_response: Callable[[ToolCallEvent], Any] | None = None,
                 available_modes: tuple[str, ...] = ("plan",)) -> None:
        self.state = state
        self.approval_handler = approval_handler
        self.tool_context = tool_context
        self.await_response = await_response
        self.available_modes = available_modes

    async def run(self, tc: ToolCallEvent) -> AsyncIterator[AgentEvent | RoutedToolResult]:
        if tc.name != "request_user_input":
            return
        context = self.tool_context
        if resolve_agent_execution_profile(context.permission, context.metadata) is not None:
            yield RoutedToolResult(ToolResult(
                "request_user_input can only be used by the root thread", is_error=True, status="failed"))
            return
        mode = "plan" if self.state.prompt_context.get("collaboration_mode") == "plan" else "default"
        if mode not in self.available_modes:
            yield RoutedToolResult(ToolResult(
                f"request_user_input is unavailable in {mode.title()} mode", is_error=True, status="failed"))
            return
        tool = RequestUserInputTool(default_mode_enabled="default" in self.available_modes)
        validation_error = validate_tool_input(tool, tc.arguments)
        if validation_error:
            yield RoutedToolResult(ToolResult(
                f"Invalid input for request_user_input: {validation_error}", is_error=True, status="failed"))
            return
        questions = deepcopy(tc.arguments["questions"])
        if any(not question["options"] for question in questions):
            yield RoutedToolResult(ToolResult(
                "request_user_input requires non-empty options for every question", is_error=True, status="failed"))
            return
        if self.approval_handler is None and self.await_response is None:
            yield RoutedToolResult(ToolResult(
                "request_user_input requires the host's user input handler. The questions were not shown; do not assume an answer.",
                is_error=True, status="blocked"))
            return
        for question in questions:
            question.update(isOther=True, isSecret=False)
        yield AgentEvent(type="request_user_input", data={
            "tool_call_id": tc.id, "tool_name": tc.name, "questions": questions, "isBlocking": mode == "plan"})
        response = (await self.await_response(tc) if self.await_response is not None
                    else await self.approval_handler(tc.id))
        if response.get("action") in {"reject", "cancel", "deny", "decline"}:
            yield RoutedToolResult(ToolResult(
                "request_user_input was cancelled before receiving a response", is_error=True, status="cancelled"))
            return
        builder = context.run_context.context_builder if context.run_context is not None else None
        if builder is not None:
            evidence = builder.guardian_evidence
            order = response.get("_guardian_acceptance_order")
            if order is None:
                order = evidence.reserve_order()
            evidence.record_verified_response(turn_id=context.turn_id, call_id=tc.id,
                questions=questions, answers=response["answers"], acceptance_order=order)
        yield RoutedToolResult(ToolResult(content=json.dumps({"answers": response["answers"]}, ensure_ascii=False)))
