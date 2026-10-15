"""Slow Phase 2 maintenance must leave native foreground memory reads available."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time
from tempfile import TemporaryDirectory

import pytest

from backend.conversations.repository import ConversationRepository
from backend.memory import generation
from backend.memory.file_memory import FileMemory
from backend.memory.generation import MemoryGenerationCoordinator


@pytest.fixture
def short_memory_state(monkeypatch):
    # Git's Windows object paths otherwise exceed MAX_PATH under the test
    # runner's descriptive per-case directory before reaching the lock probe.
    with TemporaryDirectory(prefix="minicode-memory-") as directory:
        data_root = Path(directory) / "data"
        monkeypatch.setattr("backend.memory.file_memory.DATA_ROOT", data_root)
        monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", data_root / "memory")
        yield


@pytest.mark.parametrize("maintenance", ["git_init", "git_diff", "git_baseline", "old_tree_cleanup"])
def test_foreground_native_memory_read_does_not_wait_for_phase2_maintenance(tmp_path, monkeypatch, short_memory_state, maintenance):
    workspace = tmp_path / "中文 工作区"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(workspace_root=str(workspace))
    coordinator = MemoryGenerationCoordinator(repository=repository, llm=object(), workspace_root=workspace)
    memory_root = coordinator.memory_root
    old_summary, new_summary = "v1\nCommitted old summary\n", "v1\nPublished new summary\n"
    old_handbook, new_handbook = "Committed old handbook\n", "Published new handbook\n"
    (memory_root / "memory_summary.md").write_text(old_summary, encoding="utf-8")
    (memory_root / "MEMORY.md").write_text(old_handbook, encoding="utf-8")
    claim = coordinator.store.claim_stage1(thread_id=conversation.id, source_revision=conversation.content_revision,
        worker_id="foreground-lock-chain", lease_seconds=60, retry_limit=3, max_running_jobs=8)
    assert coordinator.store.complete_stage1(claim, raw_memory="Synthetic selected input", rollout_summary="Synthetic rollout",
        rollout_slug=None, source_updated_at=int(time.time()))
    entered, release = threading.Event(), threading.Event()
    barrier_details = []

    def pause(operation, root):
        barrier_details.append((operation, Path(root), threading.get_ident()))
        entered.set()
        assert release.wait(30), "Test did not release the maintenance worker"

    original_git = coordinator._git

    def git(*args, **kwargs):
        selected = maintenance == "git_init" and args[0] == "init"
        selected = selected or (maintenance == "git_diff" and args[0] == "diff" and "--no-ext-diff" in args)
        if selected and not entered.is_set():
            pause(maintenance, kwargs.get("cwd") or memory_root)
        return original_git(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_git", git)
    original_baseline = coordinator._commit_git_baseline

    def baseline(root):
        if maintenance == "git_baseline":
            pause(maintenance, root)
        return original_baseline(root)

    monkeypatch.setattr(coordinator, "_commit_git_baseline", baseline)
    original_rmtree = generation.shutil.rmtree

    def rmtree(root, *args, **kwargs):
        if maintenance == "old_tree_cleanup" and Path(root).name.endswith(".previous"):
            pause(maintenance, root)
        return original_rmtree(root, *args, **kwargs)

    monkeypatch.setattr(generation.shutil, "rmtree", rmtree)

    async def consolidate(*, memory_root, **kwargs):
        (memory_root / "memory_summary.md").write_text(new_summary, encoding="utf-8")
        (memory_root / "MEMORY.md").write_text(new_handbook, encoding="utf-8")

    monkeypatch.setattr(generation, "run_memory_consolidation_agent", consolidate)

    def foreground_read():
        # A new foreground FileMemory uses its own native FileLock instance,
        # exactly as extension binding and ContextBuilder do for a new turn.
        memory = FileMemory.for_workspace(workspace)
        with memory.reset_lock.acquire(timeout=5.0):
            return memory.read_file("memory_summary.md"), memory.read_file("MEMORY.md"), memory.get_context()

    async def run():
        task = asyncio.create_task(coordinator._run_phase2())
        try:
            assert await asyncio.to_thread(entered.wait, 15), "Phase 2 never reached the selected maintenance boundary"
            summary, handbook, context = await asyncio.to_thread(foreground_read)
            expected_summary = new_summary if maintenance == "old_tree_cleanup" else old_summary
            expected_handbook = new_handbook if maintenance == "old_tree_cleanup" else old_handbook
            assert summary == expected_summary
            assert handbook == expected_handbook
            assert expected_summary.strip() in context
            assert not release.is_set() and not task.done()
            assert barrier_details[0][2] != threading.get_ident()
        finally:
            release.set()
            await asyncio.wait_for(task, 45)
        final_summary, final_handbook, context = await asyncio.to_thread(foreground_read)
        assert final_summary == new_summary and final_handbook == new_handbook
        assert new_summary.strip() in context
        assert coordinator.store.list_stage1_outputs(limit=None, max_unused_days=30)[0].selected_for_phase2

    asyncio.run(run())
