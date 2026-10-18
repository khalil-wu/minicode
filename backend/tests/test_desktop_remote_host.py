"""Real MiniCode daemon, native PTYs and its persistent harness over loopback."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys

import httpx
import pytest
from websockets.asyncio.client import connect

from backend.desktop_remote.relay import read_frame


ROOT = Path(__file__).resolve().parents[2]


class _Attachment:
    def __init__(self, child, ready, windows):
        self.child, self.ready, self.windows = child, ready, windows
        self.events = []
        self.reverse = {}
        self.write_lock = asyncio.Lock()
        self.task = asyncio.create_task(self._read(), name="owned-desktop-frame-consumer")

    async def send(self, frame):
        body = json.dumps(frame).encode()
        async with self.write_lock:
            self.child.stdin.write(struct.pack("!I", len(body)) + body)
            await self.child.stdin.drain()

    async def _read(self):
        try:
            while True:
                frame = await read_frame(self.child.stdout)
                kind = frame["type"]
                if kind == "open":
                    assert frame["target"] == "bridge" and frame["id"] % 2 == 0
                    self.reverse[frame["id"]] = bytearray()
                elif kind == "data":
                    body = self.reverse[frame["id"]]
                    data = base64.b64decode(frame["data"])
                    body.extend(data)
                    await self.send({"type": "credit", "id": frame["id"], "bytes": len(data)})
                    head, separator, content = body.partition(b"\r\n\r\n")
                    if not separator:
                        continue
                    headers = dict(line.decode().split(": ", 1) for line in head.split(b"\r\n")[1:])
                    lower = {key.lower(): value for key, value in headers.items()}
                    if len(content) != int(lower.get("content-length", "0")):
                        continue
                    assert lower["authorization"] == "Bearer " + self.ready["bridge_token"]
                    assert head.split(b"\r\n")[0] == b"POST /v1/window-binding HTTP/1.1"
                    binding = json.loads(content)
                    expected = self.windows.get(binding["window_instance_id"])
                    accepted = expected == binding
                    payload = json.dumps({"ok": accepted}).encode()
                    response = (f"HTTP/1.1 {'200 OK' if accepted else '403 Forbidden'}\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
                    await self.send({"type": "data", "id": frame["id"], "data": base64.b64encode(response).decode()})
                    await self.send({"type": "end", "id": frame["id"]})
                elif kind == "event":
                    self.events.append(frame)
                elif kind == "close":
                    self.reverse.pop(frame["id"], None)
                elif kind == "error":
                    raise AssertionError(frame)
                else:
                    assert kind in {"end", "credit", "pong"}, frame
        except asyncio.IncompleteReadError:
            return

    async def detach(self):
        self.child.stdin.close()
        await self.child.stdin.wait_closed()
        async with asyncio.timeout(8):
            await self.child.wait()
            await self.task
        assert self.child.returncode == 0, (await self.child.stderr.read()).decode(errors="replace")


@asynccontextmanager
async def _daemon(tmp_path, *, settings=None):
    state = tmp_path / "host-state"
    state.mkdir()
    if settings is not None:
        (state / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    desktop_id = secrets.token_hex(16)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "MINICODE_CONFIG_DIR": str(state / "config"),
        "MINICODE_MANAGED_SETTINGS_DIR": str(state / "managed"), "MINICODE_REQUIREMENTS_FILE": str(state / "no-requirements.toml"),
        "MINICODE_SYSTEM_CONFIG_FILE": str(state / "no-system.toml")}
    env.pop("CODEX_EXEC_SERVER_URL", None)
    windows, attachments = {}, []

    async def attach():
        marker = secrets.token_hex(32)
        child = await asyncio.create_subprocess_exec(sys.executable, "-m", "backend.desktop_remote", "proxy", "--desktop-id", desktop_id,
            "--marker", marker, "--state-root", str(state), cwd=str(ROOT), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(35):
            assert await child.stdout.readexactly(32) == bytes.fromhex(marker)
            ready = await read_frame(child.stdout)
        assert ready["type"] == "ready" and ready["protocol"] == 1
        attachment = _Attachment(child, ready, windows)
        attachments.append(attachment)
        return attachment

    first = await attach()
    try:
        yield first, attach, windows, state
    finally:
        stop = await asyncio.create_subprocess_exec(sys.executable, "-m", "backend.desktop_remote", "stop", "--desktop-id", desktop_id,
            "--state-root", str(state), cwd=str(ROOT), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(20):
            _output, error = await stop.communicate()
        assert stop.returncode == 0, error.decode(errors="replace")
        for attachment in attachments:
            async with asyncio.timeout(8):
                await attachment.child.wait()
                await attachment.task
            assert attachment.child.returncode == 0, (await attachment.child.stderr.read()).decode(errors="replace")


async def _native(client, window, channel, args, **kwargs):
    return await client.post("/api/desktop-host/native", json={**window, "channel": "minicode:" + channel, "args": args}, **kwargs)


@pytest.mark.asyncio
async def test_internal_window_binding_connects_directly_despite_environment_http_proxy(monkeypatch):
    from backend.desktop_remote.native import NativeRequest, RemoteNative

    bridge_requests, proxy_requests = [], []

    async def respond(reader, writer, *, proxy):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("ascii").split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:] if line)
        headers = {name.lower(): value for name, value in headers.items()}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        (proxy_requests if proxy else bridge_requests).append((lines[0], headers, body))
        payload = b'{"ok":true}' if not proxy else b'{"error":"owned proxy trap"}'
        status = b"200 OK" if not proxy else b"502 Bad Gateway"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    bridge = await asyncio.start_server(lambda reader, writer: respond(reader, writer, proxy=False), "127.0.0.1", 0)
    proxy = await asyncio.start_server(lambda reader, writer: respond(reader, writer, proxy=True), "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    native = RemoteNative(lambda _event: None, f"http://127.0.0.1:{bridge.sockets[0].getsockname()[1]}", "owned-bridge-only")
    request = NativeRequest(window_instance_id="direct-window", window_owner_id="primary",
        window_binding_token="owned-window", channel="minicode:workspace:directoryEntries", args=[])
    try:
        async with asyncio.timeout(10):
            await native._binding(request)
        assert not proxy_requests
        assert len(bridge_requests) == 1
        line, headers, body = bridge_requests[0]
        assert line == "POST /v1/window-binding HTTP/1.1"
        assert headers["authorization"] == "Bearer owned-bridge-only"
        assert json.loads(body) == {"window_instance_id": "direct-window", "window_owner_id": "primary", "window_binding_token": "owned-window"}
    finally:
        await native.close()
        bridge.close()
        proxy.close()
        await bridge.wait_closed()
        await proxy.wait_closed()


@pytest.mark.asyncio
async def test_actual_remote_folder_browsing_is_window_bound_and_only_final_selection_grants_workspace_trust(tmp_path):
    parent = tmp_path / "remote chooser 中文"
    parent.mkdir()
    selected = parent / "selected project"
    selected.mkdir()
    (parent / ".hidden-folder").mkdir()
    (parent / "not-a-folder.txt").write_text("actual remote file", encoding="utf-8")
    if os.name == "nt":
        subprocess.run([shutil.which("node"), "-e", "require('node:fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
                        str(selected), str(parent / "junction")], check=True)
    async with _daemon(tmp_path) as (first, _attach, windows, _state):
        window = {"window_instance_id": "directory-picker-window", "window_owner_id": "primary", "window_binding_token": secrets.token_hex(16)}
        windows[window["window_instance_id"]] = window
        ready = first.ready
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['backend_port']}", headers={"x-minicode-token": ready["runtime_token"]}, timeout=15) as client:
            assert (await _native(client, window, "fs:listTree", [str(parent)])).status_code == 403
            response = await _native(client, window, "workspace:directoryEntries", [str(parent), True])
            assert response.status_code == 200, response.text
            listing = response.json()
            assert listing["directoryPath"] == str(parent)
            assert listing["parentPath"] == str(parent.parent)
            assert {entry["name"] for entry in listing["entries"]} == {"selected project", ".hidden-folder", *({"junction"} if os.name == "nt" else set())}
            assert all(entry["type"] == "directory" and Path(entry["path"]).is_dir() for entry in listing["entries"])
            assert (await _native(client, window, "fs:listTree", [str(parent)])).status_code == 403
            root = Path(parent.anchor)
            assert (await _native(client, window, "workspace:directoryEntries", [str(root), True])).json()["parentPath"] is None
            assert (await _native(client, window, "workspace:directoryEntries", [str(parent / "missing"), True])).status_code == 404
            assert (await _native(client, window, "workspace:directoryEntries", [str(parent / "not-a-folder.txt"), True])).status_code == 404
            forged = {**window, "window_binding_token": "different-owner"}
            assert (await _native(client, forged, "workspace:directoryEntries", [str(parent), True])).status_code == 403
            assert (await _native(client, window, "workspace:trust", [str(selected)])).status_code == 200
            assert (await _native(client, window, "fs:listTree", [str(selected)])).status_code == 200
            assert (await _native(client, window, "fs:listTree", [str(parent)])).status_code == 403


@pytest.mark.asyncio
async def test_remote_picker_directory_links_match_actual_source_filesystem(tmp_path):
    source = os.environ.get("MINICODE_SOURCE_EXECUTOR")
    if source is None:
        pytest.skip("MINICODE_SOURCE_EXECUTOR is not configured; actual Source directory-link classification not claimed")
    from backend.desktop_remote.native import _directory_entries
    from backend.environments import EnvironmentManager

    parent = tmp_path / "source picker 中文"
    parent.mkdir()
    selected = parent / "selected project"
    selected.mkdir()
    (parent / "plain.txt").write_bytes(b"actual source filesystem comparison\n")
    vanished = parent / "vanished target"
    vanished.mkdir()
    link = parent / "directory link"
    dangling = parent / "dangling link"
    if os.name == "nt":
        for destination, target in [(link, selected), (dangling, vanished)]:
            subprocess.run([shutil.which("node"), "-e", "require('node:fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
                            str(target), str(destination)], check=True)
    else:
        link.symlink_to(selected, target_is_directory=True)
        dangling.symlink_to(vanished, target_is_directory=True)
        (parent / "file link").symlink_to(parent / "plain.txt")
    vanished.rmdir()
    home = tmp_path / "owned-source-home"
    home.mkdir()
    manager = EnvironmentManager(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "none"})
    reference = manager.upsert({"id": "directory-reference", "program": source,
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"], "env": {"CODEX_HOME": str(home)}})
    try:
        expected = await reference.filesystem.read_directory(parent.as_uri(), sandbox=None)
        actual = _directory_entries(str(parent), False)
        assert {entry["name"]: entry["type"] for entry in actual["entries"]} == {
            entry["fileName"]: "directory" if entry["isDirectory"] else "file" for entry in expected}
        directory_names = {entry["name"] for entry in _directory_entries(str(parent), True)["entries"]}
        assert directory_names == {"selected project", "directory link"}
        assert "dangling link" not in {entry["name"] for entry in actual["entries"]}
        child = reference.client.connection.transport.child
    finally:
        await manager.close()
    assert child.returncode == 0 and not reference.client.transport_receipts[-1].forced


@pytest.mark.asyncio
async def test_actual_cli_native_pty_continues_offline_and_lifecycle_secret_releases_only_closed_window(tmp_path):
    workspace = tmp_path / "native-workspace"
    workspace.mkdir()
    async with _daemon(tmp_path) as (first, attach, windows, state):
        window = {"window_instance_id": "actual-native-window", "window_owner_id": "primary", "window_binding_token": secrets.token_hex(16)}
        windows[window["window_instance_id"]] = window
        ready = first.ready
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['backend_port']}", headers={"x-minicode-token": ready["runtime_token"]}, timeout=15) as client:
            assert (await _native(client, window, "workspace:trust", [str(workspace)])).status_code == 200
            response = await _native(client, window, "pty:spawn", [str(workspace), "owned-native-conversation"])
            assert response.status_code == 200, response.text
            terminal = response.json()
            session_id = terminal["session_id"]
            started, finished = workspace / "native-started", workspace / "native-finished"
            program = ("import time; from pathlib import Path; Path(" + repr(str(started)) + ").write_text('actual native child'); "
                "print('PTY中文😀-start',flush=True); time.sleep(2); Path(" + repr(str(finished)) + ").write_text('continued while detached'); "
                "print('PTY-after-detach',flush=True)")
            command = ("& '" + sys.executable.replace("'", "''") + "' -c \"" + program + "\"\r" if os.name == "nt"
                else "'" + sys.executable + "' -c \"" + program + "\"\n")
            assert (await _native(client, window, "pty:write", [session_id, command, "owned-native-conversation"])).status_code == 200
            async with asyncio.timeout(12):
                while not started.exists():
                    await asyncio.sleep(.02)
            await first.detach()
            async with asyncio.timeout(10):
                while not finished.exists():
                    await asyncio.sleep(.02)
            resumed = await attach()
            assert resumed.ready["generation"] == ready["generation"] and resumed.ready["backend_port"] == ready["backend_port"]
            response = await _native(client, window, "pty:snapshot", [session_id, 200000, "owned-native-conversation"])
            assert response.status_code == 200, response.text
            snapshot = response.json()
            assert snapshot["pid"] == terminal["pid"] and snapshot["is_alive"]
            assert "PTY-after-detach" in snapshot["output"] and "PTY中文😀-start" in snapshot["output"]
            assert (await _native(client, window, "fs:readFile", [str(finished)])).json()["content"] == "continued while detached"
            assert (await _native(client, window, "pty:snapshot", [session_id, 1000, "other-conversation"])).status_code == 403
            resize = await _native(client, window, "pty:resize", [session_id, 100, 30, "owned-native-conversation"])
            assert resize.status_code == 200 and resize.json() is True, resize.text
            other = {"window_instance_id": "other-native-window", "window_owner_id": "secondary", "window_binding_token": secrets.token_hex(16)}
            windows[other["window_instance_id"]] = other
            assert (await _native(client, other, "pty:spawn", [str(workspace), "other-native-conversation"])).status_code == 200
            del windows[window["window_instance_id"]]
            assert (await _native(client, window, "window:release", [])).status_code == 403
            release = await _native(client, window, "window:release", [], headers={"x-minicode-desktop-lifecycle-token": ready["bridge_token"]})
            assert release.status_code == 200, release.text
            assert len((await _native(client, other, "pty:list", ["other-native-conversation"])).json()) == 1
            windows[window["window_instance_id"]] = window
            assert (await _native(client, window, "pty:list", ["owned-native-conversation"])).json() == []


@asynccontextmanager
async def _provider(command):
    requests = []
    final = asyncio.Event()

    async def respond(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {key.decode().lower(): value.strip().decode() for key, value in
            (line.split(b":", 1) for line in head.split(b"\r\n")[1:] if line)}
        body = json.loads(await reader.readexactly(int(headers["content-length"])))
        requests.append(body)
        tool_outputs = [message for message in body["messages"] if message["role"] == "tool"]
        if body.get("tools") and not tool_outputs:
            delta = {"tool_calls": [{"index": 0, "id": "actual-remote-command", "type": "function", "function": {
                "name": "run_command", "arguments": json.dumps({"command": command, "yield_time_ms": 60000,
                    "timeout": 90, "login": False})}}]}
            finish = "tool_calls"
        else:
            delta = {"content": "remote harness completed" if tool_outputs else "Owned fixture title"}
            finish = "stop"
            if tool_outputs:
                final.set()
        if body.get("stream"):
            chunks = [{"id": "owned-response", "object": "chat.completion.chunk", "model": "owned-host", "choices": [
                {"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "owned-response", "object": "chat.completion.chunk", "model": "owned-host", "choices": [
                    {"index": 0, "delta": {}, "finish_reason": finish}]}]
            encoded = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"
            content_type = "text/event-stream"
        else:
            encoded = json.dumps({"id": "owned-title", "choices": [{"index": 0,
                "message": {"role": "assistant", **delta}, "finish_reason": finish}]}).encode()
            content_type = "application/json"
        writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n").encode() + encoded)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", requests, final


async def _event(socket, kind, *, command=None):
    async with asyncio.timeout(20):
        while True:
            message = json.loads(await socket.recv())
            if message["type"] == "error":
                raise AssertionError(message)
            if message["type"] == kind and (command is None or message.get("command") == command):
                return message


@pytest.mark.asyncio
async def test_real_remote_harness_command_survives_proxy_and_websocket_detach_beyond_transport_retirement(tmp_path):
    workspace = tmp_path / "harness-workspace"
    workspace.mkdir()
    started, release, finished = (workspace / name for name in ("query-started", "query-release", "query-finished"))
    script = workspace / "retained_task.py"
    script.write_text("import os,time\nfrom pathlib import Path\n"
        + "Path(" + repr(str(started)) + ").write_text(str(os.getpid()))\n"
        + "while not Path(" + repr(str(release)) + ").exists(): time.sleep(.02)\n"
        + "Path(" + repr(str(finished)) + ").write_text(str(os.getpid()))\n"
        + "print('actual remote command survived detach',flush=True)\n", encoding="utf-8")
    command = ("& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'" if os.name == "nt"
        else "'" + sys.executable + "' '" + str(script) + "'")
    async with _provider(command) as (url, requests, final):
        settings = {"llm": {"provider": "openai", "openai": {"model": "owned-host", "base_url": url, "wire_api": "chat",
            "auth_header": False, "api_key": "", "tool_mode": "direct"}}, "permissions": {"require_confirm": [], "require_diff_review": []},
            "approval_policy": "never", "agent": {"max_iterations": 4, "max_turn_seconds": 120, "code_mode_only": False}}
        async with _daemon(tmp_path, settings=settings) as (first, attach, windows, state):
            ready = first.ready
            window = {"window_instance_id": "retained-harness-window", "window_owner_id": "primary",
                "window_binding_token": secrets.token_hex(16)}
            windows[window["window_instance_id"]] = window
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['backend_port']}",
                headers={"x-minicode-token": ready["runtime_token"]}) as client:
                assert (await _native(client, window, "workspace:trust", [str(workspace)])).status_code == 200
            session_id = "owned_remote_harness_session"
            endpoint = (f"ws://127.0.0.1:{ready['backend_port']}/ws?session_id={session_id}"
                f"&window_instance_id={window['window_instance_id']}&window_owner_id={window['window_owner_id']}")
            protocols = ["minicode", "minicode-window." + window["window_binding_token"]]
            async with connect(endpoint, subprotocols=protocols, additional_headers={"x-minicode-token": ready["runtime_token"]}, max_size=8 * 1024 * 1024) as socket:
                await socket.send(json.dumps({"type": "workspace.set", "path": str(workspace), "permission_mode": "bypass"}))
                created = await _event(socket, "command.result", command="workspace.set")
                assert created["level"] == "success", created
                conversation_id = created["data"]["conversation_id"]
                await socket.send(json.dumps({"type": "user_message", "conversation_id": conversation_id,
                    "content": "Run the owned command and wait for its actual result.", "user_message_id": "owned-retained-user",
                    "assistant_message_id": "owned-retained-answer", "client_command_id": "owned-retained-command"}))
                async with asyncio.timeout(20):
                    while not started.exists():
                        if final.is_set():
                            raise AssertionError("The fixture model finished before executing the actual command")
                        await asyncio.sleep(.02)
            actual_pid = started.read_text()
            await first.detach()
            # The normal WS transport expires after 30 seconds. The retained
            # core owns this actual command, independently of either socket.
            await asyncio.sleep(31)
            assert not finished.exists() and not final.is_set()
            release.write_text("finish the same actual process", encoding="utf-8")
            async with asyncio.timeout(15):
                await final.wait()
            assert finished.read_text() == actual_pid
            assert any("actual remote command survived detach" in str(message.get("content", ""))
                for request in requests for message in request["messages"] if message["role"] == "tool")
            resumed = await attach()
            assert resumed.ready["generation"] == ready["generation"]
            async with connect(endpoint, subprotocols=protocols, additional_headers={"x-minicode-token": ready["runtime_token"]}, max_size=8 * 1024 * 1024) as socket:
                await socket.send(json.dumps({"type": "session.sync", "conversation_id": conversation_id}))
                synced = await _event(socket, "session.synced")
                assert synced["active_conversation_id"] == conversation_id
