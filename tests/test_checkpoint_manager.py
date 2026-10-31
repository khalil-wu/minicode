from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.checkpoint import CheckpointManager, CheckpointRecord, CheckpointStore
from backend.checkpoint.store import CheckpointCorruptError, MAX_RETAINED_FILE_CHECKPOINTS


def test_checkpoint_rewind_restores_existing_file(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    record = asyncio.run(
        manager.snapshot(
            tool_name="write_file",
            args={"file_path": "app.py", "content": "after\n"},
            workspace_root=workspace,
            conversation_id="conv_1",
            session_id="session_1",
            tool_call_id="tool_1",
        )
    )
    assert record is not None
    target.write_text("after\n", encoding="utf-8")

    rewound = asyncio.run(manager.rewind(record.id))

    assert rewound.id == record.id
    assert target.read_text(encoding="utf-8") == "before\n"


def test_large_checkpoint_and_rewind_stream_file_contents(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "large.bin"
    chunk = bytes(range(256)) * 4096
    with target.open("wb") as handle:
        for _ in range(8):
            handle.write(chunk)
    store = CheckpointStore(tmp_path / "checkpoints")
    manager = CheckpointManager(store)
    real_read_bytes = Path.read_bytes

    def reject_whole_file_reads(path):
        if path == target or path.parent == store._blobs:
            raise AssertionError("checkpoint payload must be streamed")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_reads)
    record = asyncio.run(manager.snapshot(
        tool_name="write_file", args={"file_path": target.name}, workspace_root=workspace,
        conversation_id="large-checkpoint",
    ))
    target.write_bytes(b"changed")
    asyncio.run(manager.rewind(record.id))
    with target.open("rb") as handle:
        for _ in range(8):
            assert handle.read(len(chunk)) == chunk
        assert handle.read(1) == b""


def test_missing_later_blob_does_not_publish_earlier_restores(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(tmp_path / "checkpoints")
    manager = CheckpointManager(store)
    first, second = workspace / "first.txt", workspace / "second.txt"
    first.write_bytes(b"first before")
    second.write_bytes(b"second before")
    chosen = asyncio.run(manager.snapshot(
        tool_name="write_file", args={"file_path": first.name}, workspace_root=workspace,
        conversation_id="missing-later-blob",
    ))
    later = asyncio.run(manager.snapshot(
        tool_name="write_file", args={"file_path": second.name}, workspace_root=workspace,
        conversation_id="missing-later-blob",
    ))
    first.write_bytes(b"first current")
    second.write_bytes(b"second current")
    store._blob_path(later.files[0].blob).unlink()
    with pytest.raises(CheckpointCorruptError, match="blob"):
        asyncio.run(manager.rewind(chosen.id))
    assert first.read_bytes() == b"first current"
    assert second.read_bytes() == b"second current"


def test_checkpoint_rewind_removes_file_created_after_snapshot(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "new.txt"
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    record = asyncio.run(
        manager.snapshot(
            tool_name="write_file",
            args={"file_path": "new.txt", "content": "hello\n"},
            workspace_root=workspace,
            conversation_id="conv_1",
            session_id="session_1",
            tool_call_id="tool_1",
        )
    )
    assert record is not None
    target.write_text("hello\n", encoding="utf-8")

    asyncio.run(manager.rewind(record.id))

    assert not target.exists()


def test_checkpoint_rewind_ignores_older_file_history(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = workspace / "created.txt"
    other = workspace / "other.txt"
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    older = asyncio.run(
        manager.snapshot(
            tool_name="write_file",
            args={"file_path": "created.txt", "content": "created\n"},
            workspace_root=workspace,
            conversation_id="conv-history",
        )
    )
    assert older is not None
    created.write_text("created\n", encoding="utf-8")

    chosen = asyncio.run(
        manager.snapshot(
            tool_name="write_file",
            args={"file_path": "other.txt", "content": "other\n"},
            workspace_root=workspace,
            conversation_id="conv-history",
        )
    )
    assert chosen is not None
    other.write_text("other\n", encoding="utf-8")

    asyncio.run(manager.rewind(chosen.id))

    assert created.read_text(encoding="utf-8") == "created\n"
    assert not other.exists()


def test_checkpoint_rewind_restores_first_later_snapshot_state(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("at chosen\n", encoding="utf-8")
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    chosen = asyncio.run(
        manager.snapshot(
            tool_name="write_file",
            args={"file_path": "other.txt", "content": "other\n"},
            workspace_root=workspace,
            conversation_id="conv-later",
        )
    )
    assert chosen is not None
    workspace.joinpath("other.txt").write_text("other\n", encoding="utf-8")

    later = asyncio.run(
        manager.snapshot(
            tool_name="write_file",
            args={"file_path": "app.py", "content": "later\n"},
            workspace_root=workspace,
            conversation_id="conv-later",
        )
    )
    assert later is not None
    target.write_text("later\n", encoding="utf-8")

    asyncio.run(manager.rewind(chosen.id))

    assert target.read_text(encoding="utf-8") == "at chosen\n"


def test_checkpoint_ignores_non_write_tools(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    record = asyncio.run(
        manager.snapshot(
            tool_name="read_file",
            args={"file_path": "app.py"},
            workspace_root=workspace,
        )
    )

    assert record is None


def test_checkpoint_store_serializes_concurrent_save_get_delete(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    records = [
        CheckpointRecord(
            id=f"cp-{index}",
            conversation_id="conv-concurrent",
            session_id="session",
            tool_call_id=f"tool-{index}",
            tool_name="write_file",
            workspace_root=str(tmp_path),
            paths=[f"file-{index}.txt"],
            files=[],
        )
        for index in range(40)
    ]

    def save_and_read(record: CheckpointRecord) -> CheckpointRecord | None:
        store.save(record)
        return store.get(record.id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        loaded = list(executor.map(save_and_read, records))

    assert {record.id for record in loaded if record is not None} == {
        record.id for record in records
    }
    assert [record.id for record in store.list_for_conversation("conv-concurrent", limit=0)] == []

    with ThreadPoolExecutor(max_workers=8) as executor:
        removed = list(
            executor.map(
                lambda _record: store.delete_for_conversation("conv-concurrent"),
                records,
            )
        )
    assert max(removed) == len(records)
    assert store.list_for_conversation("conv-concurrent") == []


def test_corrupt_checkpoint_isolated_to_its_owner(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    store = CheckpointStore(tmp_path / "checkpoints")
    manager = CheckpointManager(store)

    broken = asyncio.run(manager.snapshot(
        tool_name="write_file",
        args={"file_path": "app.py", "content": "one\n"},
        workspace_root=workspace,
        conversation_id="conversation-broken",
    ))
    healthy = asyncio.run(manager.snapshot(
        tool_name="write_file",
        args={"file_path": "app.py", "content": "two\n"},
        workspace_root=workspace,
        conversation_id="conversation-healthy",
    ))
    assert broken is not None and healthy is not None
    store._path_for(broken.id).write_text("{broken", encoding="utf-8")

    assert [record.id for record in manager.list_for_conversation("conversation-healthy")] == [healthy.id]
    assert store.scan_status.degraded is False
    assert manager.list_for_conversation("conversation-broken") == []
    assert store.scan_status.degraded is True
    with pytest.raises(CheckpointCorruptError, match="exists but is corrupt"):
        manager.get(broken.id)

    assert manager.delete_for_conversation("conversation-broken") == 1
    assert manager.get(healthy.id) is not None


def test_checkpoint_scan_status_belongs_to_its_own_result(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"before")
    store = CheckpointStore(tmp_path / "checkpoints")
    manager = CheckpointManager(store)
    broken = asyncio.run(manager.snapshot(tool_name="write_file", args={"file_path": "a.txt"},
        workspace_root=workspace, conversation_id="owner-broken"))
    store._path_for(broken.id).write_bytes(b"{broken")

    records, status = manager.list_for_conversation_with_status("owner-broken")
    assert records == [] and len(status.corrupt_files) == 1
    manager.list_for_conversation_with_status("owner-healthy")
    assert status.degraded and not store.scan_status.degraded


def test_file_checkpoints_are_pruned_per_conversation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    store = CheckpointStore(tmp_path / "checkpoints")
    manager = CheckpointManager(store)

    for index in range(MAX_RETAINED_FILE_CHECKPOINTS + 1):
        target.write_text(f"version {index}\n", encoding="utf-8")
        record = asyncio.run(manager.snapshot(
            tool_name="write_file",
            args={"file_path": "app.py", "content": f"version {index + 1}\n"},
            workspace_root=workspace,
            conversation_id="conversation-retained",
        ))
        assert record is not None

    records = manager.list_for_conversation("conversation-retained", limit=None)
    assert len(records) == MAX_RETAINED_FILE_CHECKPOINTS
    assert len(list((tmp_path / "checkpoints" / "blobs").iterdir())) == MAX_RETAINED_FILE_CHECKPOINTS
