import asyncio
import tempfile
from pathlib import Path

from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.file_tools import WriteFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.base import PermissionLevel


class _EmptyWriteThenValidWriteLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(id="write-empty", name="write_file", arguments={})
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        if self.calls == 2:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="write-valid",
                        name="write_file",
                        arguments={
                            "file_path": "angry-bird.html",
                            "content": "<!doctype html><title>Angry Bird</title>",
                        },
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="已写入 angry-bird.html。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "已写入 angry-bird.html。"


def test_empty_write_file_can_recover_and_write_file():
    async def _go():
        td = tempfile.mkdtemp()
        root = Path(td)
        registry = ToolRegistry()
        class _UnattendedWriteFileTool(WriteFileTool):
            def check_permission(self, args=None, context=None):
                return PermissionLevel.AUTO

        registry.register(_UnattendedWriteFileTool())
        events = []
        async for ev in run_agent_loop(
            user_message="写一个愤怒的小鸟小游戏 html 即可",
            llm=_EmptyWriteThenValidWriteLLM(),
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=td),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=root,
            ),
            agent_settings=AgentSettings(max_iterations=5),
            permission_context=PermissionContext(mode="auto", approval_policy="on-request"),
            session_context=AgentLoopSessionContext(workspace_root=root),
        ):
            events.append(ev)
        return root, events

    root, events = asyncio.run(_go())
    types = [getattr(ev, "type", "") for ev in events]
    completed = [ev for ev in events if getattr(ev, "type", "") == "item.completed"]
    final_text = completed[-1].data.get("item", {}).get("text", "")

    assert (root / "angry-bird.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "error" not in types
    assert "已写入 angry-bird.html" in final_text
    assert any(
        getattr(ev, "type", "") == "tool_result"
        and ev.data.get("id") == "write-empty"
        and ev.data.get("status") == "blocked"
        for ev in events
    )
