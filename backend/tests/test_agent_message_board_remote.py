from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from backend.agent.message_board import BoardActor, MessageBoardError, encode
from backend.agent import message_board_remote as remote
from backend.agent.message_board_remote import RemoteAgentMessageBoard, start_remote_board_notifications
from backend.agent.message_board_remote_protocol import (
    MAX_BODY, MAX_RESPONSE, PostContent, PostMetadata, WireRecord, board_events, board_identity, member_identity,
)
from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.agent.turn_kernel import TurnKernel
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.tests.test_agent_message_board import child, make_case


TOKEN = "fixture-board-credential-000000000000"
RUNTIME_TOKEN = "fixture-runtime-credential-00000000000"
MESSAGE = "00000000-0000-4000-8000-000000000001"
NOW = "2026-10-21T00:00:00Z"
META = {"message_id": MESSAGE, "thread_id": MESSAGE, "channel_name": "design", "author": "/root", "created_at": NOW}
PREVIEW = {**META, "text_preview": "Straße 🙂", "n_chars": 8, "truncated": False}
CHANNEL = {"channel_name": "design", "created_at": NOW, "created_by": "/root", "message_count": 1, "last_message_id": MESSAGE}


class LoopbackBoard:
    """Actual TCP HTTP/SSE service; a ready frame never ends the connection."""
    def __init__(self):
        self.requests = []
        self.connections, self.tasks = set(), set()
        self.outgoing = asyncio.Queue()
        self.disconnected = asyncio.Event()
        self.send_ready = True
        self.watch_rejections = 0
        self.handler = self.default_reply
        self.failures = []

    async def start(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/board-service/"
        return self

    def default_reply(self, request):
        if request["method"] == "PUT":
            return 200, request["path"].rsplit("/", 1)[-1]
        if request["path"].endswith("/members"):
            return 200, RUNTIME_TOKEN
        if request["method"] == "DELETE":
            return 204, None
        method = request["json"]["method"]
        if method == "create_channel":
            return 200, CHANNEL
        if method == "post":
            return 200, META
        if method == "list_channels":
            return 200, {"results": [CHANNEL], "next_cursor": None}
        if method == "list_threads":
            return 200, {"results": [{"thread_id": MESSAGE, "root_post": PREVIEW, "reply_count": 0,
                "last_activity_at": NOW, "latest_reply": None}], "next_cursor": None}
        if method == "search_posts":
            return 200, {"results": [PREVIEW], "next_cursor": None}
        if method == "read_thread":
            return 200, {"root_post": PREVIEW, "results": [], "next_cursor": None}
        if method == "read_post":
            return 200, {**META, "text": "🙂", "n_chars": 8, "next_offset_chars": 8}
        if method == "set_subscription":
            return 200, {"channel_name": "design", "thread_id": None, "target_agent": "/root/worker",
                "enabled": request["json"]["params"]["change"] == "Subscribe", "last_message_id": MESSAGE}
        raise AssertionError(method)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections.add(writer)
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            method, path, _version = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if line)
            headers = {key.lower(): value for key, value in headers.items()}
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            request = {"method": method, "path": path, "headers": headers, "body": body,
                "json": json.loads(body) if body else None}
            self.requests.append(request)
            if path.endswith("/notifications"):
                if self.watch_rejections:
                    self.watch_rejections -= 1
                    payload = encode({"code": "fixture-unavailable", "message": "try again"}).encode()
                    writer.write(f"HTTP/1.1 503 Response\r\ncontent-type: application/json\r\ncontent-length: {len(payload)}\r\nconnection: close\r\n\r\n".encode() + payload)
                    await writer.drain()
                    return
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/octet-stream\r\nconnection: close\r\n\r\n")
                if self.send_ready:
                    writer.write(b"event: ready\r\ndata: {}\r\n\r\n")
                await writer.drain()
                eof = asyncio.create_task(reader.read())
                try:
                    while True:
                        outgoing = asyncio.create_task(self.outgoing.get())
                        try:
                            done, _ = await asyncio.wait({eof, outgoing}, return_when=asyncio.FIRST_COMPLETED)
                            if eof in done:
                                self.disconnected.set()
                                break
                            writer.write(outgoing.result())
                            await writer.drain()
                        finally:
                            if not outgoing.done():
                                outgoing.cancel()
                            await asyncio.gather(outgoing, return_exceptions=True)
                finally:
                    eof.cancel()
                    await asyncio.gather(eof, return_exceptions=True)
            else:
                status, value = self.handler(request)
                payload = encode(value).encode() if value is not None else b""
                writer.write(f"HTTP/1.1 {status} Response\r\ncontent-type: application/json\r\ncontent-length: {len(payload)}\r\nconnection: close\r\n\r\n".encode() + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass  # Peer closes are the tested receiver teardown/byte-limit path.
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.failures.append(error)
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.discard(writer)
            self.tasks.discard(task)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.connections):
            writer.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        assert not self.failures, self.failures


