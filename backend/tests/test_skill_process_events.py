import asyncio
from pathlib import Path

from backend.agent.control_tools import ControlToolRouter
from backend.agent.run_context import RunContext
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.skills.loader import SkillFull, SkillMeta
from backend.skills.manager import SkillManager


class _Loader:
    def __init__(self, skills: list[SkillFull]) -> None:
        self.skills = list(skills)

    def discover(self):
        return [skill.meta for skill in self.skills]

    def list_skill_names(self):
        return list(dict.fromkeys(skill.meta.name for skill in self.skills))

    def get_metas(self, name: str):
        return [skill.meta for skill in self.skills if skill.meta.name == name]

    def get_unambiguous_meta(self, name: str):
        matches = self.get_metas(name)
        return matches[0] if len(matches) == 1 else None

    def get_meta_by_path(self, path):
        if not path:
            return None
        target = Path(path).resolve()
        return next((skill.meta for skill in self.skills if skill.meta.source_path.resolve() == target), None)

    def load_full(self, name: str, path=None):
        meta = self.get_meta_by_path(path) if path else self.get_unambiguous_meta(name)
        return next((skill for skill in self.skills if skill.meta is meta), None)

    def get_all_layer1(self):
        return "\n".join(skill.meta.to_layer1_summary() for skill in self.skills)

    def list_metas(self):
        return [skill.meta for skill in self.skills]


def _manager(tmp_path: Path) -> tuple[SkillManager, Path]:
    skill_path = tmp_path / "skills" / "frontend-dev" / "SKILL.md"
    skill = SkillFull(
        SkillMeta(
            name="frontend-dev",
            description="Frontend workflow",
            source_path=skill_path,
            source_level="workspace",
        ),
        "Use React patterns.",
        "---\nname: frontend-dev\ndescription: Frontend workflow\n---\nUse React patterns.",
    )
    return SkillManager(_Loader([skill])), skill_path


def test_explicit_skill_selection_stages_turn_owned_payload_without_lifecycle_events(tmp_path) -> None:
    manager, skill_path = _manager(tmp_path)
    state = AgentState(user_message="Use the selected workflow")
    state.prompt_context["selected_skills"] = [
        {"name": "frontend-dev", "path": str(skill_path)},
    ]

    async def run() -> list:
        return [event async for event in activate_turn_skills(manager, state.user_message, state)]

    events = asyncio.run(run())

    assert events == []
    assert state.active_skills == ["frontend-dev"]
    assert state.prompt_context["skill_injections"] == [{
        "name": "frontend-dev",
        "path": str(skill_path),
        "source_level": "workspace",
        "description": "Frontend workflow",
        "content": "---\nname: frontend-dev\ndescription: Frontend workflow\n---\nUse React patterns.",
        "token_estimate": len("Use React patterns.") // 4,
    }]


def test_plain_words_do_not_activate_a_skill(tmp_path) -> None:
    manager, _ = _manager(tmp_path)
    state = AgentState(user_message="Use React patterns for this page")

    async def run() -> list:
        return [event async for event in activate_turn_skills(manager, state.user_message, state)]

    assert asyncio.run(run()) == []
    assert "skill_injections" not in state.prompt_context
    assert state.active_skills == []


def test_ask_user_emits_pre_wait_event_before_awaiting_answer() -> None:
    async def approval_handler(tool_call_id: str) -> dict[str, str]:
        assert tool_call_id == "ask-1"
        return {"answer": "yes"}

    router = ControlToolRouter(
        state=AgentState(user_message="Confirm location"),
        approval_handler=approval_handler,
        skill_manager=None,
    )
    tool_call = ToolCallEvent(
        id="ask-1",
        name="ask_user",
        arguments={"question": "Use this location?"},
    )

    pre_events = router.pre_wait_events(tool_call)
    result = asyncio.run(router.execute(tool_call))

    assert len(pre_events) == 1
    assert pre_events[0].type == "ask_user"
    assert pre_events[0].data["question"] == "Use this location?"
    assert result is not None
    assert result.events == []
    assert result.result.content == "User answer: yes"


def test_ask_user_pre_wait_event_sanitizes_options() -> None:
    router = ControlToolRouter(
        state=AgentState(user_message="Clean files"),
        approval_handler=lambda _tool_call_id: None,
        skill_manager=None,
    )
    tool_call = ToolCallEvent(
        id="ask-2",
        name="ask_user",
        arguments={
            "question": "Delete temporary files?",
            "options": ["Delete", "Keep", "Delete", "", "Extra"],
        },
    )

    [event] = router.pre_wait_events(tool_call)

    assert event.type == "ask_user"
    assert event.data["options"] == ["Delete", "Keep"]


def test_ask_user_triggers_elicitation_result_hook() -> None:
    class HookRecorder:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run_elicitation_result(self, **kwargs):
            self.calls.append(dict(kwargs))

    hook_manager = HookRecorder()

    async def approval_handler(tool_call_id: str) -> dict[str, str]:
        assert tool_call_id == "ask-1"
        return {"answer": "yes"}

    router = ControlToolRouter(
        state=AgentState(user_message="Confirm location"),
        approval_handler=approval_handler,
        skill_manager=None,
        hook_manager=hook_manager,
    )
    tool_call = ToolCallEvent(
        id="ask-1",
        name="ask_user",
        arguments={"question": "Use this location?"},
    )

    result = asyncio.run(router.execute(tool_call))

    assert result is not None
    assert hook_manager.calls == [{
        "mcp_server_name": "ask_user",
        "elicitation_id": "ask-1",
        "action": "accept",
        "content": {"answer": "yes"},
        "mode": "control",
    }]
