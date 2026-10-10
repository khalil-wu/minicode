from __future__ import annotations

import asyncio
from copy import deepcopy
import subprocess
import sys

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.turn_diff_tracker import TurnDiffTracker
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.edit_file import EditFileTool
from backend.tools.read_file import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.write_file import WriteFileTool


class _NoProvider(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        raise AssertionError("The injected runner owns this offline test")
        yield

    async def simple_chat(self, messages):
        raise AssertionError("This test sends no provider requests")


class _ScriptProvider(LLMAdapter):
    def __init__(self, batches):
        self.batches = iter(batches)

    async def stream_chat(self, messages, tools=None):
        batch = next(self.batches)
        if batch:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=batch)
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Page generated; inspection results recorded.", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        raise AssertionError("This test sends no auxiliary provider requests")


class _BrowserProfileProbe(BaseTool):
    name = "browser_profile_probe"
    permission = PermissionLevel.AUTO
    mutates_external_state = True

    def __init__(self, fail=False):
        self.fail = fail

    def get_schema(self):
        return ToolSchema(name=self.name, description="Offline browser-profile fixture", parameters={"type": "object", "properties": {}, "additionalProperties": False})

    async def execute(self, args, context=None):
        profile = context.workspace_root / "qa" / "chrome-profile"
        profile.mkdir(parents=True)
        (profile / "listdata.json").write_text('{"components":"' + "browser state " * 200_000 + '"}\n', encoding="utf-8")
        # A user can edit both another file and the tool's edited path while a
        # browser inspection is running. Neither write belongs to that tool.
        (context.workspace_root / "user-notes.md").write_text("concurrent user note\n", encoding="utf-8")
        (context.workspace_root / "index.html").write_text("concurrent user page\n", encoding="utf-8")
        return ToolResult(content="browser click timed out" if self.fail else "browser click completed", is_error=self.fail)


class _CommandWriteProbe(BaseTool):
    name = "command_write_probe"
    permission = PermissionLevel.AUTO
    mutates_workspace = True

    def get_schema(self):
        return ToolSchema(name=self.name, description="Offline command-write fixture", parameters={"type": "object", "properties": {}, "additionalProperties": False})

    async def execute(self, args, context=None):
        await asyncio.to_thread(subprocess.run, [sys.executable, "-c", (
            "from pathlib import Path; "
            "Path('backup').mkdir(); "
            "Path('backup/index.html').write_text('command backup\\n', encoding='utf-8'); "
            "Path('command-output.py').write_text('print(42)\\n', encoding='utf-8')"
        )], cwd=context.workspace_root, check=True)
        return ToolResult(content="backup and command output written")


@pytest.fixture
def query_fixture(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime" / "metrics.jsonl",
        swarm_store_dir=tmp_path / "runtime" / "swarm", enable_lease_heartbeat=False)
    sessions = []

    def create(provider, registry):
        store = ArtifactStore(storage_dir=tmp_path / "artifacts")
        owner = AgentSession(llm=provider, tool_registry=registry, artifact_store=store,
            permission_checker=PermissionChecker(PermissionSettings(), workspace),
            agent_settings=AgentSettings(max_iterations=10, max_turn_seconds=30), token_budget=TokenBudget(),
            context_builder=ContextBuilder(llm=provider))
        sessions.append(owner)
        emitted = []

        async def emit(event_type, data):
            emitted.append(AgentEvent(type=event_type, data=deepcopy(data)))

        state = AgentState(user_message="Create a simple page and inspect it", workspace_root=workspace, conversation_id="page-conversation")
        run_context = RunContext(agent_runtime=runtime)
        submission = QuerySubmission(session=owner, state=state, user_message=state.user_message,
            runtime=AgentLoopSessionContext(session_id="page-session", workspace_root=workspace,
                permission_context=PermissionContext(mode="bypass"), run_context=run_context, emit_event=emit))
        return submission, emitted, run_context

    yield workspace, create
    for owner in sessions:
        asyncio.run(owner.aclose())
    runtime.close(release_lease=True)


