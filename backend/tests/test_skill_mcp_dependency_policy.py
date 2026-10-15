from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.agent.skill_activation import activate_turn_skills
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.sandbox.policy import NetworkSandboxPolicy, ResolvedSandboxPolicy, SandboxEnforcement
from backend.services import mcp_service
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tools.registry import ToolRegistry


def _skill_manager(tmp_path: Path, monkeypatch) -> SkillManager:
    source = tmp_path / "skills" / "read-docs" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: read-docs\ndescription: Read documentation.\n---\nUse the docs server.\n", encoding="utf-8")
    metadata = source.parent / "agents" / "openai.yaml"
    metadata.parent.mkdir()
    metadata.write_text(
        "dependencies:\n  tools:\n    - type: mcp\n      value: docs\n"
        "      transport: streamable_http\n      url: https://docs.example.test/mcp\n",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", tmp_path / "skills")])
    return SkillManager(loader)


class _McpManager:
    def __init__(self):
        self.allowed = True
        self.connected = False
        self.admitted = []
        self.registry_version = 0

    def get_server_instructions(self):
        return {}

    def get_client(self, _name):
        return object() if self.connected else None

    def get_server_config(self, _name):
        return None

    def allows_user_server_install(self, config):
        self.admitted.append(config.name)
        return self.allowed


@pytest.mark.parametrize(
    "approval,mode,enforcement,full_write,handlers,expect_prompt,expect_install",
    [
        ("never", "confirm", SandboxEnforcement.DISABLED, False, True, False, True),
        ("never", "confirm", SandboxEnforcement.EXTERNAL, False, True, False, True),
        ("never", "confirm", SandboxEnforcement.MANAGED, True, True, False, True),
        ("never", "bypass", SandboxEnforcement.MANAGED, False, True, False, False),
        ("never", "confirm", SandboxEnforcement.DISABLED, False, False, False, True),
        ("on-request", "auto", SandboxEnforcement.DISABLED, False, True, True, True),
        ("on-request", "bypass", SandboxEnforcement.MANAGED, True, True, True, True),
    ],
)
def test_skill_dependency_install_uses_actual_approval_and_sandbox_profile(
    tmp_path, monkeypatch, approval, mode, enforcement, full_write, handlers, expect_prompt, expect_install,
):
    skills = _skill_manager(tmp_path, monkeypatch)
    manager = _McpManager()
    state = AgentState(user_message="$read-docs", workspace_root=tmp_path)
    published, installed, prompted = [], [], set()

    async def publish(event):
        published.append(event)

    async def answer(request_id):
        assert published[0].data["tool_call_id"] == request_id
        return {"answers": {"skill_mcp_dependency_install": {"answers": ["Install"]}}}

    async def install(actual_manager, candidates):
        assert actual_manager is manager
        installed.extend(candidates)
        manager.connected = True
        return ["docs"]

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", install)

    async def run():
        return [event async for event in activate_turn_skills(
            skills, state.user_message, state, manager,
            answer if handlers else None, publish if handlers else None,
            permission_context=PermissionContext(mode=mode, approval_policy=approval),
            permission_profile=ResolvedSandboxPolicy(enforcement, NetworkSandboxPolicy.RESTRICTED, full_disk_write=full_write),
            prompted_dependencies=prompted,
        )]

    events = asyncio.run(run())
    assert bool(published) is expect_prompt
    assert bool(installed) is expect_install
    assert manager.admitted == ["docs"]
    assert prompted == ({"mcp__streamable_http__https://docs.example.test/mcp"} if expect_prompt else set())
    assert state.active_skills == ["read-docs"]
    if expect_install:
        assert installed == [{"name": "docs", "transport": "http", "url": "https://docs.example.test/mcp", "auto_start": True}]
        assert events == []
    else:
        assert [event.type for event in events] == ["system_notice"]
        assert "MCP dependency unavailable: docs" in state.prompt_context["skill_injections"][0]["content"]


@pytest.mark.parametrize("approval", ["never", "on-request"])
def test_managed_denied_dependency_is_neither_prompted_nor_installed(tmp_path, monkeypatch, approval):
    skills = _skill_manager(tmp_path, monkeypatch)
    manager = _McpManager()
    manager.allowed = False
    state = AgentState(user_message="$read-docs", workspace_root=tmp_path)

    async def unexpected(*_args):
        raise AssertionError("A managed-denied candidate must not prompt or install")

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", unexpected)

    async def run():
        return [event async for event in activate_turn_skills(
            skills, state.user_message, state, manager, unexpected, unexpected,
            permission_context=PermissionContext(approval_policy=approval),
            permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.DISABLED, NetworkSandboxPolicy.ENABLED),
        )]

    events = asyncio.run(run())
    assert manager.admitted == ["docs"]
    assert len(events) == 1 and "not connected: docs" in events[0].data["content"]


