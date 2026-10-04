from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from backend.conversations.repository import ConversationRepository
from backend.memory.file_memory import FileMemory
from backend.memory.generation import MemoryGenerationCoordinator
from backend.memory.local_backend import LocalMemoryBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["publish", "disabled", "cancel", "note_changed", "commit_error"])
async def test_phase2_publishes_only_a_committed_eligible_tree(tmp_path, monkeypatch, outcome):
    from backend.memory import generation

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(FileMemory, "workspace_memory_dir", classmethod(lambda _cls, _workspace: memory_root))
    repository = ConversationRepository(tmp_path / "conversations")
    record = repository.create_conversation(workspace_root=str(tmp_path / "workspace"))
    coordinator = MemoryGenerationCoordinator(
        repository=repository, llm=object(), workspace_root=tmp_path / "workspace",
    )
    summary = memory_root / "memory_summary.md"
    summary.write_text("v1\nCommitted old summary", encoding="utf-8")
    (memory_root / "MEMORY.md").write_text("Committed old handbook", encoding="utf-8")
    claim = coordinator.store.claim_stage1(
        thread_id=record.id, source_revision=record.content_revision,
        worker_id="chain-audit", lease_seconds=60, retry_limit=3, max_running_jobs=8,
    )
    assert coordinator.store.complete_stage1(
        claim, raw_memory="Selected input", rollout_summary="Selected rollout",
        rollout_slug=None, source_updated_at=int(time.time()),
    )
    started, proceed = asyncio.Event(), asyncio.Event()
    staged_roots = []

    async def consolidate(*, memory_root, **_kwargs):
        staged_roots.append(memory_root)
        assert memory_root != coordinator.memory_root
        (memory_root / "memory_summary.md").write_text("v1\nNew published summary", encoding="utf-8")
        (memory_root / "MEMORY.md").write_text("New published handbook", encoding="utf-8")
        started.set()
        await proceed.wait()

    monkeypatch.setattr(generation, "run_memory_consolidation_agent", consolidate)
    task = asyncio.create_task(coordinator._run_phase2())
    await asyncio.wait_for(started.wait(), timeout=10)
    assert "Committed old summary" in coordinator.file_memory.get_context()
    assert coordinator.file_memory.read_file("MEMORY.md") == "Committed old handbook"
    if outcome == "disabled":
        repository.update_memory_mode(record.id, "disabled")
    elif outcome == "note_changed":
        with coordinator.file_memory.reset_lock.acquire(timeout=5.0):
            LocalMemoryBackend(memory_root).add_ad_hoc_note(
                filename="2026-10-02T10-00-00-new-note.md", note="User note added while consolidating",
            )
    elif outcome == "commit_error":
        monkeypatch.setattr(coordinator.store, "complete_phase2", lambda *_args, **_kwargs: False)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        proceed.set()
        await task
    if outcome == "publish":
        assert "New published summary" in coordinator.file_memory.get_context()
        assert coordinator.file_memory.read_file("MEMORY.md") == "New published handbook"
        outputs = coordinator.store.list_stage1_outputs(limit=None, max_unused_days=30)
        assert outputs[0].selected_for_phase2 is True
    else:
        assert "Committed old summary" in coordinator.file_memory.get_context()
        assert coordinator.file_memory.read_file("MEMORY.md") == "Committed old handbook"
    if outcome == "note_changed":
        assert (memory_root / "extensions/ad_hoc/notes/2026-10-02T10-00-00-new-note.md").read_text(encoding="utf-8") == "User note added while consolidating"
    assert all(not staged.exists() for staged in staged_roots)
    assert not list(tmp_path.glob(".memory.consolidation-*"))
