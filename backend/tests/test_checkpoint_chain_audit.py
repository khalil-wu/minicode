from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from backend.agent.checkpoint import load_latest_checkpoint, save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.checkpoint.manager import CheckpointManager
from backend.checkpoint.store import CheckpointCorruptError, CheckpointFileSnapshot, CheckpointRecord, CheckpointStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.checkpoint_service import prepare_run_checkpoint_resume
from backend.tools.registry import ToolRegistry


def record(identifier, root, path, content, *, timestamp="2026-10-02T01:00:00+00:00"):
    return CheckpointRecord(
        id=identifier, conversation_id="chain_audit", session_id="old_session",
        tool_call_id=identifier, tool_name="write_file", workspace_root=str(root),
        paths=[path], files=[CheckpointFileSnapshot(path, True, content=content)], created_at=timestamp,
    )


@pytest.mark.parametrize("corruption", ["payload", "json"])
def test_later_corrupt_snapshot_refuses_rewind_before_any_file_mutation(tmp_path, corruption):
    store = CheckpointStore(tmp_path / "history")
    manager = CheckpointManager(store)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("current a")
    (root / "b.txt").write_text("current b")
    first = store.save(record("first", root, "a.txt", "before a"))
    later = store.save(record("later", root, "b.txt", None, timestamp="2026-10-02T02:00:00+00:00"))
    if corruption == "json":
        store._path_for(later.id).write_text("{", encoding="utf-8")
    with pytest.raises(CheckpointCorruptError):
        asyncio.run(manager.rewind(first.id))
    assert (root / "a.txt").read_text() == "current a"
    assert (root / "b.txt").read_text() == "current b"


def test_sparse_rewind_preserves_persistence_order_when_timestamps_match(tmp_path):
    store = CheckpointStore(tmp_path / "history")
    manager = CheckpointManager(store)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("after a")
    (root / "b.txt").write_text("after b")
    first = store.save(record("z_first", root, "a.txt", "before a"))
    later = store.save(record("a_later", root, "b.txt", "before b"))
    os.utime(store._path_for(first.id), ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(store._path_for(later.id), ns=(1_700_000_000_001_000_000,) * 2)
    restored = asyncio.run(manager.rewind(first.id))
    assert (root / "a.txt").read_text() == "before a"
    assert (root / "b.txt").read_text() == "before b"
    assert set(restored.paths) == {"a.txt", "b.txt"}


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity")
def test_sparse_rewind_uses_canonical_workspace_and_file_identity(tmp_path):
    store = CheckpointStore(tmp_path / "history")
    manager = CheckpointManager(store)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("latest")
    (root / "b.txt").write_text("new b")
    first = store.save(record("first", root, "a.txt", "original"))
    store.save(record("second", Path(str(root).swapcase()), "A.TXT", "intermediate", timestamp="2026-10-02T02:00:00+00:00"))
    store.save(record("third", Path(str(root).swapcase()), "b.txt", "old b", timestamp="2026-10-02T03:00:00+00:00"))
    restored = asyncio.run(manager.rewind(first.id))
    assert (root / "a.txt").read_text() == "original"
    assert (root / "b.txt").read_text() == "old b"
    assert len(restored.paths) == 2


@pytest.fixture
def runtime(tmp_path):
    owner = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    yield owner
    owner.close(release_lease=True)


def stopped_checkpoint(runtime, tmp_path):
    from backend.agent.state import AgentState

    source = runtime.start_run(conversation_id="chain_audit", session_id="old_session", role="main")
    runtime.commit_terminal(source.run_id, status="cancelled", terminal_reason="interrupted")
    builder = ContextBuilder(conversation_id="chain_audit", workspace_root=tmp_path)
    state = AgentState(user_message="retain this request", conversation_id="chain_audit", workspace_root=tmp_path)
    asyncio.run(builder.start_turn(state.user_message, state))
    save_checkpoint(
        session_id="old_session", base_dir=runtime.state_root, user_message=state.user_message,
        iterations=1, reply="", messages=[], context_snapshot=builder.export_snapshot(),
        tool_calls=[], active_skills=[], disabled_tools=set(), stopped_reason="interrupted",
        last_mutation_index=0, run_id=source.run_id, conversation_id="chain_audit", resume_payload={"role": "main"},
    )
    return source


def test_resume_resolves_persisted_main_session_and_ignores_child_checkpoint(runtime, tmp_path):
    source = stopped_checkpoint(runtime, tmp_path)
    runtime.start_run(conversation_id="chain_audit", session_id="child_session", parent_run_id=source.run_id, role="subagent:worker")
    resume = prepare_run_checkpoint_resume(session_id="new_session", requested_conversation_id="chain_audit", runtime=runtime)
    assert resume.session_id == "new_session"
    assert resume.checkpoint_session_id == "old_session"
    assert resume.run_id == source.run_id


def test_new_completed_main_run_prevents_resuming_an_older_paused_turn(runtime, tmp_path):
    stopped_checkpoint(runtime, tmp_path)
    latest = runtime.start_run(conversation_id="chain_audit", session_id="new_session", role="main")
    runtime.commit_terminal(latest.run_id, status="completed", terminal_reason="completed")
    assert prepare_run_checkpoint_resume(session_id="third_session", requested_conversation_id="chain_audit", runtime=runtime) is None


def test_real_query_resume_preserves_request_identity_and_clears_source_checkpoint(runtime, tmp_path):
    source = stopped_checkpoint(runtime, tmp_path)

    class Provider(LLMAdapter):
        messages = None

        async def stream_chat(self, messages, tools=None):
            self.messages = messages
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished the retained request.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    async def scenario():
        provider = Provider()
        budget = TokenBudget(total=32768, response_reserve=4096)
        session = AgentSession(
            llm=provider, tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=3), token_budget=budget,
        )
        events = [event async for event in QueryEngine().submit(QuerySubmission(
            user_message="retain this request", session=session,
            runtime=AgentLoopSessionContext(
                workspace_root=tmp_path, session_id="new_session", run_context=RunContext(agent_runtime=runtime),
                permission_context=PermissionContext(mode="bypass"),
                metadata={"conversation_id": "chain_audit", "resume_from_checkpoint": True,
                          "resume_checkpoint_run_id": source.run_id, "resume_checkpoint_session_id": "old_session"},
            ),
        ))]
        assert next(event for event in events if event.type == "done").data["status"] == "completed"
        assert sum(message.is_user_input for message in provider.messages) == 1
        latest = runtime.latest_main_run("chain_audit")
        assert latest.run_id != source.run_id
        assert latest.session_id == "new_session" and latest.role == "main"
        assert load_latest_checkpoint("old_session", runtime.state_root, conversation_id="chain_audit") is None
        assert load_latest_checkpoint("new_session", runtime.state_root, conversation_id="chain_audit") is None
        await session.aclose()

    asyncio.run(scenario())


def test_completed_parent_with_live_descendant_still_blocks_mutation(runtime, tmp_path, monkeypatch):
    from backend.ws.conversation_activity import live_subagent_count

    parent = runtime.start_run(conversation_id="chain_audit", session_id="old_session", role="main")
    runtime.start_subagent(subagent_id="audit_child", parent_run_id=parent.run_id, agent_type="worker", background=True)
    runtime.commit_terminal(parent.run_id, status="completed", terminal_reason="completed")
    monkeypatch.setattr("backend.agent.runtime.default_runtime_if_initialized", lambda: runtime)
    assert live_subagent_count("chain_audit") == 1
    assert live_subagent_count("other_conversation") == 0
