import asyncio
import os
from pathlib import Path

from backend.agent.control_tools import ControlToolRouter
from backend.agent.context import ContextBuilder
from backend.agent.nested_tool_events import NestedToolEvents
from backend.agent.run_context import RunContext
from backend.agent.skill_activation import activate_turn_skills, implicit_skill_for_tool
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import _finalize_tool_result
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry
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
        "mcp_dependencies": [],
        "mcp_dependency_specs": [],
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


def test_skill_activation_reports_missing_mcp_dependency_to_user_and_model(tmp_path) -> None:
    manager, skill_path = _manager(tmp_path)
    manager._loader.skills[0].meta.mcp_dependencies = ["docs"]
    state = AgentState(user_message="Use $frontend-dev")

    class _McpManager:
        def get_client(self, name: str):
            assert name == "docs"
            return None

    async def run() -> list:
        return [event async for event in activate_turn_skills(manager, state.user_message, state, _McpManager())]

    events = asyncio.run(run())
    assert [event.type for event in events] == ["system_notice"]
    assert "docs" in events[0].data["content"]
    assert "MCP dependency unavailable: docs" in state.prompt_context["skill_injections"][0]["content"]
    assert state.prompt_context["skill_injections"][0]["path"] == str(skill_path)


def test_explicit_skill_installs_approved_mcp_dependency_before_injection(tmp_path, monkeypatch) -> None:
    from backend.services import mcp_service

    manager, _ = _manager(tmp_path)
    manager._loader.skills[0].meta.mcp_dependencies = ["docs"]
    manager._loader.skills[0].meta.mcp_dependency_specs = [{
        "value": "docs", "transport": "streamable_http", "url": "https://docs.example.test/mcp",
    }]
    state = AgentState(user_message="Use $frontend-dev")
    connected: set[str] = set()
    published = []
    installed = []

    class _McpManager:
        def get_client(self, name):
            return object() if name in connected else None

        def get_server_config(self, name):
            return None

    async def publish(event):
        published.append(event)

    async def answer(request_id):
        assert published[0].data["tool_call_id"] == request_id
        return {"answer": "Install"}

    async def install(_mcp_manager, candidates):
        installed.extend(candidates)
        connected.update(candidate["name"] for candidate in candidates)
        return list(connected)

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", install)

    async def run():
        return [event async for event in activate_turn_skills(
            manager, state.user_message, state, _McpManager(), answer, publish,
        )]

    assert asyncio.run(run()) == []
    assert published[0].type == "ask_user"
    assert published[0].data["options"] == ["Install", "Skip"]
    assert installed == [{"name": "docs", "transport": "http", "url": "https://docs.example.test/mcp", "auto_start": True}]
    assert "MCP dependency unavailable" not in state.prompt_context["skill_injections"][0]["content"]


def test_skill_install_prompt_is_delivered_before_waiting_for_answer(tmp_path, monkeypatch) -> None:
    from backend.services import mcp_service

    manager, _ = _manager(tmp_path)
    manager._loader.skills[0].meta.mcp_dependencies = ["docs"]
    manager._loader.skills[0].meta.mcp_dependency_specs = [{
        "value": "docs", "url": "https://docs.example.test/mcp",
    }]
    state = AgentState(user_message="Use $frontend-dev")
    connected = False

    class _McpManager:
        def get_client(self, _name):
            return object() if connected else None

        def get_server_config(self, _name):
            return None

    class _Journal:
        def record_event(self, _event):
            pass

    async def install(_manager, _candidates):
        nonlocal connected
        connected = True
        return ["docs"]

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", install)

    async def run():
        nested = NestedToolEvents(_Journal())
        answered = asyncio.Event()

        async def approve(_request_id):
            await answered.wait()
            return {"answer": "Install"}

        async def producer():
            async for event in activate_turn_skills(
                manager, state.user_message, state, _McpManager(), approve, nested.publish,
            ):
                yield event

        seen = []
        async for event in nested.stream(producer()):
            seen.append(event.type)
            if event.type == "ask_user":
                answered.set()
        return seen

    assert asyncio.run(asyncio.wait_for(run(), 10)) == ["ask_user"]
    assert connected is True


def test_reading_skill_file_tracks_implicit_use_and_dependency(tmp_path) -> None:
    manager, skill_path = _manager(tmp_path)
    manager._loader.skills[0].meta.mcp_dependencies = ["docs"]
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("Use React patterns.", encoding="utf-8")
    state = AgentState(user_message="Inspect the skill", workspace_root=tmp_path)
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path,
        run_context=RunContext(skill_manager=manager),
    )
    call = ToolCallEvent(id="read-skill", name="read_file", arguments={"file_path": str(skill_path)})

    async def run() -> list:
        return [event async for event in _finalize_tool_result(
            call, ToolResult("Use React patterns."), ctx=ContextBuilder(), state=state,
            tool_ctx=context, iteration_id="iter:1", turn_id="turn:1", tool_registry=ToolRegistry(),
        )]

    events = asyncio.run(run())
    assert state.active_skills == ["frontend-dev"]
    assert any(event.type == "system_notice" and "docs" in event.data["content"] for event in events)
    assert "MCP dependency unavailable" in events[-1].data["summary"]


def test_shell_read_and_skill_script_are_implicit_skill_use(tmp_path) -> None:
    manager, skill_path = _manager(tmp_path)
    script = skill_path.parent / "scripts" / "inspect.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")

    read_command = (
        f'Get-Content -LiteralPath "{skill_path}"'
        if os.name == "nt" else f'cat "{skill_path}"'
    )
    read = implicit_skill_for_tool(
        manager, "run_command", {"command": read_command}, tmp_path,
    )
    run = implicit_skill_for_tool(
        manager, "run_command", {"command": f'python "{script}"'}, tmp_path,
    )

    assert read is not None and read.name == "frontend-dev"
    assert run is not None and run.name == "frontend-dev"


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