@asynccontextmanager
async def service():
    value = await LoopbackBoard().start()
    try:
        yield value
    finally:
        await value.close()


def remote_case(tmp_path, url, *, token=TOKEN, env=None, active=False):
    case = make_case(tmp_path, backend="local")
    settings = {"url": url, **({"bearer_token_env_var": env} if env is not None else {"bearer_token": token})}
    case.config.llm = replace(case.config.llm, proxy_mode="direct")
    case.config.config_layer_stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"features": {
        "multi_agent_v2": {"enabled": True, "message_board_remote": settings}}}),))
    case.snapshot = replace(case.snapshot, config=case.config)
    case.root = replace(case.root, model_execution=case.snapshot)
    case.root.run_context.model_execution = case.snapshot
    case.root.metadata["_toolset_policy"] = effective_toolset_policy(base_policy=None, tool_registry=case.registry,
        disabled_tools=set(), requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="bypass",
        tool_mode="direct", model_execution=case.snapshot)
    kernel = None
    if active:
        kernel = TurnKernel(runtime=case.runtime, run_record=case.runtime.get_run(case.root.metadata["run_id"]),
            metadata=case.root.metadata, emit_event=None, initial_user_message="Owned remote turn",
            state=AgentState(user_message="Owned remote turn"), run_context=case.root.run_context)
    case.kernel = kernel
    return case