@pytest.mark.parametrize("editing_tool", ["write_file", "edit_file", "apply_patch"])
@pytest.mark.parametrize("browser_fails", [False, True])
def test_query_diff_keeps_committed_edits_through_browser_command_and_user_writes(query_fixture, editing_tool, browser_fails):
    workspace, create = query_fixture
    before = "<p>old page</p>\n"
    after = "<p>generated page</p>\n"
    (workspace / "index.html").write_text(before, encoding="utf-8")
    args = {
        "write_file": {"file_path": "index.html", "content": after},
        "edit_file": {"file_path": "index.html", "old_string": "old page", "new_string": "generated page"},
        "apply_patch": {"patch": "*** Begin Patch\n*** Update File: index.html\n@@\n-<p>old page</p>\n+<p>generated page</p>\n*** End Patch"},
    }[editing_tool]
    provider = _ScriptProvider([
        [ToolCallEvent(id="read-page", name="read_file", arguments={"file_path": "index.html"})],
        [ToolCallEvent(id="edit-page", name=editing_tool, arguments=args)],
        [ToolCallEvent(id="inspect", name="browser_profile_probe", arguments={}),
         ToolCallEvent(id="command", name="command_write_probe", arguments={})],
        [ToolCallEvent(id="style", name="write_file", arguments={"file_path": "styles.css", "content": "p { color: red; }\n"})],
        [],
    ])
    registry = ToolRegistry()
    store = ArtifactStore(storage_dir=workspace.parent / "read-artifacts")
    for tool in [ReadFileTool(store), WriteFileTool(), EditFileTool(), ApplyPatchTool(), _BrowserProfileProbe(browser_fails), _CommandWriteProbe()]:
        registry.register(tool)
    submission, emitted, run_context = create(provider, registry)

    async def run():
        return [event async for event in QueryEngine().submit(submission)]

    events = asyncio.run(run())
    diffs = [event for event in emitted + events if event.type == "turn.diff.updated"]
    assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
    assert len(diffs) == 2
    assert [event.data["revision"] for event in diffs] == [1, 2]
    final = diffs[-1].data
    assert final["thread_id"] == "page-conversation"
    assert final["diff"] == run_context.turn_diff_tracker.snapshot().unified_diff
    assert "-<p>old page</p>" in final["diff"] and "+<p>generated page</p>" in final["diff"]
    assert "+++ b/styles.css" in final["diff"]
    assert all(path not in final["diff"] for path in ["chrome-profile", "listdata.json", "backup/", "command-output.py", "user-notes.md", "concurrent user page"])
    assert all(event.data.get("source") != "workspace_snapshot" for event in diffs)
    assert (workspace / "index.html").read_text(encoding="utf-8") == "concurrent user page\n"
    result = next(event for event in events if event.type == "tool_result" and event.data["id"] == "inspect")
    assert result.data["status"] == ("failed" if browser_fails else "success")


def test_query_without_editing_receipts_does_not_attribute_browser_or_command_files(query_fixture):
    workspace, create = query_fixture
    registry = ToolRegistry()
    registry.register(_BrowserProfileProbe())
    registry.register(_CommandWriteProbe())
    submission, emitted, _ = create(_ScriptProvider([
        [ToolCallEvent(id="inspect", name="browser_profile_probe", arguments={})],
        [ToolCallEvent(id="command", name="command_write_probe", arguments={})], [],
    ]), registry)

    async def run():
        return [event async for event in QueryEngine().submit(submission)]

    events = asyncio.run(run())
    assert (workspace / "qa" / "chrome-profile" / "listdata.json").exists()
    assert (workspace / "command-output.py").exists()
    assert not any(event.type == "turn.diff.updated" for event in emitted + events)
    assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]


def test_child_committed_diff_projection_keeps_its_own_container_id():
    from backend.services.subagent_service import build_subagent_transcript_messages
    from backend.conversations.public_projection import project_public_transcript

    tracker = TurnDiffTracker()
    tracker.track_change(old_path="parser.py", new_path="parser.py", old_content=None, new_content="def parse(): return []\n")
    events = [
        {"event_id": "start", "event_type": "system", "ts_ms": 1, "payload": {"lifecycle": "turn_started", "run_id": "child-turn", "turn_id": "child-turn"}},
        {"event_id": "model-item", "event_type": "assistant", "ts_ms": 2, "payload": {"content": "Done"}},
        {"event_id": "committed-diff", "event_type": "system", "ts_ms": 3, "payload": {"lifecycle": "turn_diff_updated", "thread_id": "parent-conversation", "turn_id": "child-turn",
            "message_id": "parent-container", "diff": tracker.get_unified_diff(), "revision": 1}},
        {"event_id": "terminal", "event_type": "terminal", "ts_ms": 4, "payload": {"status": "completed", "run_id": "child-turn"}},
    ]
    messages = project_public_transcript(build_subagent_transcript_messages({"events": events}))
    assistant = next(message for message in messages if message["role"] == "assistant")
    payload = assistant["metadata"]["turn_diff"]
    assert payload["message_id"] == assistant["id"] != "parent-container"
    assert payload["turn_id"] == assistant["turn_id"] == "child-turn"
    assert payload["conversation_id"] == payload["thread_id"] == "parent-conversation"
    assert "parser.py" in payload["diff"]
