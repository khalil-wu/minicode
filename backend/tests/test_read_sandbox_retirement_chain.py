from __future__ import annotations

import pytest

from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxCapability, SandboxRunner, SandboxUnavailableError
from backend.tools.base import validate_tool_input
from backend.tools.read_file import ReadFileTool


@pytest.mark.parametrize("field", ["start_line", "end_line"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_read_range_rejects_invalid_values_at_existing_schema_boundary(tmp_path, field, value):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    try:
        assert validate_tool_input(ReadFileTool(store), {"file_path": "source.py", field: value})
    finally:
        store.shutdown()


@pytest.mark.asyncio
async def test_repeated_focused_reads_keep_real_content_and_edit_hash_without_unused_range_ledger(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    path = tmp_path / "source.py"
    path.write_text("first\nsecond\n", encoding="utf-8")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    reader = ReadFileTool(store)
    try:
        args = {"file_path": "source.py", "start_line": 2.0, "end_line": 2}
        assert validate_tool_input(reader, args) == ""
        first = await reader.execute(args, context)
        second = await reader.execute(args, context)
        assert not first.is_error and not second.is_error
        assert first.content == second.content
        assert "second" in second.content and "write-safe full-file" in second.content
        assert "_read_file_hashes" in context.metadata
        assert "_read_file_ranges" not in context.metadata
    finally:
        store.shutdown()


def test_unavailable_windows_policy_does_not_select_retired_token_launcher(tmp_path, monkeypatch):
    import backend.sandbox.runner as runner_module
    import backend.sandbox.windows_native as native

    monkeypatch.setattr(runner_module.sys, "platform", "win32")
    monkeypatch.setattr(native, "discover_runtime", lambda: (None, "test native unavailable"))
    monkeypatch.setattr(runner_module, "_container_runtime", lambda: ("", "", "test container unavailable"))
    capability = SandboxRunner(SandboxPolicy.workspace_default(tmp_path)).capability(cwd=tmp_path)
    assert not capability.available
    assert "another Low-labelled workspace" in capability.reason
    assert not capability.filesystem_isolated and not capability.network_isolated


def test_retired_backend_has_no_wrapping_branch_or_temp_ownership(tmp_path):
    runner = SandboxRunner(SandboxPolicy.workspace_default(tmp_path))
    capability = SandboxCapability(available=True, backend="low-integrity", filesystem_isolated=True, network_isolated=False)
    with pytest.raises(SandboxUnavailableError, match="Unsupported sandbox backend"):
        runner._wrap_command("unused", capability, cwd=tmp_path)
    assert not hasattr(runner, "_low_integrity_temp_dir")
