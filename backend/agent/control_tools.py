from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
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
        await_response: Callable[[ToolCallEvent], Any] | None = None,
    ) -> None:
        self.state = state
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager
        # Optional deadline-aware waiter supplied by the executor. ask_user is
        # unbounded work owned by the turn, so it must honour the same
        # wall-clock boundary as tool execution when the caller provides one.
        self.await_response = await_response

    def pre_wait_events(self, tc: ToolCallEvent) -> list[AgentEvent]:
        if tc.name == "ask_user" and self.approval_handler:
            return [self._ask_user_event(tc)]
        return []

    async def execute(self, tc: ToolCallEvent) -> RoutedToolResult | None:
        if tc.name == "ask_user" and self.approval_handler:
            return await self._ask_user(tc)
        return None

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
        answer = answer_data.get("answer", answer_data.get("guidance", ""))
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
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
        return RoutedToolResult(
            result=ToolResult(content=f"User answer: {answer}"),
        )

def _sanitize_ask_user_options(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    options: list[str] = []
    seen: set[str] = set()
    for item in raw[:4]:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        options.append(text[:80])
    return options
