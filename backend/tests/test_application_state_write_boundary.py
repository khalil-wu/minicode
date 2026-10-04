from types import SimpleNamespace

import pytest

from backend.agent.run_context import RunContext
from backend.config import PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import FileSystemAccessMode, sandbox_policy_for_permission_context
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.edit_file import EditFileTool
from backend.tools.notebook_tool import NotebookEditTool
from backend.tools.write_file import WriteFileTool


@pytest.fixture
def owner(tmp_path, monkeypatch):
    from backend import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / "app-data"
    state.mkdir()
    monkeypatch.setattr(config, "DATA_ROOT", state)
    repository = ConversationRepository(workspace / "custom-conversations")
    runtime_root = workspace / "custom-runtime"
    runtime_root.mkdir()
    run_context = RunContext(
        conversation_repository=repository,
        agent_runtime=SimpleNamespace(state_root=runtime_root),
    )
    context = ToolExecutionContext(
        permission=PermissionContext(mode="auto", workspace_root=workspace),
        workspace_root=workspace,
        run_context=run_context,
    )
    return workspace, (state, repository.storage_root, runtime_root), context


@pytest.mark.parametrize("root_index", range(3))
@pytest.mark.parametrize("kind", ["write", "edit", "patch", "notebook"])
@pytest.mark.asyncio
async def test_generic_file_tools_refuse_host_storage(owner, root_index, kind):
    _, roots, context = owner
    target = roots[root_index] / ("receipt.ipynb" if kind == "notebook" else "receipt.txt")
    target.write_text("original", encoding="utf-8")
    if kind == "write":
        tool, args = WriteFileTool(), {"file_path": str(target), "content": "changed"}
    elif kind == "edit":
        tool, args = EditFileTool(), {"file_path": str(target), "old_string": "original", "new_string": "changed"}
    elif kind == "notebook":
        tool, args = NotebookEditTool(), {"notebook_path": str(target), "new_source": "changed"}
    else:
        tool, args = ApplyPatchTool(), {"patch": f"*** Begin Patch\n*** Delete File: {target.as_posix()}\n*** End Patch"}
    result = await tool.execute(args, context)
    assert result.is_error
    assert "protected" in result.content.lower()
    assert target.read_text(encoding="utf-8") == "original"


def test_checker_and_managed_shell_share_actual_state_roots(owner):
    workspace, roots, context = owner
    checker = PermissionChecker(PermissionSettings(), workspace)
    policy = sandbox_policy_for_permission_context(workspace, context.permission)
    resolved = policy.resolve(cwd=workspace)
    for root in roots:
        target = root / "receipt.txt"
        assert not checker.validate_file_operation(str(target), "write", context=context.permission)[0]
        assert resolved.resolve_access(target) is FileSystemAccessMode.READ
        assert not any(entry.is_path_writable(target) for entry in resolved.writable_roots)
    assert resolved.resolve_access(workspace / "data/user.txt") is FileSystemAccessMode.WRITE
    assert checker.validate_file_operation("data/user.txt", "write", context=context.permission)[0]


@pytest.mark.asyncio
async def test_regular_data_directory_and_host_repository_remain_writable(owner):
    workspace, _, context = owner
    result = await WriteFileTool().execute({"file_path": "data/user.txt", "content": "user content"}, context)
    assert not result.is_error
    assert (workspace / "data/user.txt").read_text(encoding="utf-8") == "user content"
    record = context.run_context.conversation_repository.create_conversation(title="Host owned")
    assert context.run_context.conversation_repository.get_conversation(record.id).title == "Host owned"


def test_explicit_nested_write_grant_does_not_reopen_state(owner):
    workspace, roots, context = owner
    from dataclasses import replace

    permission = replace(context.permission, filesystem_constraints={"write_allowlist": [".", str(roots[0] / "nested")]})
    resolved = sandbox_policy_for_permission_context(workspace, permission).resolve(cwd=workspace)
    assert resolved.resolve_access(roots[0] / "nested/receipt.txt") is FileSystemAccessMode.READ
