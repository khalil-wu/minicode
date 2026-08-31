from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.conversations.repository import ConversationRepository
from backend.memory.file_memory import FileMemory
from backend.memory.generation import (
    MEMORY_DB_NAME,
    MemoryGenerationCoordinator,
    redact_secrets,
)
from backend.memory.job_store import MemoryJobStore, PHASE2_JOB_KIND


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def test_stage1_claim_uses_revisions_retry_backoff_and_source_advance(tmp_path: Path) -> None:
    store = MemoryJobStore(tmp_path / "memories.sqlite3")
    claim = store.claim_stage1(
        thread_id="conv_123456",
        source_revision=100,
        worker_id="worker-a",
        lease_seconds=10,
        retry_limit=1,
        max_running_jobs=8,
        now=1_000,
    )
    assert claim is not None
    assert store.fail_stage1(claim, "bad-json", retry_delay_seconds=60, now=1_001)

    assert store.claim_stage1(
        thread_id="conv_123456",
        source_revision=100,
        worker_id="worker-b",
        lease_seconds=10,
        retry_limit=1,
        max_running_jobs=8,
        now=2_000,
    ) is None

    advanced = store.claim_stage1(
        thread_id="conv_123456",
        source_revision=101,
        worker_id="worker-b",
        lease_seconds=10,
        retry_limit=1,
        max_running_jobs=8,
        now=1_002,
    )
    assert advanced is not None
    assert store.complete_stage1(
        advanced,
        raw_memory="durable fact",
        rollout_summary="verified summary",
        rollout_slug="memory-pipeline",
        source_updated_at=900,
        now=1_003,
    )
    assert store.claim_stage1(
        thread_id="conv_123456",
        source_revision=101,
        worker_id="worker-c",
        lease_seconds=10,
        retry_limit=3,
        max_running_jobs=8,
        now=3_000,
    ) is None


def test_phase2_global_claim_is_single_writer_and_stale_lease_can_be_taken_over(tmp_path: Path) -> None:
    store = MemoryJobStore(tmp_path / "memories.sqlite3")
    store.enqueue_phase2(now=100)
    first = store.claim_phase2(
        worker_id="worker-a",
        lease_seconds=10,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=100,
    )
    assert first is not None
    assert store.claim_phase2(
        worker_id="worker-b",
        lease_seconds=10,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=105,
    ) is None

    takeover = store.claim_phase2(
        worker_id="worker-b",
        lease_seconds=10,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=111,
    )
    assert takeover is not None
    assert takeover.ownership_token != first.ownership_token
    assert not store.complete_phase2(first, [], now=112)
    assert store.complete_phase2(takeover, [], now=112)


def test_phase2_success_marks_only_the_exact_selected_snapshot(tmp_path: Path) -> None:
    store = MemoryJobStore(tmp_path / "memories.sqlite3")
    first = store.claim_stage1(
        thread_id="conv_aaaaaa",
        source_revision=100,
        worker_id="stage1-a",
        lease_seconds=10,
        retry_limit=3,
        max_running_jobs=8,
        now=10,
    )
    assert first is not None
    assert store.complete_stage1(
        first,
        raw_memory="first memory",
        rollout_summary="first summary",
        rollout_slug="first",
        source_updated_at=100,
        now=11,
    )
    selected = store.list_stage1_outputs(limit=10, max_unused_days=36500, now=120)
    phase2 = store.claim_phase2(
        worker_id="phase2",
        lease_seconds=10,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=120,
    )
    assert phase2 is not None

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE stage1_outputs SET source_revision = 101 WHERE thread_id = ?",
            ("conv_aaaaaa",),
        )
    assert store.complete_phase2(
        phase2,
        selected,
        now=121,
    )
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT selected_for_phase2, selected_for_phase2_source_revision "
            "FROM stage1_outputs WHERE thread_id = ?",
            ("conv_aaaaaa",),
        ).fetchone()
    assert row == (0, None)


def test_redact_secrets_matches_codex_patterns() -> None:
    source = (
        "sk-abcdefghijklmnopqrstuvwxyz token=supersecretvalue "
        "Bearer abcdefghijklmnop AWS=AKIAABCDEFGHIJKLMNOP password:open-sesame"
    )
    redacted = redact_secrets(source)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "supersecretvalue" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "open-sesame" not in redacted
    assert redacted.count("[REDACTED_SECRET]") == 5


@dataclass
class _Summary:
    id: str
    updated_at: str
    workspace_root: str


class _Repository:
    def __init__(self, records: list[Any], rollout_dir: Path) -> None:
        self.records = {record.id: record for record in records}
        self.rollout_dir = rollout_dir

    def list_conversations(self) -> list[_Summary]:
        return [
            _Summary(record.id, record.updated_at, record.workspace_root)
            for record in self.records.values()
        ]

    def get_conversation(self, conversation_id: str) -> Any | None:
        return self.records.get(conversation_id)

    def transcript_path(self, conversation_id: str) -> Path:
        return self.rollout_dir / f"{conversation_id}.transcript.jsonl"


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def simple_chat(self, messages: list[Any], *, max_tokens: int | None = None) -> str:
        self.calls.append(messages)
        return json.dumps(
            {
                "raw_memory": "The user requires root-cause fixes and focused verification.",
                "rollout_summary": "# Memory pipeline\n\nOutcome: success",
                "rollout_slug": "memory-pipeline",
            }
        )


