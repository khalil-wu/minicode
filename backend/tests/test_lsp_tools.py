from __future__ import annotations

import asyncio
import json
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


def test_lsp_legacy_default_is_readonly_and_offline(tmp_path) -> None:
    runner = _lsp_sandbox_runner(str(tmp_path))

    assert runner._policy.workspace_root == tmp_path.resolve()
    assert runner._policy.allow_network is False
    assert runner._policy.disable_os_sandbox is False
    assert runner._policy.resolve(cwd=tmp_path).resolve_access(tmp_path / "source.py").value == "read"


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
    monkeypatch.chdir(tmp_path)
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

        def is_available(self, file_path: str, workspace_root: str, *, sandbox_policy=None) -> bool:
            return True

        async def get_client(self, file_path: str, workspace_root: str, *, sandbox_policy=None) -> _Client:
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
    monkeypatch.chdir(tmp_path)
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

        def is_available(self, file_path: str, workspace_root: str, *, sandbox_policy=None) -> bool:
            return True

        async def get_client(self, file_path: str, workspace_root: str, *, sandbox_policy=None) -> _Client:
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
        def __init__(self, process: object) -> None:
            self.process = process
            self.terminated: list[object] = []

        async def cleanup(self) -> bool:
            self.terminated.append(self.process)
            self.process = None
            return True

    process = _Process()
    runner = _Runner(process)
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


@pytest.mark.parametrize("server_id,method", [
    (1, "workspace/configuration"),
    ("server-request", "workspace/configuration"),
    (1, "workspace/unsupported"),
])
def test_lsp_server_requests_do_not_consume_client_response_ids(server_id, method) -> None:
    async def scenario() -> None:
        client = LSPClient("unused", [], ".")
        client._process = SimpleNamespace(returncode=None)
        stream = asyncio.StreamReader()
        client._stdout = stream
        sent: list[dict] = []
        written = asyncio.Event()

        class Writer:
            def write(self, data: bytes) -> None:
                header, body = data.split(b"\r\n\r\n", 1)
                assert int(header.split(b":", 1)[1]) == len(body)
                sent.append(json.loads(body))
                written.set()

            async def drain(self) -> None:
                pass

        client._stdin = Writer()
        read_task = asyncio.create_task(client._read_loop())
        query = asyncio.create_task(client._send_request("textDocument/hover", {}))
        await written.wait()
        query_id = sent[0]["id"]
        actual_result = {"contents": "Actual type information"}
        for message in [
            {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3, "message": "Indexing"}},
            {"jsonrpc": "2.0", "id": server_id, "method": method, "params": {"items": [{"section": "python"}, {"section": "typescript"}]}},
            {"jsonrpc": "2.0", "id": query_id, "result": actual_result},
        ]:
            body = json.dumps(message).encode("utf-8")
            frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            stream.feed_data(frame[:17])
            await asyncio.sleep(0)
            stream.feed_data(frame[17:])
        stream.feed_eof()

        assert await query == actual_result
        await read_task
        assert len(sent) == 2
        assert sent[1]["id"] == server_id
        if method == "workspace/configuration":
            assert sent[1]["result"] == [None, None]
        else:
            assert sent[1]["error"]["code"] == -32601
        assert client._pending == {}

    asyncio.run(scenario())


def test_runtime_review_lsp_all_callers_carry_same_captured_policy(monkeypatch, tmp_path):
    import asyncio
    import json
    from backend.tools import lsp_tools as module
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox.policy import SandboxPolicy

    narrow = SandboxPolicy(workspace_root=tmp_path, writable_roots=(), denied_roots=(tmp_path / "denied",), allow_network=False)
    broad = SandboxPolicy(workspace_root=tmp_path, writable_roots=(), allow_network=True)
    class Client:
        async def definition(self, *args): return []
        async def references(self, *args): return []
        async def hover(self, *args): return None
        async def document_symbols(self, *args): return []
    class ManagerContractSpy:
        def __init__(self): self.calls = []
        def is_available(self, file_path, workspace_root, *, sandbox_policy: SandboxPolicy):
            self.calls.append(("capability", file_path, workspace_root, sandbox_policy))
            return True
        async def get_client(self, file_path, workspace_root, *, sandbox_policy: SandboxPolicy):
            self.calls.append(("client", file_path, workspace_root, sandbox_policy))
            return Client()
    manager = ManagerContractSpy()
    monkeypatch.setattr(module, "get_lsp_manager", lambda: manager)
    async def verify():
        for tool_class in (module.LSPGoToDefinitionTool, module.LSPFindReferencesTool, module.LSPHoverTool, module.LSPDocumentSymbolsTool):
            for policy in (broad, narrow):
                context = ToolExecutionContext(permission=PermissionContext(mode="confirm"), workspace_root=tmp_path, sandbox_policy=policy, allow_network=not policy.allow_network)
                args = {"file_path": "source.py", "line": 0, "character": 0}
                result = await tool_class().execute(args, context)
                assert not result.is_error
                capability, client = manager.calls[-2:]
                assert capability[0] == "capability" and client[0] == "client"
                assert capability[3] is policy and client[3] is policy
                assert capability[1:3] == client[1:3]
        assert manager.calls[-1][3].resolve(cwd=tmp_path).resolve_access(tmp_path / "denied" / "nonsecret.txt").value == "deny"
        print(json.dumps({"oracle": "lsp_caller_contract_unit", "all_four_callers": True, "capability_and_client_same_captured_policy": True, "legacy_network_flag_did_not_override_snapshot": True, "actual_lsp_server_or_cache_execution": False}))
    asyncio.run(verify())


