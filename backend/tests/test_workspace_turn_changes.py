from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.workspace_turn_changes import WorkspaceTurnChanges
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


def test_workspace_diff_uses_current_dirty_content_and_includes_commands_and_untracked_files(tmp_path):
    (tmp_path / "already-dirty.py").write_text("amount = 9\n# prior user edit\n", encoding="utf-8")
    (tmp_path / "removed.txt").write_text("remove this\n", encoding="utf-8")
    tracker = WorkspaceTurnChanges.capture(tmp_path, application_roots=())
    subprocess.run([sys.executable, "-c", "from pathlib import Path; Path('cli.py').write_text('print(42)\\n', encoding='utf-8')"], cwd=tmp_path, check=True)
    (tmp_path / "already-dirty.py").write_text("amount = 12\n# prior user edit\n", encoding="utf-8")
    (tmp_path / "removed.txt").unlink()
    (tmp_path / "parser.py").write_text("def parse(value): return int(value)\n", encoding="utf-8")
    diff = tracker.unified_diff()
    assert "+++ b/cli.py" in diff and "+print(42)" in diff
    assert "+++ b/parser.py" in diff
    assert "-amount = 9" in diff and "+amount = 12" in diff
    assert "--- a/removed.txt\n+++ /dev/null" in diff
    assert "-# prior user edit" not in diff


def test_workspace_diff_reports_actual_binary_changes_and_excludes_index_state_and_ignored_outputs(tmp_path):
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"original index")
    (tmp_path / "picture.png").write_bytes(b"\x89PNG\0old")
    tracker = WorkspaceTurnChanges.capture(tmp_path, application_roots=())
    (tmp_path / ".git" / "index").write_bytes(b"new index")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "report.txt").write_text("generated", encoding="utf-8")
    (tmp_path / "picture.png").write_bytes(b"\x89PNG\0new")
    diff = tracker.unified_diff()
    assert "Binary files a/picture.png and b/picture.png differ" in diff
    assert "generated" not in diff and ".git/index" not in diff


def test_reverted_edits_and_add_then_remove_are_an_exact_empty_diff(tmp_path):
    (tmp_path / "module.py").write_text("original\n", encoding="utf-8")
    tracker = WorkspaceTurnChanges.capture(tmp_path, application_roots=())
    (tmp_path / "module.py").write_text("changed\n", encoding="utf-8")
    (tmp_path / "scratch.py").write_text("temporary\n", encoding="utf-8")
    (tmp_path / "module.py").write_text("original\n", encoding="utf-8")
    (tmp_path / "scratch.py").unlink()
    assert tracker.unified_diff() == ""


