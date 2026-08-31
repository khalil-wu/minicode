from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

import backend.artifact.store as artifact_store_module
from backend.artifact.store import ArtifactPersistenceError, ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.owner_scope import OwnerScope, canonical_workspace_root
from backend.services.artifact_service import read_artifact_content


def test_artifact_composite_owner_scope_is_exact_across_all_read_paths(tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store = ArtifactStore(storage_dir=artifact_dir)
    artifact_id = store.save(
        "private body",
        source="owner-test",
        type="text",
        conversation_id="conv-a",
        workspace_root=workspace_a,
    )

    assert store.get(artifact_id) is None
    assert store.get(artifact_id, conversation_id="conv-a") is None
    assert store.get(artifact_id, workspace_root=workspace_a) is None
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_b,
    ) is None
    assert store.get(
        artifact_id,
        conversation_id="conv-b",
        workspace_root=workspace_a,
    ) is None
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_a,
    ) == "private body"
    assert store.get_meta(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_a,
    ) is not None
    assert store.get_preview(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_a,
    ) == "private body"
    assert [
        meta.artifact_id
        for meta in store.list_artifacts(
            conversation_id="conv-a",
            workspace_root=workspace_a,
        )
    ] == [artifact_id]
    assert store.list_artifacts(
        conversation_id="conv-a",
        workspace_root=workspace_b,
    ) == []

    # A new store exercises the cold disk path with the same authorization.
    cold_store = ArtifactStore(storage_dir=artifact_dir)
    assert cold_store.get(artifact_id) is None
    assert cold_store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_a,
    ) == "private body"


def test_artifact_grants_do_not_form_a_conversation_workspace_cross_product(tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store = ArtifactStore(storage_dir=artifact_dir)
    artifact_id = store.save(
        "shared body",
        source="owner-test",
        conversation_id="conv-a",
        workspace_root=workspace_a,
    )

    assert store.share_for_conversation("conv-a", "conv-b", workspace_b) == 1
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_a,
    ) == "shared body"
    assert store.get(
        artifact_id,
        conversation_id="conv-b",
        workspace_root=workspace_b,
    ) == "shared body"
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_b,
    ) is None
    assert store.get(
        artifact_id,
        conversation_id="conv-b",
        workspace_root=workspace_a,
    ) is None

    assert store.delete_for_conversation("conv-a") == 0
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace_a,
    ) is None
    assert store.get(
        artifact_id,
        conversation_id="conv-b",
        workspace_root=workspace_b,
    ) == "shared body"
    assert store.delete_for_conversation("conv-b") == 1
    assert store.get(
        artifact_id,
        conversation_id="conv-b",
        workspace_root=workspace_b,
    ) is None


def test_artifact_missing_or_malformed_sidecar_never_falls_back_to_cached_content(tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore(storage_dir=artifact_dir)
    artifact_id = store.save(
        "cached secret",
        source="owner-test",
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace,
    ) == "cached secret"

    sidecar_path = artifact_dir / f"{artifact_id}.meta.json"
    sidecar_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ArtifactPersistenceError):
        store.get(
            artifact_id,
            conversation_id="conv-a",
            workspace_root=workspace,
        )

    payload = {
        "schema_version": 4,
        "artifact_id": artifact_id,
        "source": "owner-test",
        "type": "text",
        "size": len("cached secret"),
        "preview": "cached secret",
        "preview_lines": 5,
        "created_at": 1.0,
        "conversation_id": "conv-a",
        "conversation_ids": ["conv-a"],
        "workspace_root": str(workspace),
        "owner_scopes": [
            {"conversation_id": "conv-a", "workspace_root": "bad\u0000path"}
        ],
    }
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactPersistenceError):
        store.get(
            artifact_id,
            conversation_id="conv-a",
            workspace_root=workspace,
        )


def test_artifact_warm_cache_rechecks_the_authoritative_owner_before_return(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = store.save(
        "cached secret",
        source="owner-test",
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    meta_path = tmp_path / "artifacts" / f"{artifact_id}.meta.json"
    original = artifact_store_module._read_meta_sidecar(meta_path)
    assert original is not None
    replacement_scope = OwnerScope("conv-b", canonical_workspace_root(workspace))
    replacement = replace(
        original,
        conversation_id="conv-b",
        conversation_ids=("conv-b",),
        owner_scopes=(replacement_scope,),
    )
    calls = 0

    def racing_read(path):
        nonlocal calls
        calls += 1
        return original if calls == 1 else replacement

    monkeypatch.setattr(artifact_store_module, "_read_meta_sidecar", racing_read)

    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=workspace,
    ) is None
    assert calls >= 2


def test_ownerless_legacy_artifact_is_not_public_to_a_scoped_conversation(tmp_path) -> None:
    artifact_id = "art_legacy"
    (tmp_path / f"{artifact_id}.json").write_text(
        json.dumps({
            "artifact_id": artifact_id,
            "source": "legacy",
            "type": "text",
            "size": 13,
            "preview": "legacy secret",
            "content": "legacy secret",
        }),
        encoding="utf-8",
    )
    store = ArtifactStore(storage_dir=tmp_path)

    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=tmp_path / "workspace",
    ) is None
    assert store.get(artifact_id) is None


