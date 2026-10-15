from __future__ import annotations

import asyncio
from pathlib import Path

from backend.agent.context import ContextBuilder
from backend.agent.prompting import PromptParts, PromptSection
from backend.sandbox.policy import ResolvedSandboxPolicy, SandboxEnforcement, NetworkSandboxPolicy
from backend.permissions.context import PermissionContext
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.state import AgentState
from backend.skills.executor import SkillExecutor
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.skill_catalog_assertions import assert_skill_locator


def test_skill_catalog_is_separate_from_system_and_project_user_instructions():
    parts = PromptParts.from_sections([
        PromptSection("stable_system", "model instructions", "stable"),
        PromptSection("skill_context", "## Skills\n- local: workflow", "context"),
        PromptSection("project_guidelines", "AGENTS instructions", "context"),
    ])
    assert "local: workflow" not in parts.render_system()
    assert "local: workflow" not in parts.render_user_instructions()
    assert parts.render_skill_instructions() == "<skills_instructions>\n## Skills\n- local: workflow\n</skills_instructions>"
    assert "AGENTS instructions" in parts.render_user_instructions()


def test_real_skill_selection_catalog_developer_and_verbatim_body_user(tmp_path: Path, monkeypatch):
    source = tmp_path / "skills" / "read-workflow" / "SKILL.md"
    source.parent.mkdir(parents=True)
    raw = "---\nname: read-workflow\ndescription: Read a source file and report findings.\n---\nRead the actual file. Keep the source unchanged.\n"
    source.write_text(raw, encoding="utf-8")
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", tmp_path / "skills")])
    manager = SkillManager(loader)
    state = AgentState(user_message="$read-workflow", workspace_root=tmp_path)
    context = ContextBuilder(skill_executor=SkillExecutor(manager), workspace_root=tmp_path)

    async def prepare():
        events = [event async for event in activate_turn_skills(manager, state.user_message, state, permission_context=PermissionContext(), permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.MANAGED, NetworkSandboxPolicy.RESTRICTED))]
        assert events == []
        await context.start_turn(state.user_message, state)
        return context._render_prompt_messages(state, tmp_path)

    messages = asyncio.run(prepare())
    catalogs = [message for message in messages if "<skills_instructions>" in str(message.content)]
    bodies = [message for message in messages if str(message.content).startswith("<skill>\n")]
    assert len(catalogs) == len(bodies) == 1
    assert catalogs[0].role == "developer"
    assert "read-workflow" in catalogs[0].content
    assert_skill_locator(catalogs[0].content, "read-workflow", source)
    assert bodies[0].role == "user" and raw in bodies[0].content
    assert not any("<skills_instructions>" in str(message.content) for message in messages if message.role == "system")
    snapshot = context.export_snapshot()
    restored = ContextBuilder(skill_executor=SkillExecutor(manager), workspace_root=tmp_path)
    restored.load_snapshot(snapshot)
    restored_messages = restored._render_prompt_messages(state, tmp_path)
    assert [message.role for message in restored_messages if "<skills_instructions>" in str(message.content)] == ["developer"]
    assert [message.content for message in restored_messages if str(message.content).startswith("<skill>\n")] == [bodies[0].content]


def test_empty_skill_catalog_adds_no_developer_fragment(tmp_path: Path, monkeypatch):
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [])
    context = ContextBuilder(skill_executor=SkillExecutor(SkillManager(loader)))
    messages = context._render_prompt_messages(AgentState(user_message="hello"), None)
    assert not any("<skills_instructions>" in str(message.content) for message in messages)
