"""File checkpoints cover authorized writes outside the workspace.

The plan file lives in MiniCode's state directory and bypass mode may edit
files outside the project. A write tool refuses to run without a recovery
checkpoint, so such files must be snapshotted and restorable too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.checkpoint.manager import CheckpointManager
from backend.checkpoint.store import CheckpointStore


def test_outside_file_is_snapshotted_and_rewound(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = tmp_path / "state" / "plans" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_bytes(b"# Plan\r\nstep one\r\n")
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    record = asyncio.run(manager.snapshot(
        tool_name="edit_file",
        args={"file_path": str(plan)},
        workspace_root=workspace,
        conversation_id="conversation-1",
    ))
    assert record is not None
    assert record.paths == [plan.resolve().as_posix()]

    plan.write_bytes(b"# Plan\r\nstep 1\r\n")
    asyncio.run(manager.rewind(record.id))
    assert plan.read_bytes() == b"# Plan\r\nstep one\r\n"


def test_workspace_files_keep_relative_keys(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    manager = CheckpointManager(CheckpointStore(tmp_path / "checkpoints"))

    record = asyncio.run(manager.snapshot(
        tool_name="write_file",
        args={"file_path": "src/app.py"},
        workspace_root=workspace,
        conversation_id="conversation-1",
    ))
    assert record is not None and record.paths == ["src/app.py"]