async def close_case(case):
    if case.kernel is not None:
        case.kernel.complete_run_record("completed")
    await case.runtime.aclose(release_lease=True)
    await asyncio.gather(*tuple(case.root.run_context.lifecycle_cleanup_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_http_nine_operations_provisioning_identity_credentials_and_clock(tmp_path):
    async with service() as server:
        case = remote_case(tmp_path, server.url)
        actor = BoardActor(case.root)
        calls = []
        async def clock(caller):
            calls.append(caller)
            return datetime(2026, 10, 21, tzinfo=timezone.utc)
        board = RemoteAgentMessageBoard(actor, case.root, clock=clock)
        other, _ = child(case)
        try:
            assert board.identity == board_identity("tree-thread")
            assert board.caller == member_identity("tree-thread", "/root")
            assert await board.create_board() == board.identity
            registered = await board.register_members({board.caller: "/root", member_identity(actor.tree, "/root/worker"): "/root/worker"})
            assert registered.get_secret_value() == RUNTIME_TOKEN and RUNTIME_TOKEN not in repr(registered)
            rows = [
                ("create_channel", {"channel_name": "design"}, "create_channel", {"channel_name": "design", "subscription": "Subscribe"}),
                ("get_channels", {"query": "de", "recent_first": False}, "list_channels", {"query": "de", "direction": "OldestFirst", "page": {"cursor": None, "limit": 3}}),
                ("list_threads", {"channel_name": "design", "sort": "activity"}, "list_threads", {"channel_name": "design", "sort": "activity", "direction": "NewestFirst", "page": {"cursor": None, "limit": 3}, "max_chars_per_post": 17}),
                ("search_posts", {"author": "/root/worker", "after_message_id": MESSAGE}, "search_posts", {"channel_name": None, "query": None, "after_message_id": MESSAGE, "author": "/root/worker", "page": {"cursor": None, "limit": 3}, "max_chars_per_post": 17}),
                ("read_thread", {"thread_id": MESSAGE}, "read_thread", {"thread_id": MESSAGE, "page": {"cursor": None, "limit": 3}, "max_chars_per_post": 17}),
                ("read_post", {"message_id": MESSAGE, "offset_chars": 7}, "read_post", {"message_id": MESSAGE, "offset_chars": 7, "limit_chars": 17}),
                ("subscribe", {"channel_name": "design", "target_agent": "/root/worker"}, "set_subscription", {"target": {"Channel": "design"}, "target_agent": "/root/worker", "change": "Subscribe"}),
                ("unsubscribe", {"thread_id": MESSAGE}, "set_subscription", {"target": {"Thread": MESSAGE}, "target_agent": None, "change": "Unsubscribe"}),
                ("post", {"new_channel_name": "design", "text": "Straße 🙂", "agents_to_notify": ["/root/worker"]}, "post", {"request_id": "call-1", "destination": {"NewChannel": "design"}, "text": "Straße 🙂", "agents_to_notify": ["/root/worker"]}),
            ]
            for name, args, method, params in rows:
                before = json.dumps(args, sort_keys=True)
                result, recipients, notice = await board.run(name, args, request_id="call-1", limit=3, chars=17)
                request = server.requests[-1]
                assert request["json"] == {"caller": str(board.caller), "timestamp": "2026-10-21T00:00:00+00:00" if method in {"create_channel", "post"} else None, "method": method, "params": params}
                assert result and recipients == [] and notice is None
                assert json.dumps(args, sort_keys=True) == before
            assert calls == [board.caller, board.caller]
            await board.delete_board()
            base = f"/board-service/v1/boards/{board.identity}"
            assert server.requests[0]["path"] == base and server.requests[1]["path"] == base + "/members"
            assert all(request["headers"]["authorization"] == "Bearer " + TOKEN for request in server.requests)
            assert not actor.runtime.message_boards.path.exists() and not actor.runtime.message_boards.memory
            assert UUID(str(board.caller)) and other.run_context.agent_runtime is case.runtime
        finally:
            await close_case(case)


@pytest.mark.asyncio
async def test_actual_http_post_replay_keeps_request_identity_after_clock_and_response_failure(tmp_path):
    async with service() as server:
        case = remote_case(tmp_path, server.url)
        available = True
        async def clock(_caller):
            if not available:
                raise OSError("clock unavailable")
            return datetime(2026, 10, 21, tzinfo=timezone.utc)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root, clock=clock)
        server.handler = lambda request: (503, {"code": "unavailable", "message": "response unavailable"})
        try:
            with pytest.raises(OSError, match="unavailable"):
                await board.run("post", {"new_channel_name": "design", "text": "committed"}, request_id="same-call")
            available = False
            server.handler = server.default_reply
            result, _recipients, _notice = await board.run("post", {"new_channel_name": "design", "text": "committed"}, request_id="same-call")
            assert result["message_id"] == MESSAGE
            first, replay = server.requests
            assert first["json"]["params"] == replay["json"]["params"]
            assert first["json"]["timestamp"] is not None and replay["json"]["timestamp"] is None
            with pytest.raises(OSError, match="clock unavailable"):
                await board.run("create_channel", {"channel_name": "clock-blocked"})
            assert len(server.requests) == 2
        finally:
            await close_case(case)


@pytest.mark.parametrize("url", ["not a URL", "file:///tmp/board", "https://board.test/?query", "https://board.test/#fragment"])
def test_remote_endpoint_is_validated_without_local_fallback(tmp_path, url):
    case = remote_case(tmp_path, url)
    try:
        with pytest.raises((MessageBoardError, httpx.InvalidURL)):
            RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        assert not case.runtime.message_boards.path.exists() and not case.runtime.message_boards.memory
    finally:
        case.runtime.close(release_lease=True)


@pytest.mark.parametrize("token", ["short", "x" * 4097, "x" * 31 + " ", "é" * 32], ids=["short", "long", "space", "non-ascii"])
def test_invalid_remote_credentials_are_not_exposed_or_sent(tmp_path, token):
    case = remote_case(tmp_path, "https://board.test", token=token)
    try:
        with pytest.raises(MessageBoardError) as caught:
            RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        assert token not in str(caught.value)
    finally:
        case.runtime.close(release_lease=True)


def test_env_credential_precedence_and_proxy_are_captured_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARD_FIXTURE_TOKEN", RUNTIME_TOKEN)
    calls = []
    def proxy(url, *, proxy_mode):
        calls.append((url, proxy_mode))
        return {"proxy": "http://127.0.0.1:9", "trust_env": False}
    monkeypatch.setattr(remote, "provider_httpx_proxy_kwargs", proxy)
    case = remote_case(tmp_path, "https://board.test/prefix", env="BOARD_FIXTURE_TOKEN")
    try:
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        case.config.llm = replace(case.config.llm, proxy_mode="inherit")
        monkeypatch.setenv("BOARD_FIXTURE_TOKEN", TOKEN)
        assert board._token == RUNTIME_TOKEN and board.http_options == {"proxy": "http://127.0.0.1:9", "trust_env": False}
        assert calls == [("https://board.test/prefix", "direct")]
        assert "bearer_token" not in encode(case.root.metadata["_toolset_policy"].coordination_options)
    finally:
        case.runtime.close(release_lease=True)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [False, True])
