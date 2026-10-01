"""Isolated regressions for the five reproduced persistence commit failures."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import threading
import uuid

import pytest

from backend import atomic_io
from backend.artifact import store as artifact_module
from backend.conversations.repository import (
    ConversationRepository,
    ConversationStorageCorruptError,
    ConversationWriteConflict,
)
from backend.runtime_env import sanitized_git_env
from backend.tasks import scheduler as scheduler_module
from backend.workspace.worktree import WorktreeManager
from backend.workspace.worktree_snapshots import WorktreeSnapshotStore


@pytest.fixture(autouse=True)
def isolated_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_io, "_MUTATION_LOCK_ROOT", tmp_path / "mutation-locks")


def receipt(case, **values):
    print("PERSISTENCE_RECEIPT " + json.dumps({"case": case, **values}, sort_keys=True))


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=sanitized_git_env(), check=True,
        capture_output=True, timeout=30,
    ).stdout


@pytest.mark.parametrize("ignored_only", [True, False])
def test_worktree_backup_matches_removed_files_and_preserves_index(tmp_path, ignored_only):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "persistence@example.invalid")
    git(repo, "config", "user.name", "Persistence verification")
    git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("private.local\nignored-tree/\n", encoding="utf-8")
    (repo / "tracked.txt").write_bytes(b"base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-b", "verify", str(worktree))
    ignored = b"private\x00\xff\r\n"
    (worktree / "private.local").write_bytes(ignored)
    (worktree / "ignored-tree").mkdir()
    (worktree / "ignored-tree" / "nested.bin").write_bytes(b"nested\x00\xfe")
    if not ignored_only:
        (worktree / "tracked.txt").write_bytes(b"staged\n")
        git(worktree, "add", "tracked.txt")
        (worktree / "tracked.txt").write_bytes(b"unstaged\n")
        (worktree / "new.txt").write_bytes(b"untracked\n")
    manager = WorktreeManager(repo, snapshot_store=WorktreeSnapshotStore(tmp_path / "snapshots"))
    assert manager.has_local_changes(worktree)
    denied = manager.safe_remove_worktree(worktree)
    assert not denied.removed and denied.needs_force
    index = Path(git(worktree, "rev-parse", "--git-path", "index").decode().strip())
    if not index.is_absolute():
        index = worktree / index
    before_index = index.read_bytes()
    snapshot = manager.snapshot_worktree(worktree)
    assert snapshot is not None
    assert index.read_bytes() == before_index
    removed = manager.safe_remove_worktree(worktree, force=True)
    assert removed.removed and removed.snapshot is not None
    restored = manager.restore_removed_worktree(
        worktree, branch="verify", expected_head=removed.head,
        snapshot_id=removed.snapshot.id,
    )
    assert restored.restored
    assert (worktree / "private.local").read_bytes() == ignored
    assert (worktree / "ignored-tree" / "nested.bin").read_bytes() == b"nested\x00\xfe"
    if not ignored_only:
        assert (worktree / "tracked.txt").read_bytes() == b"unstaged\n"
        assert (worktree / "new.txt").read_bytes() == b"untracked\n"
    receipt("WP-C01", ignored_only=ignored_only, restored=True, real_index_unchanged=True)


def projected_conversation(root, *, predecessor_projection=False):
    repo = ConversationRepository(root)
    record = repo.create_conversation(title="before")
    if predecessor_projection:
        record = repo.commit_turn_projection(
            record.id, assistant_message={"id": "prior", "role": "assistant", "content": "prior"},
            context_delta={}, partial=True,
        )
    else:
        record = repo.rename_conversation(record.id, "predecessor")
    previous_revision = record.revision
    record = repo.append_transcript_message(record.id, {"id": "user", "role": "user", "content": "hi"})
    record = repo.commit_turn_projection(
        record.id, assistant_message={"id": "partial", "role": "assistant", "content": "streamed"},
        context_delta={}, partial=True,
    )
    manifest = repo._read_manifest(record.id)
    log = repo._partial_projection_path(record.id, manifest["projection_log"]["generation"])
    log.write_bytes(b"{broken")
    return record, previous_revision, log


@pytest.mark.parametrize("predecessor_projection", [False, True])
def test_projection_recovery_and_commit_agree(tmp_path, predecessor_projection):
    root = tmp_path / "conversations"
    damaged, expected, log = projected_conversation(root, predecessor_projection=predecessor_projection)
    fresh = ConversationRepository(root)
    recovered = fresh.get_conversation(damaged.id)
    assert recovered.revision == expected
    evidence = log.read_bytes()
    renamed = fresh.rename_conversation(damaged.id, "resumed")
    assert renamed.title == "resumed" and renamed.revision > damaged.revision
    assert log.read_bytes() == evidence
    reread = ConversationRepository(root).get_conversation(damaged.id)
    assert reread.revision == renamed.revision
    assert reread.transcript == recovered.transcript
    with pytest.raises(ConversationWriteConflict):
        fresh.save_conversation(recovered)
    receipt("WP-C02", predecessor_revision=expected, published_revision=renamed.revision,
            corrupt_evidence_retained=True, real_stale_writer_rejected=True)


def test_no_readable_projection_predecessor_does_not_publish(tmp_path):
    root = tmp_path / "conversations"
    damaged, _, _ = projected_conversation(root, predecessor_projection=True)
    fresh = ConversationRepository(root)
    manifest = fresh._read_manifest(damaged.id)
    previous = fresh._partial_projection_path(damaged.id, manifest["previous_projection_log"]["generation"])
    previous.write_bytes(b"{broken-previous")
    marker = fresh._manifest_path_for(damaged.id).read_bytes()
    with pytest.raises(ConversationStorageCorruptError):
        fresh.get_conversation(damaged.id)
    with pytest.raises(ConversationStorageCorruptError):
        fresh.save_conversation(damaged)
    assert fresh._manifest_path_for(damaged.id).read_bytes() == marker


@pytest.mark.parametrize("mutation", ["metadata", "generation", "delete"])
def test_inventory_revision_and_publication_are_serialized(tmp_path, monkeypatch, mutation):
    root = tmp_path / "conversations"
    writer = ConversationRepository(root)
    record = writer.create_conversation(title="before")
    reader = ConversationRepository(root)
    _, before_revision, before_rows = reader.list_conversations_with_revision()
    assert [row.title for row in before_rows] == ["before"]
    reached = threading.Event()
    release = threading.Event()
    entered = threading.Event()
    original = writer._safe_write_text

    def pause_manifest(path, value, encoding="utf-8"):
        if path == writer._manifest_path_for(record.id):
            reached.set()
            assert release.wait(3)
        original(path, value, encoding)

    monkeypatch.setattr(writer, "_safe_write_text", pause_manifest)

    def mutate():
        if mutation == "metadata":
            return writer.rename_conversation(record.id, "after")
        if mutation == "delete":
            return writer.delete_conversation(record.id)
        detached = writer.get_conversation(record.id)
        detached.title = "after"
        return writer.save_conversation(detached)

    def inventory():
        entered.set()
        return reader.list_conversations_with_revision()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(mutate)
        assert reached.wait(3)
        reading = pool.submit(inventory)
        assert entered.wait(3)
        try:
            with pytest.raises(TimeoutError):
                reading.result(timeout=0.15)
        finally:
            release.set()
        writing.result(timeout=3)
        _, after_revision, after_rows = reading.result(timeout=3)
    assert after_revision == before_revision + 1
    assert [row.title for row in after_rows] == ([] if mutation == "delete" else ["after"])
    receipt("WP-C03", mutation=mutation, before_revision=before_revision,
            after_revision=after_revision, reader_blocked_until_manifest=True)


def test_artifact_collision_never_replaces_content_or_owner(tmp_path, monkeypatch):
    store = artifact_module.ArtifactStore(storage_dir=tmp_path / "artifacts")
    collision = uuid.UUID("deadbeef-0000-0000-0000-000000000001")
    replacement = uuid.UUID("deadbeef-0000-0000-0000-000000000002")
    ids = iter([collision, collision, replacement])
    monkeypatch.setattr(artifact_module.uuid, "uuid4", lambda: next(ids))
    first = store.save("OWNER_A", "verify", conversation_id="conv_AAAAAA", workspace_root=tmp_path / "A")
    content_path = store._storage_dir / f"{first}.txt"
    meta_path = store._storage_dir / f"{first}.meta.json"
    original_pair = (content_path.read_bytes(), meta_path.read_bytes())
    original = artifact_module.atomic_write_text
    observations = []

    def observe(path, value, **kwargs):
        original(path, value, **kwargs)
        if path.name == f"art_{replacement.hex}.txt":
            reader = artifact_module.ArtifactStore(storage_dir=store._storage_dir)
            observations.append(reader.get(first, conversation_id="conv_AAAAAA", workspace_root=tmp_path / "A"))
            assert reader.get(f"art_{replacement.hex}", conversation_id="conv_BBBBBB", workspace_root=tmp_path / "B") is None

    monkeypatch.setattr(artifact_module, "atomic_write_text", observe)
    second = store.save("OWNER_B_PRIVATE", "verify", conversation_id="conv_BBBBBB", workspace_root=tmp_path / "B")
    assert first != second and len(first) == len("art_") + 32
    assert observations == ["OWNER_A"]
    assert original_pair == (content_path.read_bytes(), meta_path.read_bytes())
    reader = artifact_module.ArtifactStore(storage_dir=store._storage_dir)
    assert reader.get(first, conversation_id="conv_AAAAAA", workspace_root=tmp_path / "A") == "OWNER_A"
    assert reader.get(second, conversation_id="conv_AAAAAA", workspace_root=tmp_path / "A") is None
    assert reader.get(second, conversation_id="conv_BBBBBB", workspace_root=tmp_path / "B") == "OWNER_B_PRIVATE"
    receipt("WP-C04", first=first, second=second, old_owner_during_publication=observations,
            original_pair_unchanged=True)


@pytest.mark.parametrize("reserved_suffix", [".txt", ".meta.json"])
def test_artifact_orphan_also_reserves_id(tmp_path, monkeypatch, reserved_suffix):
    store = artifact_module.ArtifactStore(storage_dir=tmp_path / "artifacts")
    collision, replacement = uuid.uuid4(), uuid.uuid4()
    reserved = store._storage_dir / f"art_{collision.hex}{reserved_suffix}"
    reserved.write_bytes(b"orphan-evidence")
    ids = iter([collision, replacement])
    monkeypatch.setattr(artifact_module.uuid, "uuid4", lambda: next(ids))
    saved = store.save("new", "verify")
    assert saved == f"art_{replacement.hex}"
    assert reserved.read_bytes() == b"orphan-evidence"


def test_artifact_io_errors_are_not_allocation_fallbacks(tmp_path, monkeypatch):
    store = artifact_module.ArtifactStore(storage_dir=tmp_path / "artifacts")
    original = artifact_module.atomic_write_text

    def fail_metadata(path, value, **kwargs):
        if path.name.endswith(".meta.json"):
            raise OSError("metadata publication failure")
        original(path, value, **kwargs)

    monkeypatch.setattr(artifact_module, "atomic_write_text", fail_metadata)
    with pytest.raises(OSError, match="metadata publication failure"):
        store.save("new", "verify")
    assert list(store._storage_dir.iterdir()) == []

    def fail_temporary_allocation(*args, **kwargs):
        raise FileExistsError("temporary inode allocation failed")

    monkeypatch.setattr(artifact_module, "atomic_write_text", fail_temporary_allocation)
    with pytest.raises(FileExistsError, match="temporary inode allocation failed"):
        store.save("new", "verify")


@pytest.mark.parametrize("recurring", [True, False])
def test_scheduler_claim_and_lifecycle_ignore_unrelated_publications(tmp_path, monkeypatch, recurring):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "process" / "scheduled_tasks.json")

    async def scenario():
        fired = []
        scheduler = scheduler_module.TaskScheduler()
        task = scheduler.add_task("A", "prompt", "* * * * *", workspace_root=str(tmp_path / "A"), recurring=recurring)
        other = scheduler.add_task("B", "prompt", "* * * * *", workspace_root=str(tmp_path / "B"))
        target = scheduler_module._project_schedule_file(task.workspace_root)
        other_path = scheduler_module._project_schedule_file(other.workspace_root)
        registry = scheduler_module._registry_file()
        before = (other_path.read_bytes(), registry.read_bytes(), scheduler_module.SCHEDULE_FILE.read_bytes())
        original = scheduler_module.atomic_write_text
        written = []

        def reject_other_publication(path, value, **kwargs):
            if path != target:
                raise OSError("injected ancillary registry/other-project failure")
            written.append(str(path))
            original(path, value, **kwargs)

        monkeypatch.setattr(scheduler_module, "atomic_write_text", reject_other_publication)

        async def callback(task, run):
            fired.append(run.id)
            scheduler.bind_run_conversation(run.id, "conv_AAAAAA")
            return {"status": "completed"}

        scheduler._on_fire = callback
        run = scheduler.run_now(task.id)
        durable = json.loads(target.read_text(encoding="utf-8"))
        assert [(row["id"], row["status"]) for row in durable["runs"]] == [(run.id, "pending")]
        assert durable["tasks"][0]["last_run_id"] == run.id
        fresh = scheduler_module.TaskScheduler()
        replay = fresh._reconcile_orphaned_runs()
        assert [pending.id for _, pending in replay] == [run.id]
        scheduler._tick(datetime.now(UTC))
        assert len(scheduler._runs) == 1
        if recurring:
            assert scheduler.run_now(task.id).id == run.id
        worker = scheduler._run_tasks[run.id]
        await worker
        await asyncio.sleep(0)
        assert fired == [run.id]
        final = json.loads(target.read_text(encoding="utf-8"))
        assert final["runs"][0]["status"] == "completed"
        assert final["runs"][0]["conversation_id"] == "conv_AAAAAA"
        assert before == (other_path.read_bytes(), registry.read_bytes(), scheduler_module.SCHEDULE_FILE.read_bytes())
        assert scheduler_module.TaskScheduler()._reconcile_orphaned_runs() == []
        receipt("WP-C05", recurring=recurring, claim_id=run.id, pending_replay_count=len(replay),
                callback_count=len(fired), writes=written, ancillary_files_unchanged=True)

    asyncio.run(scenario())


def test_scheduler_precommit_failure_leaves_no_consumed_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "process" / "scheduled_tasks.json")

    async def scenario():
        fired = []

        async def callback(task, run):
            fired.append(run.id)
            return {"status": "completed"}

        scheduler = scheduler_module.TaskScheduler(on_fire=callback)
        task = scheduler.add_task("A", "prompt", "* * * * *", workspace_root=str(tmp_path / "A"), recurring=False)
        target = scheduler_module._project_schedule_file(task.workspace_root)
        before = target.read_bytes()
        cursor = task.to_dict()
        original = scheduler_module.atomic_write_text

        def fail_project(*args, **kwargs):
            raise OSError("project commit failed")

        monkeypatch.setattr(scheduler_module, "atomic_write_text", fail_project)
        with pytest.raises(OSError, match="project commit failed"):
            scheduler.run_now(task.id)
        await asyncio.sleep(0)
        assert target.read_bytes() == before and task.to_dict() == cursor
        assert scheduler._runs == {} and scheduler._run_tasks == {} and fired == []
        assert scheduler_module.TaskScheduler()._reconcile_orphaned_runs() == []
        monkeypatch.setattr(scheduler_module, "atomic_write_text", original)
        run = scheduler.run_now(task.id)
        await scheduler._run_tasks[run.id]
        await asyncio.sleep(0)
        assert fired == [run.id]
        receipt("WP-C05-precommit", request_failed=True, no_durable_claim=True, retry_callback_count=len(fired))

    asyncio.run(scenario())
