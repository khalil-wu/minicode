from __future__ import annotations

from pathlib import Path

from backend.runtime_paths import agent_runtime_root
from backend.terminal.task_output import _runtime_state_root


def test_agent_runtime_defaults_to_minicode_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("MINICODE_STATE_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    expected = tmp_path / ".minicode" / "data" / "agent-runtime"
    assert agent_runtime_root() == expected
    assert _runtime_state_root() == expected


def test_agent_runtime_explicit_base_is_exact(tmp_path: Path) -> None:
    base = tmp_path / "isolated-runtime"
    assert agent_runtime_root(base) == base.resolve()
