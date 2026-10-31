from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from backend.artifact.store import ArtifactScanError, ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.conversations.repository import ConversationRepository
from backend.services.conversation_resources_service import conversation_resource_page


def test_damaged_sidecar_keeps_healthy_ownership_operations_and_reports_partial(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    broken = root / "art_private-broken.meta.json"
    broken.write_bytes(b"[]")
    store = ArtifactStore(storage_dir=root)
    artifact_id = store.save("owned content", source="fixture", conversation_id="source", workspace_root=tmp_path)

    failures = []
    items = store.list_artifacts(conversation_id="source", workspace_root=tmp_path, read_errors=failures)
    assert [item.artifact_id for item in items] == [artifact_id]
    assert len(failures) == 1 and failures[0][1].reason == "invalid_record"
    with pytest.raises(ArtifactScanError) as shared:
        store.share_for_conversation("source", "target", tmp_path)
    assert shared.value.completed == 1
    assert store.get(artifact_id, conversation_id="target", workspace_root=tmp_path) == "owned content"

    with pytest.raises(ArtifactScanError):
        store.delete_for_conversation("target")
    assert store.get(artifact_id, conversation_id="target", workspace_root=tmp_path) is None
    assert store.get(artifact_id, conversation_id="source", workspace_root=tmp_path) == "owned content"
    with pytest.raises(ArtifactScanError) as deleted:
        store.delete_for_conversation("source")
    assert deleted.value.completed == 1
    assert not (root / f"{artifact_id}.meta.json").exists()
    assert broken.read_bytes() == b"[]"


def test_cleanup_processes_healthy_artifacts_without_erasing_corrupt_evidence(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    broken = root / "art_bad.meta.json"
    broken.write_bytes(b"{broken")
    store = ArtifactStore(storage_dir=root)
    artifact_id = store.save("expired", source="fixture", conversation_id="source", workspace_root=tmp_path)

    with pytest.raises(ArtifactScanError) as failed:
        store.cleanup_expired(now=time.time() + 100_000)
    assert failed.value.completed == 1
    assert store.cleanup_status()["cleanup_pending"] is True
    assert store.count == 0
    assert not (root / f"{artifact_id}.meta.json").exists()
    assert broken.read_bytes() == b"{broken"
    broken.unlink()
    assert store.cleanup_expired(now=time.time() + 100_000) == 0
    assert store.cleanup_status() == {"cleanup_pending": False, "error": ""}


def test_resource_page_reports_incomplete_artifacts_without_leaking_another_record(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    repo.create_conversation(conversation_id="conv_resource_owner", workspace_root=str(root))
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = artifacts.save("visible", source="owned", conversation_id="conv_resource_owner", workspace_root=root)
    broken = tmp_path / "artifacts" / "art_private-broken.meta.json"
    broken.write_bytes(b"{broken")
    session = SimpleNamespace(conversation_repo=repo, artifact_store=artifacts,
        attachment_store=AttachmentStore(tmp_path / "attachments"),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda record: record.workspace_root))
    page = conversation_resource_page(session_id="session", conversation_id="conv_resource_owner",
        ws_manager=SimpleNamespace(get_session=lambda _: session))
    assert [item["artifact_id"] for item in page["items"]] == [artifact_id]
    assert len(page["read_errors"]) == 1 and not page["read_errors"][0]["artifact_id"]
    assert "private-broken" not in json.dumps(page["read_errors"])
    assert broken.read_bytes() == b"{broken"


@pytest.mark.asyncio
async def test_failed_clone_rolls_back_readable_artifact_grants_with_corrupt_sidecar(tmp_path, monkeypatch):
    from backend.tests.test_conversation_activation_contract import _session
    from backend.ws.handlers import conversation

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    source = repo.create_conversation(conversation_id="conv_source123", workspace_root=str(workspace))
    session = _session(repo, source.id)
    session.attachment_store = AttachmentStore(tmp_path / "attachments")
    session.artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = session.artifact_store.save("original", source="fixture", conversation_id=source.id, workspace_root=workspace)
    broken = tmp_path / "artifacts" / "art_bad.meta.json"
    broken.write_bytes(b"{broken")
    created = []
    clone = repo.clone_conversation

    def capture_clone(*args, **kwargs):
        result = clone(*args, **kwargs)
        created.append(result.id)
        return result

    monkeypatch.setattr(repo, "clone_conversation", capture_clone)
    monkeypatch.setattr(conversation, "_conversation_has_active_run", lambda *_: False)
    try:
        assert await conversation.handle_conversation_clone(session, {"conversation_id": source.id})
        assert len(created) == 1 and repo.get_conversation(created[0]) is None
        assert session.artifact_store.get(artifact_id, conversation_id=source.id, workspace_root=workspace) == "original"
        assert session.artifact_store.get(artifact_id, conversation_id=created[0], workspace_root=workspace) is None
        result = session.emit_command_result.await_args
        assert result.kwargs["level"] == "error" and "share: 1 processed" in result.args[1]
        assert broken.read_bytes() == b"{broken"
    finally:
        await session.artifact_store.aclose()


@pytest.mark.asyncio
async def test_background_cleanup_keeps_working_until_damaged_record_is_repaired(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    broken = root / "art_bad.meta.json"
    broken.write_bytes(b"{broken")
    store = ArtifactStore(storage_dir=root, ttl_seconds=1, cleanup_interval_seconds=0.1)

    def expire(artifact_id):
        path = root / f"{artifact_id}.meta.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["created_at"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.02)

    try:
        first = expire(store.save("first", source="fixture", conversation_id="source"))
        await wait_for(lambda: store.cleanup_status()["cleanup_pending"] and not first.exists())
        second = expire(store.save("second", source="fixture", conversation_id="source"))
        await wait_for(lambda: not second.exists())
        assert not store._cleanup_task.done()
        assert broken.read_bytes() == b"{broken"
        broken.unlink()
        await wait_for(lambda: not store.cleanup_status()["cleanup_pending"])
    finally:
        await store.aclose()
