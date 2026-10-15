from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from backend.lsp.client import LSPClient
from backend.lsp.editor import EditorLanguageProcess, EditorLanguageServers, EditorLanguageStateError, editor_language_servers
from backend.workspace.api import create_workspace_router


class _LanguageClient:
    instances = []
    startup_gate = None
    request_gate = None
    request_entered = None

    def __init__(self, command, args, root, **kwargs):
        self.running = False
        self.root = root
        self.files = {}
        self.closed = []
        self.stop_count = 0
        self.capabilities = {}
        self._opened_files = {}
        self.instances.append(self)

    def is_running(self):
        return self.running

    async def start(self):
        if self.startup_gate is not None:
            await self.startup_gate.wait()
        self.running = True

    async def stop(self):
        self.stop_count += 1
        self.running = False
        self.files.clear()

    async def _send_notification(self, method, params):
        pass

    async def _sync_file(self, path, content, language):
        self.files[path] = content
        self._opened_files[path] = (1, content)

    async def close_file(self, path):
        self.closed.append(path)
        self.files.pop(path)
        self._opened_files.pop(path)

    async def _send_request(self, method, params):
        if self.request_gate is not None:
            self.request_entered.set()
            await self.request_gate.wait()
        return {"files": dict(self.files)}


@pytest.fixture
def language_servers(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    entry = bundle / "node_modules" / "pyright" / "langserver.index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// not launched by routing tests", encoding="utf-8")
    monkeypatch.setenv("MINICODE_EDITOR_LANGUAGE_SERVICES_DIR", str(bundle))
    monkeypatch.setenv("MINICODE_EDITOR_NODE", sys.executable)
    _LanguageClient.instances = []
    _LanguageClient.startup_gate = None
    _LanguageClient.request_gate = None
    _LanguageClient.request_entered = None
    monkeypatch.setattr("backend.lsp.editor.LSPClient", _LanguageClient)
    return EditorLanguageServers()


def document(path, identifier, content="value = 1", version=1):
    return {"path": str(path), "document_id": identifier, "model_version": version, "content": content, "language": "python"}


async def query(servers, root, documents, revision, *, instance="window-one", client="renderer-one"):
    return await servers.request(root, "python", "hover", documents, {"textDocument": {"uri": Path(documents[0]["path"]).as_uri()}},
        window_instance_id=instance, client_id=client, snapshot_revision=revision)


@pytest.mark.asyncio
async def test_document_close_preserves_hidden_models_and_rejects_late_snapshots(tmp_path, language_servers):
    servers = language_servers
    a, b, hidden = document(tmp_path / "a.py", "a-old", 'value = "discard"'), document(tmp_path / "b.py", "b"), document(tmp_path / "hidden.py", "hidden", "kept = 2")
    await query(servers, tmp_path, [b, a, hidden], 1)
    native = _LanguageClient.instances[0]
    await query(servers, tmp_path, [b], 2)
    assert native.files[a["path"]] == a["content"] and native.files[hidden["path"]] == hidden["content"]
    await servers.close_documents(window_instance_id="window-one", client_id="renderer-one", document_ids=["a-old"])
    assert a["path"] in native.closed and a["path"] not in native.files
    assert hidden["path"] in native.files and native.running
    with pytest.raises(EditorLanguageStateError, match="closed"):
        await query(servers, tmp_path, [b, a], 3)
    reopened = document(tmp_path / "a.py", "a-new", "value = 3")
    await query(servers, tmp_path, [b, reopened], 4)
    await servers.close_documents(window_instance_id="window-one", client_id="renderer-one", document_ids=["a-old"])
    assert native.files[a["path"]] == reopened["content"]
    await servers.close_documents(window_instance_id="window-one", client_id="renderer-one", document_ids=["b", "hidden", "a-new"])
    assert native.stop_count == 1 and not servers.clients


@pytest.mark.asyncio
async def test_two_renderers_keep_separate_unsaved_text_and_window_instances(tmp_path, language_servers):
    servers = language_servers
    path = tmp_path / "shared.py"
    await query(servers, tmp_path, [document(path, "model-a", "first = 1")], 1, instance="old-secondary", client="renderer-a")
    await query(servers, tmp_path, [document(path, "model-b", "second = 2")], 1, instance="new-secondary", client="renderer-b")
    old, new = _LanguageClient.instances
    assert old.files[str(path)] == "first = 1" and new.files[str(path)] == "second = 2"
    await servers.release_window("old-secondary")
    assert not old.running and new.running and new.files[str(path)] == "second = 2"
    with pytest.raises(EditorLanguageStateError, match="window"):
        await query(servers, tmp_path, [document(path, "late", "late = 3")], 3, instance="old-secondary", client="never-registered")
    await servers.release_client(window_instance_id="new-secondary", client_id="renderer-b")
    assert not new.running
    with pytest.raises(EditorLanguageStateError, match="released"):
        await query(servers, tmp_path, [document(path, "model-b")], 2, instance="new-secondary", client="renderer-b")


