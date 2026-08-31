from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from backend.conversations.repository import (
    ConversationRepository,
    ConversationWriteConflict,
)


def test_two_repositories_cannot_overwrite_the_same_explicit_id(tmp_path: Path) -> None:
    base_dir = tmp_path / "conversations"
    first_repo = ConversationRepository(base_dir)
    second_repo = ConversationRepository(base_dir)
    barrier = threading.Barrier(2)

    def create(repo: ConversationRepository, title: str):
        barrier.wait(timeout=5)
        return repo.create_conversation(
            conversation_id="conv_shared_explicit",
            title=title,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(create, first_repo, "First writer")
        second_future = pool.submit(create, second_repo, "Second writer")
        created = [first_future.result(timeout=10), second_future.result(timeout=10)]

    assert len({item.id for item in created}) == 2
    assert sum(item.id == "conv_shared_explicit" for item in created) == 1
    restored = {
        item.id: ConversationRepository(base_dir).get_conversation(item.id)
        for item in created
    }
    assert all(record is not None for record in restored.values())
    assert {record.title for record in restored.values() if record is not None} == {
        "First writer",
        "Second writer",
    }


def test_repository_returns_detached_records_and_summaries(tmp_path: Path) -> None:
    repo = ConversationRepository(tmp_path / "conversations")
    created = repo.create_conversation(
        title="Authoritative",
        transcript=[{"id": "message-1", "role": "user", "content": "keep"}],
    )

    detached = repo.get_conversation(created.id)
    assert detached is not None
    detached.title = "Unsaved mutation"
    detached.transcript[0]["content"] = "corrupted in memory"

    listed = repo.list_conversations()
    listed[0].title = "Unsaved summary mutation"

    restored = repo.get_conversation(created.id)
    relisted = repo.list_conversations()
    assert restored is not None
    assert restored.title == "Authoritative"
    assert restored.transcript[0]["content"] == "keep"
    assert relisted[0].title == "Authoritative"


def test_stale_detached_save_conflicts_and_fresh_save_advances_revision(tmp_path: Path) -> None:
    base_dir = tmp_path / "conversations"
    first_repo = ConversationRepository(base_dir)
    second_repo = ConversationRepository(base_dir)
    created = first_repo.create_conversation(title="Initial")
    stale = first_repo.get_conversation(created.id)
    assert stale is not None

    renamed = second_repo.rename_conversation(created.id, "Concurrent rename")
    assert renamed is not None
    stale.title = "Stale overwrite"
    with pytest.raises(ConversationWriteConflict) as conflict:
        first_repo.save_conversation(stale)
    assert conflict.value.expected == created.revision
    assert conflict.value.current == renamed.revision
    assert stale.revision == created.revision

    fresh = first_repo.get_conversation(created.id)
    assert fresh is not None
    fresh.title = "Fresh save"
    saved = first_repo.save_conversation(fresh)
    assert saved.revision == renamed.revision + 1
    assert second_repo.get_conversation(created.id).title == "Fresh save"


def test_inventory_revision_and_instance_are_process_independent_and_monotonic(tmp_path: Path) -> None:
    base_dir = tmp_path / "conversations"
    first_repo = ConversationRepository(base_dir)
    second_repo = ConversationRepository(base_dir)
    created = first_repo.create_conversation(
        title="Inventory",
        transcript=[{"id": "message-1", "role": "user", "content": "history"}],
    )

    instance_id, revision, summaries = first_repo.list_conversations_with_revision()
    second_instance_id, second_revision, _ = second_repo.list_conversations_with_revision()
    assert instance_id == second_instance_id
    assert revision == second_revision
    assert [item.id for item in summaries] == [created.id]

    renamed = second_repo.rename_conversation(created.id, "Renamed")
    assert renamed is not None
    _, rename_revision, _ = first_repo.list_conversations_with_revision()
    assert rename_revision == revision + 1

    archived = first_repo.set_archived(created.id, True)
    assert archived is not None
    _, archive_revision, _ = second_repo.list_conversations_with_revision()
    assert archive_revision == rename_revision + 1

    cleared = second_repo.clear_conversation(created.id)
    assert cleared is not None
    _, clear_revision, _ = first_repo.list_conversations_with_revision()
    assert clear_revision == archive_revision + 1

    assert first_repo.delete_conversation(created.id) is True
    final_instance_id, delete_revision, final_summaries = second_repo.list_conversations_with_revision()
    assert final_instance_id == instance_id
    assert delete_revision == clear_revision + 1
    assert final_summaries == []

    other_instance_id, _, _ = ConversationRepository(
        tmp_path / "other-conversations"
    ).list_conversations_with_revision()
    assert other_instance_id != instance_id


@pytest.mark.parametrize("raw", ["not-an-integer\n", "-1\n", "9007199254740992\n"])
def test_store_revision_corruption_fails_closed(tmp_path: Path, raw: str) -> None:
    base_dir = tmp_path / raw.strip().replace("-", "_")
    repo = ConversationRepository(base_dir)
    repo.create_conversation()
    (base_dir / ".conversation-store.revision").write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="store revision is malformed"):
        repo.list_conversations_with_revision()