def test_runtime_review_lsp_standalone_uses_existing_canonical_constructor(monkeypatch, tmp_path):
    from unittest.mock import patch
    from backend.tools import lsp_tools as module
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox.policy import SandboxPolicy
    permission = PermissionContext(mode="confirm", sandbox_mode="read-only", workspace_root=tmp_path)
    context = ToolExecutionContext(permission=permission, workspace_root=tmp_path)
    with patch.object(module, "sandbox_policy_for_permission_context", wraps=module.sandbox_policy_for_permission_context) as canonical:
        policy = module._lsp_request_policy(str(tmp_path), context)
        canonical.assert_called_once_with(tmp_path, permission)
        assert isinstance(policy, SandboxPolicy)
    with patch.object(module, "sandbox_policy_for_permission_context", wraps=module.sandbox_policy_for_permission_context) as canonical:
        policy = module._lsp_request_policy(str(tmp_path), None)
        assert policy is None
        canonical.assert_not_called()


def test_runtime_review_actual_lsp_manager_cache_separates_authority(monkeypatch, tmp_path):
    import asyncio
    import json
    import sys
    from dataclasses import replace
    from types import SimpleNamespace
    from backend.lsp import client as module
    from backend.tools import lsp_tools as callers
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox.policy import SandboxPolicy

    created = []
    class ProcessContractStub:
        def __init__(self, command, args, workspace_root, *, server_name, sandbox_runner):
            self.runner = sandbox_runner
            self.running = False
            self.closed_files = []
            created.append(self)
        async def start(self): self.running = True
        async def stop(self): self.running = False
        def is_running(self): return self.running
        async def document_symbols(self, file_path): return []
        async def close_file(self, file_path): self.closed_files.append(file_path)
    monkeypatch.setattr(module, "_resolve_server_executable", lambda server, root: sys.executable)
    monkeypatch.setattr(module.SandboxRunner, "capability", lambda self: SimpleNamespace(available=True, reason="cache-unit-only"))
    monkeypatch.setattr(module, "LSPClient", ProcessContractStub)
    manager = module.LSPManager()
    monkeypatch.setattr(callers, "get_lsp_manager", lambda: manager)
    broad = SandboxPolicy(workspace_root=tmp_path, writable_roots=(), allow_network=True)
    offline = SandboxPolicy(workspace_root=tmp_path, writable_roots=(), allow_network=False)
    narrow = SandboxPolicy(workspace_root=tmp_path, writable_roots=(), denied_roots=(tmp_path / "denied",), allow_network=False)
    env_changed = replace(narrow, env_overrides={"REVIEW_LABEL": "different"})
    async def verify():
        tool = callers.LSPDocumentSymbolsTool()
        for policy in (broad, offline, narrow, env_changed):
            context = ToolExecutionContext(permission=PermissionContext(mode="confirm"), workspace_root=tmp_path, sandbox_policy=policy)
            result = await tool.execute({"file_path": "source.py"}, context)
            assert not result.is_error
            assert created[-1].runner._policy.resolve(cwd=tmp_path) == policy.resolve(cwd=tmp_path)
        assert len(created) == len(manager._clients) == 4
        expected = created[2]
        same_authority = replace(narrow, timeout=99)
        assert await manager.get_client(str(tmp_path / "source.py"), str(tmp_path), sandbox_policy=same_authority) is expected
        assert len(created) == 4
        await manager.close_file(str(tmp_path / "source.py"), str(tmp_path))
        assert all(c.closed_files == [str(tmp_path / "source.py")] for c in created)
        await manager.shutdown_all()
        assert manager._clients == {} and all(not c.running for c in created)
        print(json.dumps({"oracle": "actual_caller_and_manager_cache_unit", "actual_production_cache_executed": True, "network_deny_and_env_each_separate": True, "same_authority_reuses": True, "close_file_all_authorities": True, "actual_sandbox_server_started": False}))
    asyncio.run(verify())