async def test_remote_rpc_request_and_response_byte_limits(tmp_path, monkeypatch, over):
    case = remote_case(tmp_path, "https://board.test")
    board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
    sent = []
    response = {**META, "text": "", "n_chars": 1, "next_offset_chars": 1}
    empty_size = len(encode(response).encode())
    response["text"] = "x" * (MAX_RESPONSE - empty_size + int(over))
    stream = Chunks([encode(response).encode()])
    async def respond(request):
        sent.append(request)
        return httpx.Response(200, request=request, stream=stream)
    monkeypatch.setattr(board, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        payload = {"caller": str(board.caller), "timestamp": None, "method": "post", "params": {"request_id": "same", "destination": {"NewChannel": "design"}, "text": "", "agents_to_notify": []}}
        payload["params"]["text"] = "x" * (MAX_BODY - len(encode(payload).encode()) + int(over))
        if over:
            with pytest.raises(MessageBoardError, match="request exceeds"):
                await board._request("POST", "/call", payload=payload, model=PostMetadata)
            assert sent == []
            with pytest.raises(OSError, match="response exceeds"):
                await board._request("POST", "/call", payload={"method": "read_post"}, model=PostContent)
        else:
            result = await board._request("POST", "/call", payload=payload, model=PostContent)
            assert len(sent[0].content) == MAX_BODY and len(result.text) == MAX_RESPONSE - empty_size
        assert stream.closed
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(400, MessageBoardError), (503, OSError)])
async def test_actual_http_status_keeps_source_error_kind(tmp_path, status, error):
    async with service() as server:
        case = remote_case(tmp_path, server.url)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        server.handler = lambda request: (status, {"code": "fixture-code", "message": "fixture failure"})
        try:
            with pytest.raises(error, match="fixture-code: fixture failure"):
                await board.run("read_post", {"message_id": MESSAGE})
        finally:
            await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(200, ValidationError), (400, OSError), (503, OSError)])
async def test_invalid_body_preserves_the_source_success_and_error_kinds(tmp_path, status, error):
    async with service() as server:
        case = remote_case(tmp_path, server.url)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        server.handler = lambda request: (status, {"code": 7})
        try:
            with pytest.raises(error):
                await board.run("read_post", {"message_id": MESSAGE})
        finally:
            await close_case(case)


@pytest.mark.asyncio
async def test_remote_factory_and_real_executor_use_the_captured_policy_without_opening_sqlite(tmp_path):
    async with service() as server:
        case = remote_case(tmp_path, server.url)
        try:
            result = await run_tool(ToolCallEvent("real-rpc", "post", {"new_channel_name": "design", "text": "wire"}),
                case.registry, case.root)
            assert not result.is_error, result.content
            assert json.loads(result.content)["message_id"] == MESSAGE
            assert server.requests[0]["json"]["method"] == "post"
            assert server.requests[0]["json"]["params"]["request_id"] == "main-a:real-rpc"
            assert not case.runtime.message_boards.path.exists() and not case.runtime.message_boards.memory
        finally:
            await close_case(case)


