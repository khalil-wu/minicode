from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.loop_preflight import PhaseDeadlineExceeded, await_preflight
from backend.agent.state import AgentState
from backend.agent.tool_execution_guardrails import rejection_result
from backend.llm.base import ToolCallEvent
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import ToolResult


CONTROL_TOOL_NAMES = {"ask_user"}
logger = logging.getLogger(__name__)


@dataclass
class RoutedToolResult:
    result: ToolResult
    events: list[AgentEvent] = field(default_factory=list)


class ControlToolRouter:
    """Route agent control tools implemented by the agent runtime."""

    def __init__(
        self,
        *,
        state: AgentState,
        approval_handler: Callable | None,
        skill_manager: Any | None,
        tool_context: ToolExecutionContext,
        hook_manager: Any | None = None,
        await_response: Callable[[ToolCallEvent], Any] | None = None,
    ) -> None:
        self.state = state
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager
        self.hook_manager = hook_manager
        self.tool_context = tool_context
        # Optional deadline-aware waiter supplied by the executor. ask_user is
        # unbounded work owned by the turn, so it must honour the same
        # wall-clock boundary as tool execution when the caller provides one.
        self.await_response = await_response

    async def run(self, tc: ToolCallEvent) -> AsyncIterator[AgentEvent | RoutedToolResult]:
        """Own the elicitation gate, question, wait, and result in that order."""
        if tc.name != "ask_user":
            return
        if self.approval_handler is None and self.await_response is None:
            yield RoutedToolResult(result=ToolResult(
                content="The host cannot receive user input. The question was not shown; do not assume an answer.",
                is_error=True, status="blocked",
            ))
            return
        if self.hook_manager is not None:
            context = self.tool_context
            try:
                start = await await_preflight(
                    self.hook_manager.run_elicitation(
                        str(tc.arguments.get("question", "")),
                        mcp_server_name="ask_user", elicitation_id=tc.id, mode="control",
                    ),
                    deadline=context.deadline_monotonic, cancel_event=context.cancel_event,
                    run_context=context.run_context, llm=context.llm,
                )
            except PhaseDeadlineExceeded:
                yield RoutedToolResult(result=ToolResult(
                    content="Turn deadline reached before the question was shown to the user.",
                    is_error=True, status="timeout", display_summary="Elicitation start timed out",
                ))
                return
            if start.blocked:
                message = start.message or start.feedback or "elicitation blocked by hook"
                yield RoutedToolResult(result=rejection_result(
                    tc, f"Elicitation blocked by hook: {message}",
                    display_summary="Question blocked by hook", error_kind="hook_blocked",
                    user_summary="澄清问题被钩子拒绝，未向用户提问。",
                    model_observation="The question was not shown. Do not assume a user answer.",
                ))
                return
        yield self._ask_user_event(tc)
        yield await self._ask_user(tc)

    def _ask_user_event(self, tc: ToolCallEvent) -> AgentEvent:
        question = tc.arguments.get("question", "")
        data: dict[str, Any] = {"tool_call_id": tc.id, "question": question}
        options = _sanitize_ask_user_options(tc.arguments.get("options"))
        if options:
            data["options"] = options
        return AgentEvent(type="ask_user", data=data)

    async def _ask_user(self, tc: ToolCallEvent) -> RoutedToolResult:
        if self.await_response is not None:
            answer_data = await self.await_response(tc)
        else:
            answer_data = await self.approval_handler(tc.id)
        action = str(answer_data.get("action") or "").strip().lower()
        answer = "" if action in {"reject", "cancel", "deny", "decline"} else str(answer_data.get("answer") or "").strip()
        if not answer and action in {"approve", "accept"}:
            answer = str(answer_data.get("guidance") or "").strip()
        hook_mgr = self.hook_manager
        if hook_mgr:
            try:
                await hook_mgr.run_elicitation_result(
                    mcp_server_name="ask_user",
                    elicitation_id=tc.id,
                    action="accept" if answer else "cancel",
                    content={"answer": answer},
                    mode="control",
                )
            except Exception as exc:
                logger.debug("MCP elicitation response failed (harmless): %s", exc)
        if not answer:
            # A dismissed question is not an answer. Reporting ``User answer: ``
            # as a successful reply let the model invent the decision it had just
            # asked about and act on it. cc treats an unanswered prompt as a
            # refusal, so say so explicitly instead.
            dismissed = action == "reject" and not answer_data.get("guidance")
            return RoutedToolResult(
                result=ToolResult(
                    content=(
                        "The user dismissed the question without answering."
                        if dismissed
                        else "The user did not answer the question."
                    )
                    + " Do not assume an answer: stop and report what you need,"
                    " or ask one narrower question.",
                    status="partial",
                ),
            )
        return RoutedToolResult(
            result=ToolResult(content=f"User answer: {answer}"),
        )

def _sanitize_ask_user_options(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    options: list[str] = []
    seen: set[str] = set()
    for item in raw[:4]:
        text = str(item or "").strip()[:80]
        if not text or text in seen:
            continue
        seen.add(text)
        options.append(text)
    return options
