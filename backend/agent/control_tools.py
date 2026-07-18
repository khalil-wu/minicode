from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.skill_events import skill_process_event
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.skills.names import normalize_skill_name
from backend.tools.base import ToolResult


CONTROL_TOOL_NAMES = {"load_skill", "unload_skill", "list_skills", "ask_user"}
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
    ) -> None:
        self.state = state
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager

    def pre_wait_events(self, tc: ToolCallEvent) -> list[AgentEvent]:
        if tc.name == "ask_user" and self.approval_handler:
            return [self._ask_user_event(tc)]
        return []

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

    def _ask_user_event(self, tc: ToolCallEvent) -> AgentEvent:
        question = tc.arguments.get("question", "")
        data: dict[str, Any] = {"tool_call_id": tc.id, "question": question}
        options = _sanitize_ask_user_options(tc.arguments.get("options"))
        if options:
            data["options"] = options
        return AgentEvent(type="ask_user", data=data)

    async def _ask_user(self, tc: ToolCallEvent) -> RoutedToolResult:
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

    def _load_skill(self, tc: ToolCallEvent) -> RoutedToolResult:
        name = normalize_skill_name(
            tc.arguments.get("skill_name")
            or tc.arguments.get("skillName")
            or tc.arguments.get("skill")
            or tc.arguments.get("name")
        )
        events: list[AgentEvent] = []
        if not name:
            result = ToolResult(
                content="Missing required argument: skill_name",
                is_error=True,
                display_summary="Missing skill name",
                result_kind="skill",
            )
        elif name in self.state.active_skills or _skill_is_active(self.skill_manager, name):
            if name not in self.state.active_skills and _skill_is_active(self.skill_manager, name):
                self.state.active_skills.append(name)
            events.append(skill_process_event(
                name,
                lifecycle="skipped",
                trigger_mode="model",
                status="info",
                reason="Skill already active",
                skill_manager=self.skill_manager,
            ))
            result = ToolResult(
                content=f"Skill '{name}' is already active",
                display_summary=f"Skill already active: {name}",
                result_kind="skill",
            )
        else:
            events.append(skill_process_event(
                name,
                lifecycle="selected",
                trigger_mode="model",
                reason="模型请求加载该 skill",
                skill_manager=self.skill_manager,
            ))
            active_before = set(_active_skill_names(self.skill_manager))
            if self.skill_manager.activate(name):
                if name not in self.state.active_skills:
                    self.state.active_skills.append(name)
                active_after = set(_active_skill_names(self.skill_manager))
                for removed_name in sorted(active_before - active_after):
                    self.state.active_skills = [skill for skill in self.state.active_skills if skill != removed_name]
                    events.append(skill_process_event(
                        removed_name,
                        lifecycle="skipped",
                        trigger_mode="model",
                        status="info",
                        reason=f"与 {name} 冲突，已自动停用",
                        skill_manager=self.skill_manager,
                    ))
                events.append(skill_process_event(
                    name,
                    lifecycle="loaded",
                    trigger_mode="model",
                    reason="模型请求加载该 skill",
                    skill_manager=self.skill_manager,
                ))
                result = ToolResult(
                    content=f"Skill '{name}' activated",
                    display_summary=f"Activated skill: {name}",
                    result_kind="skill",
                )
            else:
                events.append(skill_process_event(
                    name,
                    lifecycle="failed",
                    trigger_mode="model",
                    status="failed",
                    reason=f"Skill '{name}' activation failed",
                    skill_manager=self.skill_manager,
                ))
                result = ToolResult(
                    content=f"Skill '{name}' activation failed",
                    is_error=True,
                    display_summary=f"Skill activation failed: {name}",
                    result_kind="skill",
                )
        return RoutedToolResult(result=result, events=events)

    def _unload_skill(self, tc: ToolCallEvent) -> RoutedToolResult:
        name = normalize_skill_name(
            tc.arguments.get("skill_name")
            or tc.arguments.get("skillName")
            or tc.arguments.get("skill")
            or tc.arguments.get("name")
        )
        events: list[AgentEvent] = []
        if not name:
            result = ToolResult(
                content="Missing required argument: skill_name",
                is_error=True,
                display_summary="Missing skill name",
                result_kind="skill",
            )
        elif self.skill_manager.deactivate(name):
            self.state.active_skills = [skill for skill in self.state.active_skills if skill != name]
            events.append(skill_process_event(
                name,
                lifecycle="unloaded",
                trigger_mode="model",
                status="info",
                reason="模型请求停用该 skill",
                skill_manager=self.skill_manager,
            ))
            result = ToolResult(
                content=f"Skill '{name}' deactivated",
                display_summary=f"Deactivated skill: {name}",
                result_kind="skill",
            )
        else:
            events.append(skill_process_event(
                name,
                lifecycle="skipped",
                trigger_mode="model",
                status="info",
                reason="Skill is not active",
                skill_manager=self.skill_manager,
            ))
            result = ToolResult(
                content=f"Skill '{name}' is not active",
                display_summary=f"Skill is not active: {name}",
                result_kind="skill",
            )
        return RoutedToolResult(result=result, events=events)

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


def _active_skill_names(skill_manager: Any | None) -> list[str]:
    get_active_names = getattr(skill_manager, "get_active_names", None)
    if callable(get_active_names):
        try:
            return list(get_active_names())
        except Exception as exc:
            logger.debug("skill_manager.get_active_names() failed: %s", exc)
            return []
    active = getattr(skill_manager, "_active", None)
    if isinstance(active, dict):
        return list(active.keys())
    return []


def _skill_is_active(skill_manager: Any | None, name: str) -> bool:
    is_active = getattr(skill_manager, "is_active", None)
    if callable(is_active):
        try:
            return bool(is_active(name))
        except Exception as exc:
            logger.debug("skill_manager.is_active(%s) failed: %s", name, exc)
            return False
    return name in _active_skill_names(skill_manager)


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