@pytest.mark.asyncio
async def test_request_and_readiness_share_the_source_30_second_deadline(tmp_path, monkeypatch):
    deadlines = []
    def timeout(seconds):
        deadlines.append(seconds)
        return asyncio.timeout(.03)
    monkeypatch.setattr(remote, "asyncio", SimpleNamespace(timeout=timeout))
    async with service() as server:
        server.send_ready = False
        case = remote_case(tmp_path, server.url)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        try:
            ready = False
            def on_ready():
                nonlocal ready
                ready = True
            with pytest.raises(TimeoutError):
                await anext(board.notifications("original-turn", on_ready))
            assert not ready and deadlines == [30]
        finally:
            await close_case(case)


def notice(board, turn="main-a", *, post=None, recipient=None):
    return {"recipient": str(recipient or board.caller), "turn_id": turn, "post": post or PREVIEW}


@pytest.mark.asyncio
async def test_actual_sse_ready_stays_open_and_invalid_notices_do_not_hide_the_next_valid_one(tmp_path):
    async with service() as server:
        case = remote_case(tmp_path, server.url)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        ready = asyncio.Event()
        stream = board.notifications("main-a", ready.set)
        next_notice = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            assert not next_notice.done() and not server.disconnected.is_set()
            await server.outgoing.put(b"event: unexpected\ndata: {}\n\n")
            await server.outgoing.put(b"event: notification\ndata: not-json\n\n")
            bad = [notice(board, recipient=UUID(MESSAGE)), notice(board, turn="other"),
                notice(board, post={**PREVIEW, "text_preview": "🙂" * 151})]
            for item in bad:
                await server.outgoing.put(("event: notification\r\ndata: " + encode(item) + "\r\n\r\n").encode())
            expected = {**PREVIEW, "text_preview": "🙂" * 150, "n_chars": 150}
            await server.outgoing.put(("event: notification\r\ndata: " + encode(notice(board, post=expected)) + "\r\n\r\n").encode())
            actual = await asyncio.wait_for(next_notice, 2)
            assert actual["text_preview"] == expected["text_preview"]
            request = server.requests[0]
            assert request["json"] == {"caller": str(board.caller), "turn_id": "main-a"}
            assert request["headers"]["authorization"] == "Bearer " + TOKEN
        finally:
            if not next_notice.done():
                next_notice.cancel()
            await asyncio.gather(next_notice, return_exceptions=True)
            await stream.aclose()
            await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
async def test_sse_split_utf8_and_line_endings_use_the_public_decoder(ending):
    payload = b"event: ready" + ending + b"data: " + "中文🙂".encode() + ending + ending
    response = httpx.Response(200, request=httpx.Request("POST", "https://board.test"), stream=Chunks([bytes([byte]) for byte in payload]))
    events = [event async for event in board_events(response)]
    assert len(events) == 1 and events[0].event == "ready" and events[0].data == "中文🙂"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,error", [(b"event: ready\ndata: \xff\n\n", UnicodeDecodeError),
    (b":" + b"x" * (MAX_BODY - 2) + b"\n\n", MessageBoardError)], ids=["invalid-utf8", "oversized-frame"])
async def test_sse_rejects_invalid_utf8_and_oversized_frames(payload, error):
    response = httpx.Response(200, request=httpx.Request("POST", "https://board.test"), stream=Chunks([payload]))
    with pytest.raises(error):
        _events = [event async for event in board_events(response)]


@pytest.mark.asyncio
async def test_sse_frame_limit_resets_at_complete_blank_lines():
    comment = b":" + b"x" * (MAX_BODY - 3) + b"\n\n"
    payload = comment + comment + b"event: ready\ndata: {}\n\n"
    response = httpx.Response(200, request=httpx.Request("POST", "https://board.test"), stream=Chunks([payload]))
    events = [event async for event in board_events(response)]
    assert len(events) == 1 and events[0].event == "ready"


