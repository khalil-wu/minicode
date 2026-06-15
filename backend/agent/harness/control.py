from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.skills.names import normalize_skill_name
from backend.tools.base import ToolResult


CONTROL_TOOL_NAMES = {"load_skill", "unload_skill", "list_skills", "ask_user"}


@dataclass
class RoutedToolResult:
    result: ToolResult
    events: list[AgentEvent] = field(default_factory=list)


class ControlToolRouter:
    """Route agent control tools that are implemented by the runtime harness."""

    def __init__(
        self,
        *,
        state: AgentState,
        approval_handler: Callable | None,
        skill_manager: Any | None,
    ) -> None:
        self.state = state
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager

    async def execute(self, tc: ToolCallEvent) -> RoutedToolResult | None:
        if tc.name == "ask_user" and self.approval_handler:
            return await self._ask_user(tc)
        if tc.name == "load_skill" and self.skill_manager:
            return self._load_skill(tc)
        if tc.name == "unload_skill" and self.skill_manager:
            return self._unload_skill(tc)
        if tc.name == "list_skills" and self.skill_manager:
            return self._list_skills()
        return None

    async def _ask_user(self, tc: ToolCallEvent) -> RoutedToolResult:
        question = tc.arguments.get("question", "")
        event = AgentEvent(type="ask_user", data={"tool_call_id": tc.id, "question": question})
        answer_data = await self.approval_handler(tc.id)
        answer = answer_data.get("answer", answer_data.get("guidance", ""))
        return RoutedToolResult(
            result=ToolResult(content=f"User answer: {answer}"),
            events=[event],
        )

    def _load_skill(self, tc: ToolCallEvent) -> RoutedToolResult:
        name = normalize_skill_name(tc.arguments.get("skill_name"))
        if not name:
            result = ToolResult(
                content="Missing required argument: skill_name",
                is_error=True,
                display_summary="Missing skill name",
                result_kind="skill",
            )
        elif name in self.state.active_skills:
            result = ToolResult(
                content=f"Skill '{name}' is already active",
                display_summary=f"Skill already active: {name}",
                result_kind="skill",
            )
        elif self.skill_manager.activate(name):
            if name not in self.state.active_skills:
                self.state.active_skills.append(name)
            result = ToolResult(
                content=f"Skill '{name}' activated",
                display_summary=f"Activated skill: {name}",
                result_kind="skill",
            )
        else:
            result = ToolResult(
                content=f"Skill '{name}' activation failed",
                is_error=True,
                display_summary=f"Skill activation failed: {name}",
                result_kind="skill",
            )
        return RoutedToolResult(result=result)

    def _unload_skill(self, tc: ToolCallEvent) -> RoutedToolResult:
        name = normalize_skill_name(tc.arguments.get("skill_name"))
        if not name:
            result = ToolResult(
                content="Missing required argument: skill_name",
                is_error=True,
                display_summary="Missing skill name",
                result_kind="skill",
            )
        elif self.skill_manager.deactivate(name):
            self.state.active_skills = [skill for skill in self.state.active_skills if skill != name]
            result = ToolResult(
                content=f"Skill '{name}' deactivated",
                display_summary=f"Deactivated skill: {name}",
                result_kind="skill",
            )
        else:
            result = ToolResult(
                content=f"Skill '{name}' is not active",
                is_error=True,
                display_summary=f"Skill is not active: {name}",
                result_kind="skill",
            )
        return RoutedToolResult(result=result)

    def _list_skills(self) -> RoutedToolResult:
        skills = self.skill_manager.list_all()
        lines = ["Available Skills:"] + [
            f"  [{'active' if skill.get('active') else 'inactive'}] {skill['name']}: {skill.get('description', '')}"
            for skill in skills
        ]
        return RoutedToolResult(
            result=ToolResult(
                content="\n".join(lines),
                display_summary="Listed skills",
                result_kind="skill",
            )
        )