@pytest.mark.parametrize("answers", [{}, {"skill_mcp_dependency_install": {"answers": ["Continue anyway"]}}])
def test_skill_dependency_skip_keeps_missing_dependency_visible(tmp_path, monkeypatch, answers):
    skills = _skill_manager(tmp_path, monkeypatch)
    manager = _McpManager()
    state = AgentState(user_message="$read-docs", workspace_root=tmp_path)
    published = []

    async def publish(event):
        published.append(event)

    async def answer(_request_id):
        return {"answers": answers}

    async def unexpected(*_args):
        raise AssertionError("Skip must not install a dependency")

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", unexpected)

    async def run():
        return [event async for event in activate_turn_skills(
            skills, state.user_message, state, manager, answer, publish,
            permission_context=PermissionContext(),
            permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.DISABLED, NetworkSandboxPolicy.ENABLED),
        )]

    events = asyncio.run(run())
    assert len(published) == 1
    assert len(events) == 1 and "not connected: docs" in events[0].data["content"]
    assert "MCP dependency unavailable: docs" in state.prompt_context["skill_injections"][0]["content"]


@pytest.mark.parametrize("policy_changes_while_waiting", [False, True])
def test_skill_dependency_real_config_install_and_post_answer_policy_check(
    tmp_path, monkeypatch, policy_changes_while_waiting,
):
    skills = _skill_manager(tmp_path, monkeypatch)
    config_path = tmp_path / "config" / ".mcp.json"
    config_path.parent.mkdir()
    initial = {"servers": {"existing": {"transport": "stdio", "command": "python", "auto_start": False}}}
    config_path.write_text(json.dumps(initial), encoding="utf-8")
    original_content = config_path.read_bytes()
    real_read = mcp_service.config_file_mod.read_mcp_config
    monkeypatch.setattr(mcp_service.config_file_mod, "read_mcp_config", lambda: real_read(config_path))
    monkeypatch.setattr(mcp_service, "MCP_CONFIG_FILE", config_path)
    hooks = []

    async def config_hook(**kwargs):
        hooks.append(kwargs)

    real_install = mcp_service.install_skill_mcp_servers

    async def install(manager, candidates):
        return await real_install(manager, candidates, config_change_hook=config_hook)

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", install)

    class FileManager(_McpManager):
        def __init__(self):
            super().__init__()
            self.reloaded = []

        async def reload_config(self):
            self.reloaded.append(json.loads(real_read(config_path)["content"]))

    manager = FileManager()
    state = AgentState(user_message="$read-docs", workspace_root=tmp_path)
    published = []

    async def publish(event):
        published.append(event)

    async def answer(_request_id):
        assert published[0].type == "request_user_input"
        manager.allowed = not policy_changes_while_waiting
        return {"answers": {"skill_mcp_dependency_install": {"answers": ["Install"]}}}

    async def run():
        return [event async for event in activate_turn_skills(
            skills, state.user_message, state, manager,
            answer if policy_changes_while_waiting else None, publish if policy_changes_while_waiting else None,
            permission_context=PermissionContext(approval_policy="on-request" if policy_changes_while_waiting else "never"),
            permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.DISABLED, NetworkSandboxPolicy.ENABLED),
        )]

    events = asyncio.run(run())
    assert manager.admitted == ["docs", "docs"]
    assert "MCP dependency unavailable: docs" in state.prompt_context["skill_injections"][0]["content"]
    if policy_changes_while_waiting:
        assert config_path.read_bytes() == original_content
        assert manager.reloaded == hooks == []
        assert len(events) == 2 and "blocked by managed policy" in events[0].data["content"]
        assert list(config_path.parent.glob("*.bak")) == []
    else:
        saved = json.loads(real_read(config_path)["content"])
        assert set(saved["servers"]) == {"existing", "docs"}
        assert saved["servers"]["existing"]["command"] == "python"
        assert saved["servers"]["docs"]["url"] == "https://docs.example.test/mcp"
        assert manager.reloaded == [saved]
        assert hooks == [{"source": "mcp", "file_path": str(config_path)}]
        assert len(list(config_path.parent.glob("*.bak"))) == 1
        assert published == []
        assert len(events) == 1 and "not connected: docs" in events[0].data["content"]


def test_agent_loop_compiles_turn_permissions_before_automatic_skill_install(tmp_path, monkeypatch):
    skills = _skill_manager(tmp_path, monkeypatch)
    manager = _McpManager()
    installed, messages_seen = [], []

    async def install(actual_manager, candidates):
        assert actual_manager is manager
        installed.extend(candidates)
        manager.connected = True
        return ["docs"]

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", install)

    class CapturingLLM(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            assert installed
            messages_seen.extend(messages)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Docs are available.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "ok"

    async def run():
        return [event async for event in run_agent_loop(
            user_message="$read-docs",
            llm=CapturingLLM(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
            agent_settings=AgentSettings(max_iterations=1),
            skill_manager=skills,
            permission_context=PermissionContext(mode="bypass", approval_policy="never"),
            session_context=AgentLoopSessionContext(
                workspace_root=tmp_path, session_id="skill-policy-session",
                metadata={"conversation_id": "skill-policy-conversation", "assistant_message_id": "skill-policy-answer"},
                run_context=RunContext(mcp_manager=manager),
            ),
        )]

    events = asyncio.run(run())
    assert len(installed) == 1 and installed[0]["name"] == "docs"
    assert not any(event.type == "request_user_input" for event in events)
    assert any("Use the docs server." in str(message.content) for message in messages_seen)
    assert all("MCP dependency unavailable" not in str(message.content) for message in messages_seen)
