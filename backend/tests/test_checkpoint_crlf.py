from __future__ import annotations

import asyncio
from pathlib import Path

from backend.checkpoint.manager import CheckpointManager


def test_rewind_preserves_crlf_bytes(tmp_path: Path) -> None:
    """cc fileHistory snapshots are byte-exact; CRLF must survive a rewind."""

    target = tmp_path / "file.txt"
    original = b"line one\r\nline two\r\n"
    target.write_bytes(original)

    manager = CheckpointManager()
    record = asyncio.run(
        manager.snapshot(
            tool_name="edit_file",
            args={"file_path": str(target), "old_string": "x", "new_string": "y"},
            workspace_root=tmp_path,
            conversation_id="conv",
        )
    )
    assert record is not None

    target.write_bytes(b"line one CHANGED\r\nline two\r\n")
    asyncio.run(manager.rewind(record.id))

    assert target.read_bytes() == original
