from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.lsp import client as lsp_client
from backend.lsp.client import LSPClient, LSPManager
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools import lsp_tools


@pytest.fixture(params=[
    lsp_tools.LSPGoToDefinitionTool,
    lsp_tools.LSPFindReferencesTool,
    lsp_tools.LSPHoverTool,
    lsp_tools.LSPDocumentSymbolsTool,
], ids=["definition", "references", "hover", "symbols"])
def tool(request):
    return request.param()


@pytest.fixture
def manager(monkeypatch):
    captured = SimpleNamespace(availability=[], clients=[])

    class RecordingClient(LSPClient):
        def __init__(self, workspace_root: str) -> None:
            super().__init__(
                "protocol-fixture", [], workspace_root,
                sandbox_runner=SimpleNamespace(
                    map_path_to_sandbox=lambda path: path,
                    map_path_from_sandbox=lambda path: path,
                ),
            )
            self.notifications = []

        async def _send_notification(self, method: str, params: dict) -> None:
            self.notifications.append((method, params))

        async def _send_request(self, method: str, params: dict):
            return {"contents": "fixture hover"} if method == "textDocument/hover" else []

    def is_available(file_path: str, workspace_root: str) -> bool:
        captured.availability.append((file_path, workspace_root))
        return True

    async def get_client(file_path: str, workspace_root: str) -> RecordingClient:
        client = RecordingClient(workspace_root)
        captured.clients.append(client)
        return client

    captured.is_available = is_available
    captured.get_client = get_client
    monkeypatch.setattr(lsp_tools, "get_lsp_manager", lambda: captured)
    return captured


@pytest.mark.parametrize("absolute_file", [False, True], ids=["relative", "absolute"])
@pytest.mark.parametrize("root_argument", [None, ".", "src"], ids=["owner-root", "dot-root", "nested-root"])
def test_lsp_tools_read_the_owned_file_and_scope_the_server(
    tmp_path: Path, monkeypatch, tool, manager, absolute_file: bool, root_argument: str | None,
) -> None:
    parent_project = tmp_path / "parent-project"
    workspace = parent_project / "selected-workspace"
    source_directory = workspace / "src"
    source_directory.mkdir(parents=True)
    (parent_project / ".git").mkdir()
    source = source_directory / "sample.py"
    source.write_text("SELECTED_WORKSPACE_MARKER = 1\n", encoding="utf-8")
    process_cwd = tmp_path / "server-cwd"
    (process_cwd / "src").mkdir(parents=True)
    (process_cwd / "src" / "sample.py").write_text("OUTSIDE_WORKSPACE_MARKER = 1\n", encoding="utf-8")
    monkeypatch.chdir(process_cwd)
    context = ToolExecutionContext(PermissionContext(workspace_root=workspace), workspace_root=workspace)
    args = {"file_path": str(source) if absolute_file else "src/sample.py", "line": 0, "character": 0}
    if root_argument is not None:
        args["workspace_root"] = root_argument

    result = asyncio.run(tool.execute(args, context))

    expected_root = workspace / (root_argument or ".")
    assert not result.is_error
    assert manager.availability == [(str(source.resolve()), str(expected_root.resolve()))]
    assert len(manager.clients) == 1
    client = manager.clients[0]
    assert client._workspace_root == str(expected_root.resolve())
    assert client.notifications == [("textDocument/didOpen", {
        "textDocument": {
            "uri": source.resolve().as_uri(),
            "languageId": "python",
            "version": 1,
            "text": "SELECTED_WORKSPACE_MARKER = 1\n",
        },
    })]


@pytest.mark.parametrize("invalid_scope", ["outside-file", "outside-root", "projectless"])
def test_lsp_tools_reject_paths_outside_the_execution_owner(
    tmp_path: Path, tool, manager, invalid_scope: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("outside = 1\n", encoding="utf-8")
    owner_root = None if invalid_scope == "projectless" else workspace
    context = ToolExecutionContext(PermissionContext(workspace_root=owner_root), workspace_root=owner_root)
    args = {"file_path": str(source), "line": 0, "character": 0}
    if invalid_scope == "outside-file":
        args["file_path"] = "../outside.py"
    elif invalid_scope == "outside-root":
        args["workspace_root"] = ".."

    result = asyncio.run(tool.execute(args, context))

    assert result.is_error
    assert "workspace" in result.content.lower()
    assert manager.availability == []
    assert manager.clients == []


def test_standalone_lsp_keeps_the_process_working_directory_as_its_boundary(
    tmp_path: Path, monkeypatch, manager,
) -> None:
    (tmp_path / ".git").mkdir()
    workspace = tmp_path / "standalone"
    workspace.mkdir()
    source = workspace / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(workspace)

    result = asyncio.run(lsp_tools.LSPDocumentSymbolsTool().execute({"file_path": "sample.py"}))

    assert not result.is_error
    assert manager.availability == [(str(source), str(workspace))]


def test_lsp_availability_uses_the_same_workspace_as_server_start(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    source_directory = workspace / "src"
    source_directory.mkdir(parents=True)
    executable_roots = []
    sandbox_roots = []

    def resolve_executable(server: str, workspace_root: str) -> str:
        executable_roots.append(workspace_root)
        return "trusted-language-server"

    def sandbox_runner(workspace_root: str):
        sandbox_roots.append(workspace_root)
        return SimpleNamespace(capability=lambda: SimpleNamespace(available=True))

    monkeypatch.setattr(lsp_client, "_resolve_server_executable", resolve_executable)
    monkeypatch.setattr(lsp_client, "_lsp_sandbox_runner", sandbox_runner)

    assert LSPManager().is_available(str(source_directory / "sample.py"), str(workspace))
    assert executable_roots == [str(workspace.resolve())]
    assert sandbox_roots == [str(workspace.resolve())]
