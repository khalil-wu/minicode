from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.memory import file_memory
from backend.workspace import recent_projects


@pytest.mark.parametrize("scoped", [False, True])
def test_default_memory_and_reset_lock_stay_inside_test_storage(tmp_path, scoped):
    data_root = tmp_path / "state" / "data"
    assert file_memory.DATA_ROOT == data_root
    assert file_memory.MEMORY_DIR == data_root / "memory"
    workspace = tmp_path / "workspace" if scoped else None
    memory = file_memory.FileMemory.for_workspace(workspace)
    assert memory.memory_dir.is_relative_to(data_root / "memory")
    assert memory.reset_lock_path == data_root / ".memory.reset.lock"
    assert memory.reset_lock_path.exists()
    assert (memory.memory_dir / "MEMORY.md").exists()


def test_default_recent_projects_stay_inside_test_storage(tmp_path):
    expected = tmp_path / "state" / "data" / "recent_projects.json"
    assert recent_projects.DEFAULT_STORE_PATH == expected
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = recent_projects.RecentProjectStore()
    assert store.list() == []
    store.add(str(workspace), "isolated workspace")
    records = json.loads(expected.read_text(encoding="utf-8"))
    assert [Path(record["path"]) for record in records] == [workspace]
