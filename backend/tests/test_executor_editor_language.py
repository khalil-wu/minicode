"""Real target language servers over an owned Source exec-server transport."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.lsp.editor import EditorLanguageProcess, editor_language_servers
from backend.lsp.executor_editor import ExecutorEditorLSPClient, ExecutorLanguageServer
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_paths import ExecutorPath


@pytest_asyncio.fixture
async def language_editor(editor_server, monkeypatch):
    async def no_controller(*args, **kwargs):
        raise AssertionError("Executor LSP must not launch a controller language server/compiler")
    monkeypatch.setattr(EditorLanguageProcess, "spawn_interactive", no_controller)
    monkeypatch.setattr("backend.lsp.editor.cpp_compiler_configuration", no_controller)
    try:
        yield editor_server
    finally:
        await editor_language_servers.shutdown()


def configure_language(server, language, argv, **options):
    directory = server.host.workspace / ".minicode"
    directory.mkdir(exist_ok=True)
    (directory / "language-servers.json").write_text(json.dumps({"language_servers": {
        language: {"argv": argv, **options}}}), encoding="utf-8")


def model(path, text, *, language="python", version=1):
    return {"path": path, "content": text, "language": language, "document_id": "owned-model-" + path,
        "model_version": version}


async def query(server, method, documents, revision, **fields):
    response = await server.client.post("/api/workspace/language", params=server.params,
        headers={"x-minicode-window-binding-token": "owned-editor-binding"}, json={
            "window_owner_id": "owned-editor-owner", "window_instance_id": "owned-editor-window",
            "client_id": "owned-language-client", "snapshot_revision": revision,
            "method": method, "documents": documents, **fields})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["environment_id"] == server.params["environment_id"]
    assert payload["path_uri"] == server.host.workspace.joinpath(documents[0]["path"]).as_uri()
    return payload


def target_node_server(package, entry):
    # This explicitly owned Source peer runs on the test machine. These paths
    # identify its actual installed programs; production never discovers them.
    root = Path(__file__).resolve().parents[2]
    return [shutil.which("node"), str(root / "desktop/language-services/node_modules" / package / entry), "--stdio"]


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_target_pyright_uses_dirty_cross_file_documents_and_original_uri(language_editor):
    server = language_editor
    configure_language(server, "python", target_node_server("pyright", "langserver.index.js"))
    files = {"definitions.py": b"disk_only = 1\r\n", "main.py": b"disk_main = True\r\n"}
    for name, value in files.items():
        (server.host.workspace / name).write_bytes(value)
        (server.view / name).write_text("controller_shadow = 'must never be opened'", encoding="utf-8")
    definitions = model("definitions.py", "def target_only(value: int) -> int:\n    return value + 1\n", version=7)
    main = model("main.py", 'from definitions import target_only\nanswer = target_only(41)\nwrong: int = "dirty type"\n', version=11)
    hover = await query(server, "hover", [main, definitions], 1, line=1, character=13)
    assert "target_only" in json.dumps(hover["result"]) and "int" in json.dumps(hover["result"])
    definition = (await query(server, "definition", [main, definitions], 2, line=1, character=13))["result"]
    locations = definition if isinstance(definition, list) else [definition]
    assert any(ExecutorPath.parse(item.get("uri") or item["targetUri"]) == ExecutorPath.parse((server.host.workspace / "definitions.py").as_uri()) for item in locations)
    references = (await query(server, "references", [definitions, main], 3, line=0, character=7))["result"]
    assert len(references) >= 2 and any(item["uri"].endswith("/main.py") for item in references)
    renamed = (await query(server, "rename", [main, definitions], 4, line=1, character=13, new_name="renamed_dirty"))["result"]
    assert "renamed_dirty" in json.dumps(renamed) and "definitions.py" in json.dumps(renamed)
    symbols = (await query(server, "documentSymbol", [definitions, main], 5))["result"]
    assert any(item["name"] == "target_only" for item in symbols)
    completion = (await query(server, "completion", [main, definitions], 6, line=1, character=15))["result"]
    items = completion["items"] if isinstance(completion, dict) else completion
    assert any(item["label"] == "target_only" for item in items)
    signatures = (await query(server, "signatureHelp", [main, definitions], 7, line=1, character=22))["result"]
    assert signatures["signatures"]
    diagnostics = (await query(server, "diagnostics", [main, definitions], 8))["result"]
    assert any(item["range"]["start"]["line"] == 2 and item.get("code") == "reportAssignmentType"
        for item in diagnostics), diagnostics
    client, = editor_language_servers.clients.values()
    assert client._opened_files[(server.host.workspace / "main.py").as_uri()][0] == 11
    assert client._opened_files[(server.host.workspace / "definitions.py").as_uri()][0] == 7
    stale = await server.client.post("/api/workspace/language", params=server.params,
        headers={"x-minicode-window-binding-token": "owned-editor-binding"}, json={
            "window_owner_id": "owned-editor-owner", "window_instance_id": "owned-editor-window",
            "client_id": "owned-language-client", "snapshot_revision": 9, "method": "hover",
            "documents": [{**main, "model_version": 10}]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "editor.document_stale"
    assert client._opened_files[(server.host.workspace / "main.py").as_uri()][0] == 11
    old_runner = client._sandbox_runner
    configure_language(server, "python", target_node_server("pyright", "langserver.index.js"), settings={
        "python": {"analysis": {"diagnosticMode": "openFilesOnly", "typeCheckingMode": "basic",
            "diagnosticSeverityOverrides": {"reportAssignmentType": "none"}}}})
    configured = (await query(server, "diagnostics", [main, definitions], 10))["result"]
    assert not any(item.get("code") == "reportAssignmentType" for item in configured), configured
    replacement_client, = editor_language_servers.clients.values()
    assert replacement_client is not client and old_runner.handle.closed and old_runner.lease.released
    for name, value in files.items():
        assert (server.host.workspace / name).read_bytes() == value
        assert "controller_shadow" in (server.view / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_target_yaml_formatting_is_an_lsp_result_and_never_saves_the_dirty_buffer(language_editor):
    server = language_editor
    configure_language(server, "yaml", target_node_server("yaml-language-server", "bin/yaml-language-server"))
    target = server.host.workspace / "unsaved.yaml"
    target.write_bytes(b"disk: unchanged\r\n")
    result = await query(server, "formatting", [model("unsaved.yaml", "target:    [1,2]\n", language="yaml", version=4)], 1)
    assert result["capabilities"]["documentFormattingProvider"]
    assert result["result"] and "target" in json.dumps(result["result"])
    assert target.read_bytes() == b"disk: unchanged\r\n"


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_target_clangd_uses_configured_compiler_flags_and_dirty_header(language_editor):
    server = language_editor
    root = Path(__file__).resolve().parents[2]
    bundled = root / "desktop/language-services/clangd/clangd_23.1.0/bin/clangd.exe"
    command = str(bundled) if sys.platform == "win32" else shutil.which("clangd")
    configure_language(server, "cpp", [command, "--sync", "--use-dirty-headers"], initialization_options={"fallbackFlags": ["-xc++", "-std=c++20", "-DTARGET_CONFIGURATION=42"]})
    (server.host.workspace / "main.cpp").write_bytes(b"int disk_only;\r\n")
    (server.host.workspace / "target.hpp").write_text("int disk_header;\n", encoding="utf-8")
    header = model("target.hpp", "struct TargetType { int dirty_member; };\n", language="cpp", version=3)
    source = model("main.cpp", '#include "target.hpp"\nTargetType object;\nint answer = TARGET_CONFIGURATION;\n', language="cpp", version=5)
    result = await query(server, "hover", [source, header], 1, line=1, character=3)
    diagnostics = (await query(server, "diagnostics", [source, header], 2))["result"]
    assert "TargetType" in json.dumps(result["result"]), diagnostics
    assert not any("TARGET_CONFIGURATION" in item["message"] for item in diagnostics)
    assert (server.host.workspace / "main.cpp").read_bytes() == b"int disk_only;\r\n"


PROTOCOL_SERVER = r'''import json,sys
from pathlib import Path
trace=Path(sys.argv[1])
def send(message):
 body=json.dumps(message).encode()
 sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode()+body)
 sys.stdout.buffer.flush()
while True:
 headers={}
 while True:
  line=sys.stdin.buffer.readline()
  if line in (b"\r\n",b"\n",b""):break
  key,value=line.decode().split(":",1);headers[key.lower()]=value.strip()
 if not line:break
 message=json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
 with trace.open("a",encoding="utf-8") as output:output.write(json.dumps(message)+"\n")
 method=message.get("method")
 if method=="initialize":send({"jsonrpc":"2.0","id":message["id"],"result":{"capabilities":{"hoverProvider":True}}})
 elif method=="shutdown":send({"jsonrpc":"2.0","id":message["id"],"result":None})
 elif method=="exit":break
'''


async def trace_contains(path, method):
    async with asyncio.timeout(10):
        while True:
            if path.exists():
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                matches = [row for row in rows if row.get("method") == method]
                if matches:
                    return matches[-1]
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_http_disconnect_cancels_original_request_and_registration_replacement_keeps_original_lease(language_editor):
    server = language_editor
    script, trace = server.host.workspace / "owned_lsp.py", server.host.workspace / "lsp_trace.jsonl"
    script.write_text(PROTOCOL_SERVER, encoding="utf-8")
    configure_language(server, "python", [sys.executable, "-I", "-S", str(script), str(trace)])
    target = model("dirty.py", "dirty_value = 42\n", version=12)
    pending = asyncio.create_task(query(server, "hover", [target], 1))
    request = await trace_contains(trace, "textDocument/hover")
    client, = editor_language_servers.clients.values()
    runner = client._sandbox_runner
    original, handle = runner.environment, runner.handle
    assert (await trace_contains(trace, "initialize"))["params"]["processId"] is None
    assert (await trace_contains(trace, "textDocument/didOpen"))["params"]["textDocument"]["version"] == 12
    replacement = server.host.manager.upsert({"id": "target", "program": "owned-replacement-must-not-launch"})
    assert replacement is not original and original.retired and not runner.lease.released
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    cancelled = await trace_contains(trace, "$/cancelRequest")
    assert cancelled["params"]["id"] == request["id"]
    assert runner.environment is original and handle.client is original.client
    response = await server.client.post("/api/workspace/language/lifecycle",
        headers={"x-minicode-window-binding-token": "owned-editor-binding"}, json={
            "action": "close", "window_owner_id": "owned-editor-owner", "window_instance_id": "owned-editor-window",
            "client_id": "owned-language-client", "document_ids": [target["document_id"]]})
    assert response.status_code == 200, response.text
    assert handle.closed and handle.released and runner.lease.released
    assert await trace_contains(trace, "textDocument/didClose")
    assert await trace_contains(trace, "shutdown")
    assert not replacement.client.processes and replacement.client.connect_task is None


def test_executor_uri_domain_is_case_sensitive_for_posix_and_never_uses_controller_path_resolution():
    class Lease:
        environment = object()
        def clone(self): return self
    policy = SimpleNamespace(inherit="none", ignore_default_excludes=False, exclude=(), set_values={}, include_only=())
    service = SimpleNamespace(lease=Lease(), cwd=ExecutorPath.parse("file:///remote/Case%20Dir", platform_os="linux"),
        scope=SimpleNamespace(platform_os="linux", process_sandbox_context=lambda: {}),
        config=SimpleNamespace(shell_environment_policy=policy))
    client = ExecutorEditorLSPClient(service, ExecutorLanguageServer(argv=["target-lsp"]))
    uri = "file:///remote/Case%20Dir/File.py"
    assert client._path_to_uri(uri) == uri and client._normalize_file_path(uri) == uri
    assert client._file_key(uri) != client._file_key(uri.lower())
    assert client._workspace_name() == "Case Dir" and client._client_process_id() is None