@pytest.mark.asyncio
async def test_disposal_fence_during_startup_prevents_did_open(tmp_path, language_servers):
    servers = language_servers
    gate = _LanguageClient.startup_gate = asyncio.Event()
    task = asyncio.create_task(query(servers, tmp_path, [document(tmp_path / "a.py", "disposed")], 1))
    await asyncio.sleep(0)
    assert _LanguageClient.instances
    closed = asyncio.create_task(servers.close_documents(window_instance_id="window-one", client_id="renderer-one", document_ids=["disposed"]))
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(EditorLanguageStateError, match="closed"):
        await task
    await closed
    assert not _LanguageClient.instances[0].files
    assert _LanguageClient.instances[0].stop_count == 1 and not servers.clients


@pytest.mark.asyncio
async def test_superseded_snapshot_cannot_roll_back_the_open_document(tmp_path, language_servers):
    servers = language_servers
    path = tmp_path / "a.py"
    await query(servers, tmp_path, [document(path, "model", "new = 2", version=2)], 2)
    with pytest.raises(EditorLanguageStateError, match="superseded"):
        await query(servers, tmp_path, [document(path, "model", "old = 1")], 1)
    assert _LanguageClient.instances[0].files[str(path)] == "new = 2"
    await servers.shutdown()


@pytest.mark.asyncio
async def test_disposal_cancels_ongoing_dependent_query_before_did_close(tmp_path, language_servers):
    servers = language_servers
    a, b = document(tmp_path / "a.py", "a"), document(tmp_path / "b.py", "b")
    await query(servers, tmp_path, [b, a], 1)
    _LanguageClient.request_gate = asyncio.Event()
    entered = _LanguageClient.request_entered = asyncio.Event()
    pending = asyncio.create_task(query(servers, tmp_path, [b, a], 2))
    await asyncio.wait_for(entered.wait(), timeout=1)
    await servers.close_documents(window_instance_id="window-one", client_id="renderer-one", document_ids=["a"])
    with pytest.raises(EditorLanguageStateError, match="closed"):
        await pending
    native = _LanguageClient.instances[0]
    assert a["path"] not in native.files and b["path"] in native.files and native.running
    _LanguageClient.request_gate = None
    await servers.shutdown()


@pytest.mark.asyncio
async def test_failed_process_release_retains_owned_process_and_reports_error(tmp_path, language_servers, monkeypatch):
    servers = language_servers
    await query(servers, tmp_path, [document(tmp_path / "a.py", "a")], 1)
    native = _LanguageClient.instances[0]
    actual_stop = native.stop

    async def unconfirmed():
        raise RuntimeError("termination unconfirmed")

    monkeypatch.setattr(native, "stop", unconfirmed)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await servers.release_client(window_instance_id="window-one", client_id="renderer-one")
    assert native in servers.clients.values() and servers.locks
    with pytest.raises(EditorLanguageStateError, match="released"):
        await query(servers, tmp_path, [document(tmp_path / "a.py", "a")], 2)
    monkeypatch.setattr(native, "stop", actual_stop)
    await servers.release_client(window_instance_id="window-one", client_id="renderer-one")
    assert not servers.clients and not servers.locks


