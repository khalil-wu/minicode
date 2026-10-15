"""Skill directories must stay readable across live permission refreshes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.agent.loop import run_agent_loop
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.services.tool_registry_factory import build_tool_registry


class _ReadSkillThenAnswerLLM(LLMAdapter):
    """Read the selected SKILL.md, then answer."""

    def __init__(self, skill_file: Path) -> None:
        self.skill_file = skill_file
        self.attempts = 0

    async def stream_chat(self, messages, tools=None):
        del messages, tools
        self.attempts += 1
        if self.attempts == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCallEvent(
                    id="call_read_skill",
                    name="read_file",
                    arguments={"file_path": str(self.skill_file)},
                )],
                tool_calls_committed=True,
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="read it")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        del messages
        return ""


def test_skill_directory_stays_readable_after_a_live_permission_refresh(tmp_path, monkeypatch) -> None:
    # The turn snapshot granted the Skill directory as a read-only root, but the
    # per-iteration live refresh rebuilt filesystem constraints from the session
    # context, which never carries turn-owned roots. The grant therefore
    # survived only until the first refresh, and reading the advertised
    # SKILL.md failed with "Path is outside workspace" whenever the workspace
    # lived somewhere else.
    root = tmp_path
    workspace = root / "workspace"
    config_home = root / "user-config"
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(config_home))
    skill_root = config_home / "skills" / "browser"
    workspace.mkdir()
    skill_root.mkdir(parents=True)
    skill_file = skill_root / "SKILL.md"
    skill_file.write_text(
        "---\nname: browser\ndescription: Drive a browser.\n---\nOnly authorized pages.",
        encoding="utf-8",
    )
    skill_manager = SkillManager(SkillLoader(workspace))
    skill_manager.discover()
    checker = PermissionChecker(settings=PermissionSettings(), workspace_root=workspace)
    session_context = checker.build_context(mode="confirm", filesystem_constraints={})

    async def run() -> list:
        events = []
        async for event in run_agent_loop(
            user_message="use the browser skill",
            llm=_ReadSkillThenAnswerLLM(skill_file),
            tool_registry=build_tool_registry(
                artifact_store=ArtifactStore(storage_dir=str(root / "artifacts")),
            ),
            artifact_store=ArtifactStore(storage_dir=str(root / "artifacts")),
            permission_checker=checker,
                agent_settings=AgentSettings(max_iterations=3, code_mode_only=False),
            permission_context=session_context,
            skill_manager=skill_manager,
            metadata={"permission_context_provider": lambda: session_context},
        ):
            events.append(event)
        return events

    events = asyncio.run(run())

    results = [
        event
        for event in events
        if event.type == "tool_result" and event.data.get("id") == "call_read_skill"
    ]
    assert len(results) == 1
    assert results[0].data.get("status") == "success", results[0].data.get("summary")
    assert "Only authorized pages." in str(results[0].data.get("summary") or "")
