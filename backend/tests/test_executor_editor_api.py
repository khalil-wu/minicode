"""Real HTTP/WebSocket ingress with an actual session and local/Source executor."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import socket
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import httpx
import pytest
import pytest_asyncio
from starlette.requests import Request
import uvicorn
from websockets.asyncio.client import connect

from backend.api import _state
from backend.api.auth import _workspace_raw_token_signature, _is_workspace_raw_token_authorized
from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.services.workspace_api_service import capture_workspace_execution
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.api import create_workspace_router
from backend.workspace.executor_service import ExecutorWorkspaceService
from backend.workspace.trust import trust_created_task_directory
from backend.ws.handler import WebSocketSession
from backend.ws.manager import WebSocketManager


@pytest_asyncio.fixture
async def editor_server(executor_host, tmp_path, monkeypatch):
    host = executor_host
    view_root = tmp_path / "controller-view"
    view_root.mkdir()
    (view_root / "same.txt").write_text("controller must remain untouched\n", encoding="utf-8")
    (host.workspace / "same.txt").write_bytes(b"actual executor\r\n")
    trust_created_task_directory(host.workspace)
    trust_created_task_directory(view_root)
    manager = WebSocketManager()
    monkeypatch.setattr(_state, "ws_manager", manager)
    app = FastAPI()
    app.include_router(create_workspace_router())
    started = asyncio.Event()
    holder = SimpleNamespace(host=host, view=view_root, manager=manager)

    @app.websocket("/owned-editor")
    async def owned_socket(websocket: WebSocket):
        await websocket.accept()
        owner = WebSocketSession("editor-session", websocket, None, ArtifactStore(storage_dir=tmp_path / "editor-artifacts"),
            host.registry, PermissionChecker(PermissionSettings(), view_root), host.context.model_execution.config,
            mcp_manager=None, environment_manager=host.manager, ws_manager=manager,
            window_instance_id="owned-editor-window", window_owner_id="owned-editor-owner", window_binding_token="owned-editor-binding")
        holder.session = owner
        conversation = owner.conversation_repo.create_conversation(conversation_id="editor-conversation", title="Owned editor",
            workspace_root=str(view_root), execution_cwd=str(view_root), permission_mode="bypass")
        owner.active_conversation_id = conversation.id
        owner.session_lifecycle.workspace_root = view_root
        manager._sessions[owner.session_id] = owner
        runtime = manager.execution_contexts.thread_runtime(owner.conversation_repo.store_instance_id(), conversation.id)
        runtime.thread_environments = host.owner
        holder.runtime = runtime
        started.set()
        try:
            await owner.command_dispatcher.run(owner.connection_generation)
        except WebSocketDisconnect:
            pass
        finally:
            await owner.session_lifecycle.shutdown(reason="owned-editor-test")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                await asyncio.sleep(0.01)
        async with connect(f"ws://127.0.0.1:{port}/owned-editor") as websocket:
            await asyncio.wait_for(started.wait(), 10)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                holder.client, holder.websocket = client, websocket
                holder.params = {"workspace_root": str(view_root), "session_id": holder.session.session_id,
                    "conversation_id": holder.session.active_conversation_id, "environment_id": host.environments.primary().selection.environment_id}
                yield holder
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 15)
        await manager.shutdown(reason="owned-editor-fixture")


@pytest.mark.asyncio
async def test_http_editor_bytes_hash_rename_preview_and_dirty_search_use_accepted_target(editor_server, monkeypatch):
    server = editor_server
    read = await server.client.get("/api/workspace/file", params={**server.params, "path": "same.txt"})
    assert read.status_code == 200, read.text
    file = read.json()
    assert file["content"] == "actual executor\r\n" and file["path_uri"] == (server.host.workspace / "same.txt").as_uri()
    assert file["environment_id"] == server.params["environment_id"]
    stale = await server.client.put("/api/workspace/file/compare-write", params=server.params,
        json={"path": file["path_uri"], "expected_hash": "stale", "content": "wrong"})
    assert stale.status_code == 409
    saved = await server.client.put("/api/workspace/file/compare-write", params=server.params,
        json={"path": file["path_uri"], "expected_hash": file["content_hash"], "content": "saved executor\r\n"})
    assert saved.status_code == 200, saved.text
    raw = await server.client.get("/api/workspace/raw", params={**server.params, "path": file["path_uri"]})
    assert raw.content == b"saved executor\r\n"
    preview = await server.client.get("/api/workspace/preview", params={**server.params, "path": "same.txt"})
    assert preview.status_code == 200 and "saved executor" in preview.json()["content"], preview.text
    completed = []
    class CompletionAdapter:
        async def side_query(self, messages, *, options, turn_context):
            assert options.operation == "editor_inline_complete"
            body = json.loads(messages[-1].content)
            assert body["file"] == "same.txt" and body["prefix"] == "unsaved prefix"
            return "owned completion"
        async def aclose(self):
            completed.append("closed")
    def completion_factory(config, *_args, **_kwargs):
        assert config is server.session.config
        return CompletionAdapter()
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", completion_factory)
    inline = await server.client.post("/api/workspace/inline-completion", params=server.params,
        json={"path": file["path_uri"], "prefix": "unsaved prefix", "suffix": "unchanged suffix"})
    assert inline.status_code == 200 and inline.json()["text"] == "owned completion", inline.text
    assert inline.json()["path_uri"] == file["path_uri"] and inline.json()["environment_id"] == server.params["environment_id"]
    assert completed == ["closed"]
    (server.host.workspace / ".minicode").mkdir()
    (server.host.workspace / ".minicode/launch.json").write_text('{"configurations": []}', encoding="utf-8")
    launch_tree = await server.client.get("/api/workspace/tree", params={**server.params, "path": ".minicode"})
    assert launch_tree.status_code == 200 and launch_tree.json()["entries"][0]["name"] == "launch.json", launch_tree.text
    launch_file = await server.client.get("/api/workspace/file", params={**server.params, "path": ".minicode/launch.json"})
    assert launch_file.status_code == 200, launch_file.text
    search = await server.client.post("/api/workspace/search-content", params=server.params, json={"query": "unsaved", "buffers": [
        {"path": "same.txt", "content": "unsaved draft", "original": "saved executor\r\n", "content_hash": saved.json()["content_hash"]}]})
    assert search.status_code == 200 and search.json()["files"][0]["from_buffer"], search.text
    assert (server.host.workspace / "same.txt").read_bytes() == b"saved executor\r\n"
    created = await server.client.post("/api/workspace/directory", params=server.params, json={"path": "new-folder"})
    assert created.status_code == 200, created.text
    moved = await server.client.post("/api/workspace/rename", params=server.params, json={"path": "same.txt", "new_path": "new-folder/moved.txt"})
    assert moved.status_code == 200, moved.text
    tree = await server.client.get("/api/workspace/tree", params={**server.params, "path": "new-folder"})
    assert tree.status_code == 200 and tree.json()["entries"][0]["path_uri"] == (server.host.workspace / "new-folder/moved.txt").as_uri(), tree.text
    assert (server.view / "same.txt").read_text() == "controller must remain untouched\n"
    removed = await server.client.delete("/api/workspace/path", params={**server.params, "path": "new-folder", "recursive": True})
    assert removed.status_code == 200 and not (server.host.workspace / "new-folder").exists(), removed.text


@pytest.mark.asyncio
async def test_http_index_git_and_stale_owner_do_not_read_the_controller(editor_server):
    server = editor_server
    (server.host.workspace / "module.ts").write_text("export const actual = true;\n", encoding="utf-8")
    indexed = await server.client.get("/api/workspace/project-index", params=server.params)
    assert indexed.status_code == 200, indexed.text
    assert any(file["path"] == "module.ts" and "actual" in file["content"] for file in indexed.json()["files"])
    async with capture_workspace_execution(server.session, server.params["conversation_id"], environment_id=server.params["environment_id"]) as context:
        async with ExecutorWorkspaceService(context) as service:
            result = await service.run(["git", "init"])
            assert result.returncode == 0, result.stderr
    status = await server.client.get("/api/workspace/git/status", params=server.params)
    assert status.status_code == 200 and "module.ts" in status.json()["untracked"], status.text
    patch = await server.client.get("/api/workspace/git/diff", params={**server.params, "file": "module.ts"})
    assert patch.status_code == 200 and "+export const actual" in patch.json()["diff"], patch.text
    inventory = await server.client.get("/api/workspace/git/worktree", params=server.params)
    assert inventory.status_code == 200 and inventory.json()["current_path"] == (
        server.host.workspace.as_uri() if server.host.kind == "source" else str(server.host.workspace)), inventory.text
    assert inventory.json()["environment_id"] == server.params["environment_id"]
    assert inventory.json()["worktrees"][0]["path_uri"] == server.host.workspace.as_uri()
    old = server.session.active_conversation_id
    server.session.active_conversation_id = "another-conversation"
    stale = await server.client.put("/api/workspace/file/compare-write", params=server.params,
        json={"path": "same.txt", "expected_hash": "", "content": "must not write"})
    assert stale.status_code == 409 and "owner is stale" in stale.text
    server.session.active_conversation_id = old
    await server.host.owner.update_selections(())
    empty = await server.client.get("/api/workspace/file", params={**{key: value for key, value in server.params.items() if key != "environment_id"}, "path": "same.txt"})
    assert empty.status_code == 409 and "No usable execution environment" in empty.text, empty.text
    original_manager = server.session.environment_manager
    server.session.environment_manager = None
    try:
        missing_manager = await server.client.get("/api/workspace/file", params={**server.params, "path": "same.txt"})
        assert missing_manager.status_code == 503 and "No usable execution environment" in missing_manager.text, missing_manager.text
    finally:
        server.session.environment_manager = original_manager
    assert (server.view / "same.txt").read_text() == "controller must remain untouched\n"


@pytest.mark.asyncio
async def test_real_ws_checkpoint_list_and_rewind_use_selected_environment_uri(editor_server):
    server = editor_server
    target = server.host.workspace / "same.txt"
    async with capture_workspace_execution(server.session, server.params["conversation_id"], environment_id=server.params["environment_id"]) as context:
        record = await server.session.checkpoint_manager.snapshot(tool_name="write_file", args={"file_path": "same.txt"},
            workspace_root=server.view, conversation_id=server.params["conversation_id"], execution_context=context)
    target.write_bytes(b"changed on executor\r\n")
    async def request(kind, expected, **fields):
        await server.websocket.send(json.dumps({"type": kind, "conversation_id": server.params["conversation_id"],
            "workspace_root": str(server.view), "environment_id": server.params["environment_id"],
            "cwd_uri": server.host.workspace.as_uri(), **fields}))
        async with asyncio.timeout(10):
            while True:
                item = json.loads(await server.websocket.recv())
                if item["type"] == "command.result" and item.get("level") == "error":
                    pytest.fail(str(item))
                if item["type"] == expected:
                    return item
    listed = await request("checkpoint.list", "checkpoint.list")
    assert listed["checkpoints"][0]["metadata"]["environment_id"] == server.params["environment_id"]
    rewound = await request("checkpoint.rewind", "checkpoint.rewound", checkpoint_id=record.id)
    assert rewound["checkpoint"]["metadata"]["cwd_uri"] == server.host.workspace.as_uri()
    assert target.read_bytes() == b"actual executor\r\n"
    assert (server.view / "same.txt").read_text() == "controller must remain untouched\n"


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_independent_executor_reports_missing_lsp_transport_without_controller_server(editor_server, monkeypatch):
    from backend.lsp.editor import editor_language_servers
    async def no_controller_server(*args, **kwargs):
        raise AssertionError("An independent executor document cannot enter the controller LSP client")
    monkeypatch.setattr(editor_language_servers, "request", no_controller_server)
    response = await editor_server.client.post("/api/workspace/language", params=editor_server.params,
        headers={"x-minicode-window-binding-token": "owned-editor-binding"}, json={
            "method": "hover", "window_owner_id": "owned-editor-owner", "window_instance_id": "owned-editor-window",
            "client_id": "owned-editor-client", "snapshot_revision": 1, "documents": [{"path": "same.py", "content": "unsaved = True",
                "document_id": "owned-model", "model_version": 1, "language": "python"}], "line": 0, "character": 1})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "editor.executor_language_server_unconfigured"


def test_signed_workspace_raw_owner_cannot_be_retargeted(monkeypatch):
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "owned-raw-secret")
    expires = int(time.time()) + 300
    root, path = "C:/view", "file:///remote/target/image.png"
    owner = {"session_id": "session", "conversation_id": "conversation", "environment_id": "target"}
    signature = _workspace_raw_token_signature(path, root, expires, "owned-raw-secret", **owner)
    from urllib.parse import urlencode
    def request(values):
        return Request({"type": "http", "method": "GET", "path": "/api/workspace/raw", "headers": [],
            "query_string": urlencode({"path": path, "workspace_root": root, "raw_token": f"{expires}.{signature}", **values}).encode()})
    assert _is_workspace_raw_token_authorized(request(owner))
    assert not _is_workspace_raw_token_authorized(request({**owner, "environment_id": "other"}))


@pytest.mark.asyncio
async def test_fresh_backend_main_process_starts_real_http(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "MINICODE_STATE_ROOT": str(tmp_path / "cold-state"), "PYTHONPATH": str(root),
        "MINICODE_RUNTIME_TOKEN": "owned-cold-http", "PYTHONUTF8": "1"}
    program = ("import asyncio,sys,uvicorn\nasync def main():\n"
        f" server=uvicorn.Server(uvicorn.Config('backend.main:app',host='127.0.0.1',port={port},log_level='info'))\n"
        " task=asyncio.create_task(server.serve())\n await asyncio.to_thread(sys.stdin.readline)\n server.should_exit=True\n await task\nasyncio.run(main())")
    process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", program, cwd=tmp_path, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    lines = []
    try:
        async with asyncio.timeout(25):
            while True:
                value = await process.stdout.readline()
                assert value, b"".join(lines).decode("utf-8", errors="replace")
                lines.append(value)
                if b"Uvicorn running on" in value:
                    break
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/health", headers={"X-MiniCode-Token": "owned-cold-http"})
        assert response.status_code == 200, response.text
    finally:
        process.stdin.write(b"stop\n")
        await process.stdin.drain()
        output, _ = await asyncio.wait_for(process.communicate(), 25)
        lines.append(output)
        (root / "output/executor-editor-cold-http-20261016.log").write_bytes(b"".join(lines))
    assert process.returncode == 0, b"".join(lines).decode("utf-8", errors="replace")
