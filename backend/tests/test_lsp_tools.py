from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from backend.lsp import client as lsp_client
from backend.lsp.client import LSPClient, LSPLocation, _lsp_sandbox_runner, _parse_locations, _uri_to_path
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner
from backend.tools.lsp_tools import LSPGoToDefinitionTool


def test_lsp_uri_to_path_handles_windows_file_uri() -> None:
    path = _uri_to_path("file:///C:/Desktop/project/app.py").replace("\\", "/")

    assert path.endswith("C:/Desktop/project/app.py")


def test_lsp_uses_codex_windows_filesystem_sandbox_without_requiring_container(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lsp_client.sys, "platform", "win32")

    runner = _lsp_sandbox_runner(str(tmp_path))

    assert runner._policy.workspace_root == tmp_path.resolve()
    assert runner._policy.allow_network is True
    assert runner._policy.disable_os_sandbox is False


def test_lsp_parse_locations_supports_location_link() -> None:
    locations = _parse_locations(
        {
            "targetUri": "file:///C:/Desktop/project/app.py",
            "targetSelectionRange": {
                "start": {"line": 4, "character": 8},
                "end": {"line": 4, "character": 11},
            },
        }
    )

    assert len(locations) == 1
    assert locations[0].file.replace("\\", "/").endswith("C:/Desktop/project/app.py")
    assert locations[0].line == 4
    assert locations[0].character == 8
    assert locations[0].end_character == 11