def test_store_instance_corruption_fails_closed(tmp_path: Path) -> None:
    base_dir = tmp_path / "conversations"
    repo = ConversationRepository(base_dir)
    repo.create_conversation()
    (base_dir / ".conversation-store.instance").write_text(
        "not-a-store-instance\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="store instance id is malformed"):
        repo.list_conversations_with_revision()


def test_clear_is_one_generation_and_preserves_non_history_configuration(tmp_path: Path) -> None:
    base_dir = tmp_path / "conversations"
    repo = ConversationRepository(base_dir)
    created = repo.create_conversation(
        title="Keep title",
        memory_mode="polluted",
        memory_polluted=True,
        memory_pollution_sources=["web_search"],
        permission_mode="plan",
        permission_deny_rules=["run_command(rm:*)"],
        permission_overrides={"read_file": "allow"},
        summary="remove summary",
        transcript=[{"id": "message-1", "role": "user", "content": "remove"}],
        context_snapshot={"history": [{"role": "user", "content": "remove"}]},
        workspace_root="C:\\workspace",
        git_branch="feature/release",
        worktree_path="C:\\workspace\\.claude\\worktrees\\task",
        git_isolated=True,
    )
    configured = repo.get_conversation(created.id)
    assert configured is not None
    configured.goal = {"id": "goal-1", "text": "Keep goal", "status": "active"}
    configured.compaction_state = "compacted"
    configured.compaction_summary = "remove compaction"
    configured = repo.save_conversation(configured)
    before_revision = configured.revision

    cleared = repo.clear_conversation(
        created.id,
        context_snapshot={"plan_slug": "release-plan"},
    )
    assert cleared is not None
    assert cleared.revision == before_revision + 1
    assert cleared.transcript == []
    assert cleared.message_count == 0
    assert cleared.summary == ""
    assert cleared.memory_mode == "enabled"
    assert cleared.memory_polluted is False
    assert cleared.memory_pollution_sources == []
    assert cleared.compaction_state == "clean"
    assert cleared.compaction_summary == ""
    assert cleared.context_snapshot == {"plan_slug": "release-plan"}
    assert cleared.title == "Keep title"
    assert cleared.goal == {"id": "goal-1", "text": "Keep goal", "status": "active"}
    assert cleared.permission_mode == "plan"
    assert cleared.permission_deny_rules == ["run_command(rm:*)"]
    assert cleared.permission_overrides == {"read_file": "allow"}
    assert cleared.workspace_root == "C:\\workspace"
    assert cleared.git_branch == "feature/release"
    assert cleared.worktree_path == "C:\\workspace\\.claude\\worktrees\\task"
    assert cleared.git_isolated is True

    manifest = json.loads(
        (base_dir / f"{created.id}.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["current_generation"] == before_revision + 1
    assert manifest["previous_generation"] == before_revision
    assert len(list(base_dir.glob(f"{created.id}.g*"))) == 6


def test_failed_clear_keeps_authoritative_record_and_restores_detached_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "conversations"
    repo = ConversationRepository(base_dir)
    created = repo.create_conversation(
        title="Before failure",
        summary="keep",
        transcript=[{"id": "message-1", "role": "user", "content": "keep"}],
        context_snapshot={"history": [{"role": "user", "content": "keep"}]},
    )
    detached = repo.get_conversation(created.id)
    assert detached is not None
    failing_generation = created.revision + 1
    original_write = repo._safe_write_text

    def fail_snapshot(path: Path, text: str, encoding: str = "utf-8") -> None:
        if path.name == f"{created.id}.g{failing_generation}.snapshot.json":
            raise OSError("simulated clear failure")
        original_write(path, text, encoding=encoding)

    monkeypatch.setattr(repo, "_safe_write_text", fail_snapshot)
    with pytest.raises(OSError, match="simulated clear failure"):
        repo.clear_conversation(created.id)

    restored = ConversationRepository(base_dir).get_conversation(created.id)
    assert restored is not None
    assert restored.revision == created.revision
    assert restored.summary == "keep"
    assert restored.transcript[0]["content"] == "keep"
    assert restored.context_snapshot["history"][0]["content"] == "keep"

    detached.title = "Detached retry"
    with pytest.raises(OSError, match="simulated clear failure"):
        repo.save_conversation(detached)
    assert detached.revision == created.revision


def test_delete_tombstone_is_atomic_and_prevents_lifecycle_id_reuse(tmp_path: Path) -> None:
    base_dir = tmp_path / "conversations"
    repo = ConversationRepository(base_dir)
    created = repo.create_conversation(
        conversation_id="conv_delete_atomic",
        transcript=[{"id": "message-1", "role": "user", "content": "remove"}],
    )
    stale = repo.get_conversation(created.id)
    assert stale is not None

    assert repo.delete_conversation(created.id) is True
    manifest_path = base_dir / f"{created.id}.manifest.json"
    tombstone = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert tombstone["deleted"] is True
    assert tombstone["deletion_generation"] == created.revision + 1
    assert list(base_dir.glob(f"{created.id}.g*")) == []
    assert repo.get_conversation(created.id) is None
    assert all(item.id != created.id for item in repo.list_conversations())

    stale.title = "must not resurrect"
    with pytest.raises(ConversationWriteConflict) as conflict:
        repo.save_conversation(stale)
    assert conflict.value.current == tombstone["deletion_generation"]

    recreated = repo.create_conversation(
        conversation_id=created.id,
        title="New lifecycle",
    )
    assert recreated.id != created.id
    assert manifest_path.exists()


def test_delete_manifest_failure_keeps_the_old_record_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "conversations"
    repo = ConversationRepository(base_dir)
    created = repo.create_conversation(title="Keep on failure")
    manifest_path = base_dir / f"{created.id}.manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    original_write = repo._safe_write_text

    def fail_tombstone(path: Path, text: str, encoding: str = "utf-8") -> None:
        if path == manifest_path and '"deleted": true' in text:
            raise OSError("simulated tombstone failure")
        original_write(path, text, encoding=encoding)

    monkeypatch.setattr(repo, "_safe_write_text", fail_tombstone)
    with pytest.raises(OSError, match="simulated tombstone failure"):
        repo.delete_conversation(created.id)

    assert manifest_path.read_text(encoding="utf-8") == original_manifest
    restored = ConversationRepository(base_dir).get_conversation(created.id)
    assert restored is not None
    assert restored.title == "Keep on failure"


def test_delete_cleanup_failure_cannot_resurrect_tombstoned_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "conversations"
    repo = ConversationRepository(base_dir)
    created = repo.create_conversation(
        transcript=[{"id": "message-1", "role": "user", "content": "remove"}],
    )
    retained_generation = next(base_dir.glob(f"{created.id}.g*.meta.json"))
    original_unlink = Path.unlink

    def fail_one_unlink(path: Path, *args, **kwargs):
        if path == retained_generation:
            raise PermissionError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one_unlink)
    assert repo.delete_conversation(created.id) is True
    assert retained_generation.exists()
    assert ConversationRepository(base_dir).get_conversation(created.id) is None
    tombstone = json.loads(
        (base_dir / f"{created.id}.manifest.json").read_text(encoding="utf-8")
    )
    assert tombstone["deleted"] is True
