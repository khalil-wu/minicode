"""An unreadable instruction or memory file must not stop every turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agent.instruction_discovery import load_project_guideline_bundle
from backend.memory.file_memory import FileMemory


def test_unreadable_instruction_file_is_reported_in_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    locked = root / "AGENTS.md"
    locked.write_text("Always run the unit tests.", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == locked:
            raise PermissionError(13, "Permission denied", str(path))
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    bundle = load_project_guideline_bundle(workspace_dir=root)

    unreadable = [block for block in bundle.blocks if block.path == locked]
    assert len(unreadable) == 1
    assert "could not be read" in unreadable[0].content
    assert "Permission denied" in unreadable[0].content
    assert "Always run the unit tests." not in bundle.rendered_markdown


def test_undecodable_memory_summary_leaves_the_turn_without_memory(tmp_path: Path) -> None:
    memory = FileMemory(tmp_path / "memories")
    (tmp_path / "memories" / "memory_summary.md").write_bytes(b"v1\nsummary with a bad byte \xff\n")

    assert memory.get_context() == ""
