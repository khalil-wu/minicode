"""Skill directories must stay readable across live permission refreshes."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from backend.agent.loop import run_agent_loop
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.skills.loader import SkillFull, SkillMeta
from backend.skills.manager import SkillManager
from backend.services.tool_registry_factory import build_tool_registry


class _Loader:
    def __init__(self, skills: list[SkillFull]) -> None:
        self.skills = list(skills)

    def discover(self):
        return [skill.meta for skill in self.skills]

    def list_metas(self):
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
        return next(
            (skill.meta for skill in self.skills if skill.meta.source_path.resolve() == target),
            None,
        )

    def load_full(self, name: str, path=None):
        meta = self.get_meta_by_path(path) if path else self.get_unambiguous_meta(name)
        return next((skill for skill in self.skills if skill.meta is meta), None)

    def get_all_layer1(self):
        return "\n".join(skill.meta.to_layer1_summary() for skill in self.skills)

    def get_meta(self, name: str):
        return self.get_unambiguous_meta(name)


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
                tool_calls_final=True,
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="read it")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        del messages
        return ""


def test_skill_directory_stays_readable_after_a_live_permission_refresh() -> None:
    # The turn snapshot granted the Skill directory as a read-only root, but the
    # per-iteration live refresh rebuilt filesystem constraints from the session
    # context, which never carries turn-owned roots. The grant therefore
    # survived only until the first refresh, and reading the advertised
    # SKILL.md failed with "Path is outside workspace" whenever the workspace
    # lived somewhere else.
    root = Path(tempfile.mkdtemp())
    workspace = root / "workspace"
    skill_root = root / "user-skills" / "browser"
    workspace.mkdir()
    skill_root.mkdir(parents=True)
    skill_file = skill_root / "SKILL.md"
    skill_file.write_text(
        "---\nname: browser\ndescription: Drive a browser.\n---\nOnly authorized pages.",
        encoding="utf-8",
    )
    skill = SkillFull(
        SkillMeta(name="browser", description="Drive a browser.", source_path=skill_file),
        "Only authorized pages.",
        skill_file.read_text(encoding="utf-8"),
    )
    skill_manager = SkillManager(_Loader([skill]))
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
            agent_settings=AgentSettings(max_iterations=3),
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