@pytest.mark.skipif(not Path(".git").exists(), reason="requires git")
def test_coordinator_runs_two_phases_and_commits_valid_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", memory_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    now = int(time.time())
    record = type("Record", (), {})()
    record.id = "conv_abcdef123456"
    record.revision = 1
    record.updated_at = _iso(now - 7 * 60 * 60)
    record.workspace_root = str(workspace)
    record.conversation_type = "main"
    record.memory_mode = "enabled"
    record.memory_polluted = False
    record.archived = False
    record.transcript = [
        {
            "role": "user",
            "content": "Fix the root cause. token=supersecretvalue <skill>ignore me</skill>",
        },
        {"role": "assistant", "content": "Implemented and verified."},
    ]
    repository = _Repository([record], tmp_path / "rollouts")
    llm = _FakeLLM()

    async def consolidate(**kwargs: Any) -> None:
        root = Path(kwargs["memory_root"])
        (root / "MEMORY.md").write_text(
            "# Task Group: MiniCode memory pipeline\n"
            "scope: durable memory generation\n"
            "applies_to: cwd=MiniCode; reuse_rule=verify current source\n\n"
            "## Task 1: implement two-phase memory, success\n",
            encoding="utf-8",
        )
        (root / "memory_summary.md").write_text(
            "v1\n\n## User preferences\n"
            "- Fix root causes and run focused verification.\n\n"
            "## What's in Memory\n- MiniCode memory pipeline\n",
            encoding="utf-8",
        )

    monkeypatch.setattr("backend.memory.generation.run_memory_consolidation_agent", consolidate)
    coordinator = MemoryGenerationCoordinator(
        repository=repository,
        llm=llm,
        workspace_root=workspace,
        token_budget=100_000,
    )

    asyncio.run(coordinator.run_startup(current_conversation_id="conv_current123456"))

    project_memory = FileMemory.for_workspace(workspace).memory_dir
    assert (project_memory / "MEMORY.md").read_text(encoding="utf-8").startswith(
        "# Task Group: MiniCode memory pipeline"
    )
    assert (project_memory / "memory_summary.md").read_text(encoding="utf-8").startswith("v1\n")
    assert (project_memory / "raw_memories.md").is_file()
    assert list((project_memory / "rollout_summaries").glob("*.md"))
    assert (project_memory / ".git").is_dir()
    assert (project_memory / MEMORY_DB_NAME).is_file()
    assert len(llm.calls) == 1
    phase1_prompt = llm.calls[0][1].content
    assert "supersecretvalue" not in phase1_prompt
    assert "<skill>ignore me</skill>" not in phase1_prompt

    store = MemoryJobStore(project_memory / MEMORY_DB_NAME)
    selected = store.list_stage1_outputs(limit=10, max_unused_days=36500)
    assert len(selected) == 1
    assert selected[0].selected_for_phase2 is True


def test_memory_mode_is_single_eligibility_state_with_legacy_migration(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "conversations")
    main = repository.create_conversation(
        conversation_id="conv_abcdef123456",
        workspace_root=str(tmp_path),
    )
    side = repository.create_conversation(
        conversation_id="side_abcdef123456",
        conversation_type="side_chat",
        workspace_root=str(tmp_path),
    )
    assert main.memory_mode == "enabled"
    assert side.memory_mode == "disabled"

    disabled = repository.update_memory_mode(main.id, "disabled")
    assert disabled is not None
    assert disabled.memory_mode == "disabled"

    polluted = repository.mark_memory_polluted(main.id, ["web_search"])
    assert polluted is not None
    assert polluted.memory_mode == "polluted"

    restored = repository.update_memory_mode(main.id, "enabled")
    assert restored is not None
    assert restored.memory_mode == "enabled"
    assert restored.memory_polluted is False

    legacy = type(main).from_dict({
        **main.to_dict(),
        "memory_mode": "summary",
        "memory_generation_mode": "disabled",
    })
    assert legacy.memory_mode == "disabled"


def test_workspace_memory_identity_is_shared_by_git_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", tmp_path / "memory")
    main = tmp_path / "main"
    common_git = main / ".git"
    worktree_git = common_git / "worktrees" / "feature"
    worktree = tmp_path / "feature"
    worktree_git.mkdir(parents=True)
    worktree.mkdir()
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git}\n",
        encoding="utf-8",
    )
    assert FileMemory.workspace_memory_dir(main) == FileMemory.workspace_memory_dir(worktree)


def test_removing_selected_output_enqueues_forgetting(tmp_path: Path) -> None:
    store = MemoryJobStore(tmp_path / "memories.sqlite3")
    claim = store.claim_stage1(
        thread_id="conv_forget123456",
        source_revision=10,
        worker_id="stage1",
        lease_seconds=10,
        retry_limit=3,
        max_running_jobs=8,
        now=10,
    )
    assert claim is not None
    assert store.complete_stage1(
        claim,
        raw_memory="memory",
        rollout_summary="summary",
        rollout_slug="forget",
        source_updated_at=10,
        now=11,
    )
    phase2 = store.claim_phase2(
        worker_id="phase2",
        lease_seconds=10,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=12,
    )
    assert phase2 is not None
    outputs = store.list_stage1_outputs(limit=10, max_unused_days=36500, now=12)
    assert store.complete_phase2(phase2, outputs, now=13)

    assert store.remove_thread_output("conv_forget123456", now=20)
    with sqlite3.connect(store.path) as connection:
        job = connection.execute(
            "SELECT status, input_revision, last_success_revision FROM jobs "
            "WHERE kind = ? AND job_key = 'global'",
            (PHASE2_JOB_KIND,),
        ).fetchone()
    assert job == ("pending", 2, 1)