@pytest.mark.asyncio
async def test_actual_receiver_is_active_turn_owned_and_only_retired_tasks_enter_cleanup(tmp_path):
    async with service() as server:
        case = remote_case(tmp_path, server.url, active=True)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        owner = case.root.run_context
        try:
            await asyncio.wait_for(start_remote_board_notifications(case.root), 2)
            key = ("tree-thread", "/root")
            turn, task, captured = case.runtime.message_boards.remote_receivers[key]
            assert turn == "main-a" and captured is owner and not task.done()
            assert not owner.lifecycle_cleanup_tasks
            await start_remote_board_notifications(case.root)
            assert len(server.requests) == 1
            post = {**PREVIEW, "author": "/root/worker"}
            await server.outgoing.put(("event: notification\ndata: " + encode(notice(board, post=post)) + "\n\n").encode())
            async with asyncio.timeout(2):
                while not owner.turn_input_queue.has_message_board_notifications():
                    await asyncio.sleep(.01)
            assert "/root/worker" in owner.turn_input_queue.take_message_board_notifications()[0]
            owner.turn_input_queue.begin_turn("later-turn")
            await server.outgoing.put(("event: notification\ndata: " + encode(notice(board, post=post)) + "\n\n").encode())
            await asyncio.sleep(.04)
            assert not owner.turn_input_queue.has_message_board_notifications()
            case.kernel.complete_run_record("completed")
            assert key not in case.runtime.message_boards.remote_receivers and task in owner.lifecycle_cleanup_tasks
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
            await asyncio.wait_for(server.disconnected.wait(), 2)
        finally:
            await close_case(case)


@pytest.mark.asyncio
async def test_failed_first_remote_attempt_releases_startup_and_reconnects_only_the_same_turn(tmp_path):
    async with service() as server:
        server.watch_rejections = 1
        case = remote_case(tmp_path, server.url, active=True)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        try:
            await asyncio.wait_for(start_remote_board_notifications(case.root), 3)
            key = ("tree-thread", "/root")
            turn, task, _owner = case.runtime.message_boards.remote_receivers[key]
            assert turn == "main-a" and len(server.requests) == 1 and not task.done()
            async with asyncio.timeout(3):
                while len(server.requests) < 2:
                    await asyncio.sleep(.01)
            await server.outgoing.put(("event: notification\ndata: " + encode(notice(board)) + "\n\n").encode())
            async with asyncio.timeout(2):
                while not case.root.run_context.turn_input_queue.has_message_board_notifications():
                    await asyncio.sleep(.01)
            assert all(request["json"] == {"caller": str(board.caller), "turn_id": "main-a"} for request in server.requests)
            assert all("last-event-id" not in request["headers"] for request in server.requests)
        finally:
            await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["x" * 1100, "中" * 300], ids=["ascii", "multibyte"])
async def test_remote_host_rejects_notice_metadata_over_the_source_context_budget(tmp_path, channel):
    async with service() as server:
        case = remote_case(tmp_path, server.url, active=True)
        board = RemoteAgentMessageBoard(BoardActor(case.root), case.root)
        owner = case.root.run_context
        try:
            await asyncio.wait_for(start_remote_board_notifications(case.root), 2)
            oversized = {**PREVIEW, "author": "/root/worker", "channel_name": channel}
            await server.outgoing.put(("event: notification\ndata: " + encode(notice(board, post=oversized)) + "\n\n").encode())
            await asyncio.sleep(.04)
            assert not owner.turn_input_queue.has_message_board_notifications()
            await server.outgoing.put(("event: notification\ndata: " + encode(notice(board)) + "\n\n").encode())
            async with asyncio.timeout(2):
                while not owner.turn_input_queue.has_message_board_notifications():
                    await asyncio.sleep(.01)
            assert "design" in owner.turn_input_queue.take_message_board_notifications()[0]
        finally:
            await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("flags,snapshot", [({}, False), ({}, True), ({"multi_agent_v2": True}, True),
    ({"agent_message_board": True}, True)])
async def test_unconfigured_or_disabled_receivers_do_not_read_credentials_or_open_http(tmp_path, monkeypatch, flags, snapshot):
    class NoEnvironment:
        def __getitem__(self, key):
            raise AssertionError("disabled receiver read a credential")
    monkeypatch.setattr(remote, "os", SimpleNamespace(environ=NoEnvironment()))
    monkeypatch.setattr(remote, "RemoteAgentMessageBoard", lambda *args: pytest.fail("disabled receiver constructed an HTTP client"))
    case = remote_case(tmp_path, "https://board.test", env="NEVER_READ")
    case.config.feature_flags = FeatureFlags(flags)
    captured = replace(case.snapshot, config=case.config) if snapshot else None
    case.root.run_context.model_execution = captured
    context = replace(case.root, model_execution=captured)
    try:
        await start_remote_board_notifications(context)
        assert not case.runtime.message_boards.remote_receivers
    finally:
        await close_case(case)