def test_ambiguous_pre_composite_sidecar_fails_closed(tmp_path) -> None:
    artifact_id = "art_v3"
    (tmp_path / f"{artifact_id}.txt").write_text("v3 secret", encoding="utf-8")
    (tmp_path / f"{artifact_id}.meta.json").write_text(
        json.dumps({
            "schema_version": 3,
            "artifact_id": artifact_id,
            "source": "legacy-v3",
            "type": "text",
            "size": 9,
            "preview": "v3 secret",
            "preview_lines": 5,
            "created_at": 1.0,
            "conversation_id": "conv-a",
            "conversation_ids": ["conv-a"],
            "workspace_root": "",
        }),
        encoding="utf-8",
    )
    store = ArtifactStore(storage_dir=tmp_path)

    with pytest.raises(ArtifactPersistenceError):
        store.get(
            artifact_id,
            conversation_id="conv-a",
            workspace_root=tmp_path / "workspace",
        )


def test_projectless_v4_scope_is_still_an_exact_composite_owner(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = store.save(
        "projectless",
        source="owner-test",
        conversation_id="conv-a",
        workspace_root="",
    )

    assert store.get(artifact_id, conversation_id="conv-a", workspace_root="") == "projectless"
    assert store.get(
        artifact_id,
        conversation_id="conv-a",
        workspace_root=tmp_path / "workspace",
    ) is None

def test_attachment_composite_scope_guards_payload_metadata_native_preview_and_aliases(tmp_path) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store = AttachmentStore(tmp_path / "attachments")
    native = base64.b64encode(b"private bytes").decode("ascii")
    store.save(
        artifact_id="attachment-a",
        content="private attachment",
        native_data=native,
        metadata={
            "conversation_id": "conv-a",
            "workspace_root": str(workspace_a),
            "attachment": {
                "doc_id": "doc-a",
                "file_name": "private.txt",
                "media_type": "text/plain",
            },
        },
    )

    assert store.get("attachment-a") is None
    assert store.get_payload("attachment-a", conversation_id="conv-a") is None
    assert store.get_metadata("attachment-a", workspace_root=str(workspace_a)) == {}
    assert store.get_native_data(
        "attachment-a",
        conversation_id="conv-a",
        workspace_root=str(workspace_b),
    ) is None
    assert store.get_preview(
        "attachment-a",
        conversation_id="conv-b",
        workspace_root=str(workspace_a),
    ) is None
    assert store.find_payload(
        "doc-a",
        conversation_id="conv-a",
        workspace_root=str(workspace_a),
    )["artifact_id"] == "attachment-a"
    assert store.resolve_content(
        "private.txt",
        conversation_id="conv-a",
        workspace_root=str(workspace_a),
    )[1] == "private attachment"
    assert store.get_native_data(
        "attachment-a",
        conversation_id="conv-a",
        workspace_root=str(workspace_a),
    ) == native

    assert store.share_for_conversation("conv-a", "conv-b", workspace_b) == 1
    assert store.get(
        "attachment-a",
        conversation_id="conv-b",
        workspace_root=str(workspace_b),
    ) == "private attachment"
    assert store.get(
        "attachment-a",
        conversation_id="conv-a",
        workspace_root=str(workspace_b),
    ) is None
    assert store.get(
        "attachment-a",
        conversation_id="conv-b",
        workspace_root=str(workspace_a),
    ) is None

    assert store.delete_for_conversation("conv-a") == 0
    assert store.get(
        "attachment-a",
        conversation_id="conv-b",
        workspace_root=str(workspace_b),
    ) == "private attachment"
    assert store.delete_for_conversation("conv-b") == 1


def test_ownerless_or_malformed_attachment_metadata_fails_closed_for_scoped_reads(tmp_path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    store.save(artifact_id="legacy", content="legacy", metadata={})

    assert store.get(
        "legacy",
        conversation_id="conv-a",
        workspace_root=str(tmp_path / "workspace"),
    ) is None
    assert store.get("legacy") == "legacy"

    payload_path = tmp_path / "attachments" / "legacy.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["metadata"] = {
        "owner_scope_version": 1,
        "owner_scopes": [{"conversation_id": [], "workspace_root": {}}],
    }
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.get("legacy") is None
    assert store.share_for_conversation("conv-a", "conv-b", tmp_path) == 0
    assert store.delete_for_conversation("conv-a") == 0


def test_artifact_service_uses_the_same_scope_for_content_and_metadata(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    attachment_store = AttachmentStore(tmp_path / "attachments")
    artifact_id = artifact_store.save(
        "base64-image-content",
        source="image-tool",
        type="image",
        conversation_id="conv-a",
        workspace_root=workspace,
    )

    result = read_artifact_content(
        artifact_store,
        attachment_store,
        artifact_id,
        conversation_id="conv-a",
        workspace_root=str(workspace),
    )

    assert result.content == "base64-image-content"
    assert result.media_type == "image/png"