@pytest.mark.asyncio
async def test_real_pyright_discard_restores_disk_semantics_via_rest_and_keeps_reader_open(tmp_path, monkeypatch):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("value = 1\n", encoding="utf-8")
    b.write_text("from a import value\nvalue\n", encoding="utf-8")
    trust_file = tmp_path / "trusted_workspaces.json"
    trust_file.write_text(json.dumps([str(tmp_path)]), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    app = FastAPI()
    app.include_router(create_workspace_router())
    bodies = {
        "window_owner_id": "primary", "window_instance_id": "real-pyright-window", "client_id": "real-pyright-renderer",
        "method": "hover", "line": 1, "character": 2,
    }
    doc_b = document(b, "b-model", b.read_text(encoding="utf-8"))
    doc_a = document(a, "a-model", 'value = "UNSAVED_AUDIT_TEXT"\n')
    url = "/api/workspace/language?workspace_root=" + str(tmp_path)
    headers = {"x-minicode-window-binding-token": "real-pyright-proof"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://editor.test") as client:
        try:
            opened = await client.post(url, json={**bodies, "snapshot_revision": 1, "documents": [doc_b, doc_a]}, headers=headers)
            assert opened.status_code == 200, opened.text
            assert "UNSAVED_AUDIT_TEXT" in opened.text
            hidden = await client.post(url, json={**bodies, "snapshot_revision": 2, "documents": [doc_b]}, headers=headers)
            assert hidden.status_code == 200 and "UNSAVED_AUDIT_TEXT" in hidden.text
            native = next(iter(editor_language_servers.clients.values()))
            process = native._process
            closed = await client.post("/api/workspace/language/lifecycle", json={**{key: bodies[key] for key in ("window_owner_id", "window_instance_id", "client_id")}, "action": "close", "document_ids": ["a-model"]}, headers=headers)
            assert closed.status_code == 200, closed.text
            after = await client.post(url, json={**bodies, "snapshot_revision": 3, "documents": [doc_b]}, headers=headers)
            assert after.status_code == 200, after.text
            assert "Literal[1]" in after.text and "UNSAVED_AUDIT_TEXT" not in after.text
            late = await client.post(url, json={**bodies, "snapshot_revision": 1, "documents": [doc_b, doc_a]}, headers=headers)
            assert late.status_code == 409 and late.json()["detail"]["code"] == "editor.document_stale"
            assert str(a) not in native._opened_files and process.returncode is None
            released = await client.post("/api/workspace/language/lifecycle", json={**{key: bodies[key] for key in ("window_owner_id", "window_instance_id", "client_id")}, "action": "release"}, headers=headers)
            assert released.status_code == 200, released.text
            assert process.returncode == 0
            assert not editor_language_servers.clients
            assert a.read_text(encoding="utf-8") == "value = 1\n" and b.read_text(encoding="utf-8") == doc_b["content"]
        finally:
            await editor_language_servers.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("ignores_exit", [False, True])
async def test_language_shutdown_waits_for_protocol_exit_and_reaps_an_unresponsive_peer(tmp_path, monkeypatch, ignores_exit):
    trace = tmp_path / "shutdown.jsonl"
    server = tmp_path / "language_server.py"
    server.write_text('''import json, sys, time
from pathlib import Path
trace = Path(sys.argv[1])
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"\\r\\n", b"\\n", b""):
            break
        name, value = line.decode().split(":", 1)
        headers[name.lower()] = value.strip()
    if not line:
        break
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    method = message.get("method")
    if method in ("shutdown", "exit"):
        with trace.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"method": method}) + "\\n")
    if "id" in message:
        result = {"capabilities": {"hoverProvider": True}} if method == "initialize" else None
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode() + body)
        sys.stdout.buffer.flush()
    if method == "exit":
        time.sleep(30 if sys.argv[2] == "ignore" else 0.05)
        break
''', encoding="utf-8")
    monkeypatch.setattr("backend.lsp.client._SHUTDOWN_EXIT_TIMEOUT_SECONDS", 0.2)
    runner = EditorLanguageProcess()
    client = LSPClient(sys.executable, [str(server), str(trace), "ignore" if ignores_exit else "exit"],
        str(tmp_path), sandbox_runner=runner)
    try:
        await client.start()
        process = runner.process
        await asyncio.wait_for(client.stop(), 5)
        assert [json.loads(line)["method"] for line in trace.read_text(encoding="utf-8").splitlines()] == ["shutdown", "exit"]
        assert process.returncode is not None
        assert (process.returncode != 0) if ignores_exit else (process.returncode == 0)
        assert client._process is None and client._reader_task is None and client._stderr_task is None
        assert client._pending == {}
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_native_editor_binding_rejects_cross_window_query_and_release_and_accepts_closed_main_cleanup(tmp_path, monkeypatch, language_servers):
    servers = language_servers
    monkeypatch.setattr("backend.lsp.editor.editor_language_servers", servers)
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://native-bridge.test")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "main-lifecycle-proof")
    trust_file = tmp_path / "trusted_workspaces.json"
    trust_file.write_text(json.dumps([str(tmp_path)]), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    app = FastAPI()
    app.include_router(create_workspace_router())
    live = {"native-a", "native-b"}
    from backend.workspace import editor_language as language_api
    authenticate = language_api.authenticate_editor_window

    async def observe_private_boundary(request, *, binding_token, desktop_lifecycle_token=""):
        assert "window_binding_token" not in request.model_dump()
        for credential in (binding_token, desktop_lifecycle_token):
            if credential:
                assert credential not in request.model_dump_json() and credential not in repr(request)
        await authenticate(request, binding_token=binding_token, desktop_lifecycle_token=desktop_lifecycle_token)

    monkeypatch.setattr(language_api, "authenticate_editor_window", observe_private_boundary)

    def bridge(request):
        assert request.url.path == "/v1/window-binding"
        assert request.headers["authorization"] == "Bearer main-lifecycle-proof"
        identity = json.loads(request.content)
        allowed = identity["window_instance_id"] in live and (
            identity["window_instance_id"], identity["window_owner_id"], identity["window_binding_token"]
        ) in {("native-a", "primary", "a-capability"), ("native-b", "secondary-1", "b-capability")}
        return httpx.Response(200 if allowed else 403, json={"ok": allowed})

    original_client = httpx.AsyncClient
    rest = original_client(transport=httpx.ASGITransport(app=app), base_url="http://editor.test")
    monkeypatch.setattr("backend.workspace.editor_language.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(bridge), **kwargs))
    a = {"window_owner_id": "primary", "window_instance_id": "native-a", "window_binding_token": "a-capability", "client_id": "renderer-a"}
    b = {"window_owner_id": "secondary-1", "window_instance_id": "native-b", "window_binding_token": "b-capability", "client_id": "renderer-b"}
    def body(identity):
        return {key: value for key, value in identity.items() if key != "window_binding_token"}
    def headers(identity):
        return {"x-minicode-window-binding-token": identity["window_binding_token"]}
    path = tmp_path / "shared.py"
    url = "/api/workspace/language?workspace_root=" + str(tmp_path)
    async with rest:
        for identity, content in [(a, "primary unsaved"), (b, "secondary unsaved")]:
            response = await rest.post(url, json={**body(identity), "method": "hover", "snapshot_revision": 1,
                "documents": [document(path, "shared-model", content)]}, headers=headers(identity))
            assert response.status_code == 200, response.text
        first, second = _LanguageClient.instances
        spoofed = {**a, "window_binding_token": b["window_binding_token"]}
        rejected = await rest.post(url, json={**body(spoofed), "method": "hover", "snapshot_revision": 2,
            "documents": [document(path, "shared-model", "forged replacement")]}, headers=headers(spoofed))
        assert rejected.status_code == 403
        for action in ("close", "release", "release_window"):
            rejected = await rest.post("/api/workspace/language/lifecycle", json={**body(spoofed),
                "action": action, "document_ids": ["shared-model"]}, headers=headers(spoofed))
            assert rejected.status_code == 403
        assert first.files[str(path)] == "primary unsaved" and first.running
        assert second.files[str(path)] == "secondary unsaved" and second.running
        malformed = await rest.post("/api/workspace/language/lifecycle", json={**body(a), "action": "invalid-action"}, headers=headers(a))
        assert malformed.status_code == 422 and "a-capability" not in malformed.text
        live.remove("native-a")
        cleaned = await rest.post("/api/workspace/language/lifecycle", json={"action": "release_window",
            "window_owner_id": a["window_owner_id"], "window_instance_id": a["window_instance_id"]},
            headers={"x-minicode-desktop-lifecycle-token": "main-lifecycle-proof"})
        assert cleaned.status_code == 200, cleaned.text
        assert not first.running and second.running and "native-a" in servers.closed_windows
        late = await rest.post(url, json={**body(a), "method": "hover", "snapshot_revision": 3,
            "documents": [document(path, "shared-model", "late replacement")]}, headers=headers(a))
        assert late.status_code == 403 and not first.running
    await servers.shutdown()


@pytest.mark.asyncio
async def test_browser_editor_binding_proves_live_owner_and_closed_window_cannot_revive(tmp_path, monkeypatch, language_servers):
    servers = language_servers
    monkeypatch.setattr("backend.lsp.editor.editor_language_servers", servers)
    monkeypatch.delenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", raising=False)
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "private-main-release")
    trust_file = tmp_path / "trusted_workspaces.json"
    trust_file.write_text(json.dumps([str(tmp_path)]), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    app = FastAPI()
    app.include_router(create_workspace_router())
    identity = {"window_owner_id": "browser-owner", "window_instance_id": "browser-instance", "client_id": "browser-client"}
    headers = {"x-minicode-window-binding-token": "browser-private-proof"}
    request = {**identity, "method": "hover", "snapshot_revision": 1, "documents": [document(tmp_path / "app.py", "app")]}
    url = "/api/workspace/language?workspace_root=" + str(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://editor.test") as client:
        assert (await client.post(url, json=request, headers=headers)).status_code == 200
        denied = await client.post("/api/workspace/language/lifecycle", json={**identity,
            "action": "release"}, headers={"x-minicode-window-binding-token": "another-browser-proof"})
        assert denied.status_code == 403 and _LanguageClient.instances[0].running
        denied = await client.post("/api/workspace/language/lifecycle", json={"action": "release_window",
            "window_owner_id": identity["window_owner_id"], "window_instance_id": identity["window_instance_id"]},
            headers={"x-minicode-desktop-lifecycle-token": "renderer-token"})
        assert denied.status_code == 403
        accepted = await client.post("/api/workspace/language/lifecycle", json={**identity, "action": "release_window"}, headers=headers)
        assert accepted.status_code == 200 and not _LanguageClient.instances[0].running
        late = await client.post(url, json={**request, "snapshot_revision": 2}, headers=headers)
        assert late.status_code == 409 and late.json()["detail"]["code"] == "editor.document_stale"
    await servers.shutdown()