def test_lsp_tool_uses_zero_based_lines_by_default(monkeypatch, tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")

    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def definition(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
            self.calls.append((line, character))
            return [LSPLocation(file=file_path, line=line, character=character)]

    class _Manager:
        def __init__(self) -> None:
            self.client = _Client()

        def is_available(self, file_path: str) -> bool:
            return True

        async def get_client(self, file_path: str, workspace_root: str) -> _Client:
            return self.client

    manager = _Manager()
    monkeypatch.setattr("backend.tools.lsp_tools.get_lsp_manager", lambda: manager)

    result = asyncio.run(
        LSPGoToDefinitionTool().execute(
            {"file_path": str(source), "line": 7, "character": 3, "workspace_root": str(tmp_path)}
        )
    )

    assert not result.is_error
    assert manager.client.calls == [(7, 3)]


def test_lsp_tool_converts_one_based_lines_when_requested(monkeypatch, tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")

    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def definition(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
            self.calls.append((line, character))
            return [LSPLocation(file=file_path, line=line, character=character)]

    class _Manager:
        def __init__(self) -> None:
            self.client = _Client()

        def is_available(self, file_path: str) -> bool:
            return True

        async def get_client(self, file_path: str, workspace_root: str) -> _Client:
            return self.client

    manager = _Manager()
    monkeypatch.setattr("backend.tools.lsp_tools.get_lsp_manager", lambda: manager)

    result = asyncio.run(
        LSPGoToDefinitionTool().execute(
            {
                "file_path": str(source),
                "line": 7,
                "character": 3,
                "line_base": 1,
                "workspace_root": str(tmp_path),
            }
        )
    )

    assert not result.is_error
    assert manager.client.calls == [(6, 3)]


def test_lsp_client_sends_full_text_did_change_for_modified_open_file(tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    client = LSPClient("unused", [], str(tmp_path))
    notifications: list[tuple[str, dict]] = []

    async def send_notification(method: str, params: dict) -> None:
        notifications.append((method, params))

    client._send_notification = send_notification  # type: ignore[method-assign]

    async def scenario() -> None:
        await client._ensure_file_open(str(source))
        source.write_text("value = 2\n", encoding="utf-8")
        await client._ensure_file_open(str(source))

    asyncio.run(scenario())

    assert [method for method, _params in notifications] == [
        "textDocument/didOpen",
        "textDocument/didChange",
    ]
    change = notifications[1][1]
    assert change["textDocument"]["version"] == 2
    assert change["contentChanges"] == [{"text": "value = 2\n"}]


def test_container_lsp_path_mapping_rejects_parent_traversal(tmp_path) -> None:
    runner = SandboxRunner(SandboxPolicy(workspace_root=tmp_path))
    runner.capability = lambda **_kwargs: SimpleNamespace(backend="docker")  # type: ignore[method-assign]

    assert runner.map_path_from_sandbox("/workspace/src/app.py") == str(
        (tmp_path / "src" / "app.py").resolve()
    )
    assert runner.map_path_from_sandbox("/workspace/../../outside.txt") == "/workspace/../../outside.txt"


def test_lsp_request_write_failure_removes_pending_future() -> None:
    class _Process:
        returncode = None

    class _Writer:
        def write(self, _data: bytes) -> None:
            raise BrokenPipeError("language server exited")

        async def drain(self) -> None:
            return None

    client = LSPClient("unused", [], ".")
    client._process = _Process()  # type: ignore[assignment]
    client._stdin = _Writer()  # type: ignore[assignment]

    async def scenario() -> None:
        with pytest.raises(BrokenPipeError):
            await client._send_request("textDocument/definition", {})

    asyncio.run(scenario())
    assert client._pending == {}


def test_lsp_reader_eof_marks_client_not_running() -> None:
    class _Process:
        returncode = None

    class _Stdout:
        async def read(self, _size: int) -> bytes:
            return b""

    client = LSPClient("unused", [], ".")
    client._process = _Process()  # type: ignore[assignment]
    client._stdout = _Stdout()  # type: ignore[assignment]

    async def scenario() -> None:
        client._reader_task = asyncio.create_task(client._read_loop())
        await client._reader_task

    asyncio.run(scenario())
    assert client.is_running() is False


def test_lsp_stop_skips_graceful_request_after_reader_eof() -> None:
    class _Process:
        returncode = None

    class _Runner:
        def __init__(self) -> None:
            self.terminated: list[object] = []

        async def terminate(self, process: object) -> bool:
            self.terminated.append(process)
            return True

    process = _Process()
    runner = _Runner()
    client = LSPClient("unused", [], ".")
    client._process = process  # type: ignore[assignment]
    client._sandbox_runner = runner  # type: ignore[assignment]
    graceful_requests: list[str] = []

    async def send_request(method: str, _params: dict) -> object:
        graceful_requests.append(method)
        return None

    client._send_request = send_request  # type: ignore[method-assign]

    async def scenario() -> None:
        client._reader_task = asyncio.create_task(asyncio.sleep(0))
        await client._reader_task
        await client.stop()

    asyncio.run(scenario())

    assert graceful_requests == []
    assert runner.terminated == [process]
    assert client._process is None


def test_lsp_file_read_failure_is_reported_before_request(tmp_path) -> None:
    client = LSPClient("unused", [], str(tmp_path))
    requested = False

    async def send_request(_method: str, _params: dict) -> object:
        nonlocal requested
        requested = True
        return []

    client._send_request = send_request  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="Unable to read source file for LSP"):
            await client.definition(str(tmp_path / "missing.py"), 0, 0)

    asyncio.run(scenario())
    assert requested is False


def test_lsp_parsers_skip_malformed_external_positions_and_symbols() -> None:
    locations = _parse_locations([
        {
            "uri": "file:///tmp/good.py",
            "range": {"start": {"line": 1, "character": 2}},
        },
        {
            "uri": "file:///tmp/bad.py",
            "range": {"start": {"line": "bad", "character": 0}},
        },
    ])
    assert len(locations) == 1
    assert locations[0].line == 1

    client = LSPClient("unused", [], ".")

    async def ensure_file_open(_path: str) -> None:
        return None

    client._ensure_file_open = ensure_file_open  # type: ignore[method-assign]

    async def send_request(_method: str, _params: dict) -> object:
        return [
            {"name": "good", "kind": 12, "range": {"start": {}, "end": {}}},
            {"name": "bad", "kind": "twelve", "range": {"start": {}, "end": {}}},
        ]

    client._send_request = send_request  # type: ignore[method-assign]

    async def scenario() -> list:
        return await client.document_symbols("sample.py")

    symbols = asyncio.run(scenario())
    assert [symbol.name for symbol in symbols] == ["good"]