def test_parallel_child_scopes_exclude_their_peer_while_parent_includes_both_and_command_files(tmp_path):
    parent = WorkspaceTurnChanges.capture(tmp_path, application_roots=())
    parser = WorkspaceTurnChanges.capture(tmp_path, application_roots=(), write_scope=("ledger_check/parser.py", "ledger_check/summary.py"))
    tests = WorkspaceTurnChanges.capture(tmp_path, application_roots=(), write_scope=("tests/", "README.md"))
    (tmp_path / "ledger_check").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "ledger_check" / "parser.py").write_text("def parse(): return []\n", encoding="utf-8")
    (tmp_path / "ledger_check" / "summary.py").write_text("def summary(): return {}\n", encoding="utf-8")
    (tmp_path / "tests" / "test_parser.py").write_text("def test_parse(): pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("CSV parser\n", encoding="utf-8")
    (tmp_path / "cli.py").write_text("print('ready')\n", encoding="utf-8")
    parser_diff, tests_diff, parent_diff = parser.unified_diff(), tests.unified_diff(), parent.unified_diff()
    assert "ledger_check/parser.py" in parser_diff and "ledger_check/summary.py" in parser_diff
    assert "tests/test_parser.py" not in parser_diff and "README.md" not in parser_diff and "cli.py" not in parser_diff
    assert "tests/test_parser.py" in tests_diff and "README.md" in tests_diff
    assert "ledger_check/parser.py" not in tests_diff and "cli.py" not in tests_diff
    assert all(path in parent_diff for path in ("ledger_check/parser.py", "ledger_check/summary.py", "tests/test_parser.py", "README.md", "cli.py"))


def test_scoped_child_journal_diff_uses_its_real_container_id_after_projection(tmp_path):
    from backend.services.subagent_service import build_subagent_transcript_messages
    from backend.conversations.public_projection import project_public_transcript
    tracker = WorkspaceTurnChanges.capture(tmp_path, application_roots=(), write_scope=("parser.py",))
    (tmp_path / "parser.py").write_text("def parse(): return []\n", encoding="utf-8")
    (tmp_path / "peer_test.py").write_text("def test_parse(): pass\n", encoding="utf-8")
    events = [
        {"event_id": "start", "event_type": "system", "ts_ms": 1, "payload": {"lifecycle": "turn_started", "run_id": "child-turn", "turn_id": "child-turn"}},
        {"event_id": "model-item", "event_type": "assistant", "ts_ms": 2, "payload": {"content": "Done"}},
        {"event_id": "scope-diff", "event_type": "system", "ts_ms": 3, "payload": {"lifecycle": "turn_diff_updated", "thread_id": "parent-conversation", "turn_id": "child-turn",
            "message_id": "parent-container", "diff": tracker.unified_diff(), "revision": 2, "source": "workspace_snapshot"}},
        {"event_id": "terminal", "event_type": "terminal", "ts_ms": 4, "payload": {"status": "completed", "run_id": "child-turn"}},
    ]
    messages = project_public_transcript(build_subagent_transcript_messages({"events": events}))
    assistant = next(message for message in messages if message["role"] == "assistant")
    payload = assistant["metadata"]["turn_diff"]
    assert payload["message_id"] == assistant["id"] != "parent-container"
    assert payload["turn_id"] == assistant["turn_id"] == "child-turn"
    assert payload["conversation_id"] == payload["thread_id"] == "parent-conversation"
    assert "parser.py" in payload["diff"] and "peer_test.py" not in payload["diff"]


class _NoProvider(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        raise AssertionError("The injected runner owns this offline test")
        yield

    async def simple_chat(self, messages):
        raise AssertionError("This test sends no provider requests")


@pytest.mark.asyncio
async def test_parent_terminal_diff_contains_completed_children_and_real_command_write(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime = AgentRuntime(metrics_file=runtime_root / "metrics.jsonl", swarm_store_dir=runtime_root / "swarm", enable_lease_heartbeat=False)
    adapter = _NoProvider()
    state = AgentState(user_message="Build the CSV parser", workspace_root=workspace, conversation_id="parent-conversation")

    async def child(name, content):
        await asyncio.to_thread((workspace / name).write_text, content, encoding="utf-8")

    async def runner(**kwargs):
        await asyncio.gather(child("parser.py", "def parse(row): return row.split(',')\n"), child("test_parser.py", "def test_parse(): assert True\n"))
        await asyncio.to_thread(subprocess.run, [sys.executable, "-c", "from pathlib import Path; Path('cli.py').write_text('print(42)\\n', encoding='utf-8')"], cwd=workspace, check=True)
        state.reply = "Implemented and verified the CSV parser."
        yield AgentEvent.agent_message_completed(state.reply, item_id="final", source="model_final")
        yield AgentEvent.done(status="completed")

    session = AgentSession(llm=adapter, tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        permission_checker=PermissionChecker(PermissionSettings(), workspace), agent_settings=AgentSettings(max_turn_seconds=10),
        token_budget=TokenBudget(), context_builder=ContextBuilder(llm=adapter))
    try:
        events = [event async for event in QueryEngine(runner=runner).submit(QuerySubmission(session=session, state=state,
            user_message=state.user_message, runtime=AgentLoopSessionContext(workspace_root=workspace, run_context=RunContext(agent_runtime=runtime))))]
        change = next(event for event in events if event.type == "turn.diff.updated")
        assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
        assert change.data["source"] == "workspace_snapshot"
        assert change.data["thread_id"] == "parent-conversation"
        assert all(f"+++ b/{name}" in change.data["diff"] for name in ("parser.py", "test_parser.py", "cli.py"))
        assert events.index(change) < next(index for index, event in enumerate(events) if event.type == "done")
    finally:
        await session.aclose()
        runtime.close(release_lease=True)
