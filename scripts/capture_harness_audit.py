"""Exercise the production backend over HTTP/SSE and WebSocket, retaining the wire.

The local provider is deterministic fault injection, not a real-model quality or
cache-hit benchmark. No backend classes or frontend event payloads are mocked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import websockets


ROOT = Path(__file__).resolve().parents[1]
LONG_ANSWER = "AUDIT_BEGIN\n" + "完整长回复 abcdefghijklmnopqrstuvwxyz\n" * 800 + "AUDIT_END"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Provider(BaseHTTPRequestHandler):
    requests: list[dict] = []
    tool_count = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "audit-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(request)
        messages = request.get("messages", [])
        last_user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
        answer = LONG_ANSWER if "AUDIT_LONG" in last_user else "AUDIT_SHORT_OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.tool_count and "AUDIT_LONG" in last_user and messages[-1]["role"] == "user":
            for index in range(self.tool_count):
                event = {"id": "chatcmpl-audit", "object": "chat.completion.chunk", "created": 1, "model": "audit-model",
                         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "id": f"audit_read_{index}", "type": "function",
                             "function": {"name": "read_file", "arguments": json.dumps({"file_path": "README.md"})}}]}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            end = {"id": "chatcmpl-audit", "object": "chat.completion.chunk", "created": 1, "model": "audit-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
            self.wfile.write(("data: " + json.dumps(end) + "\n\ndata: [DONE]\n\n").encode())
            return
        for offset in range(0, len(answer), 1000):
            event = {"id": "chatcmpl-audit", "object": "chat.completion.chunk", "created": 1,
                     "model": "audit-model", "choices": [{"index": 0, "delta": {"content": answer[offset:offset + 1000]}, "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
        end = {"id": "chatcmpl-audit", "object": "chat.completion.chunk", "created": 1, "model": "audit-model",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(("data: " + json.dumps(end) + "\n\ndata: [DONE]\n\n").encode())


async def capture(out: Path, turns: int, *, snapshot_during_turn: bool = False, turn_timeout: float = 60) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    run_dir = out.parent / (out.stem + "-runtime")
    state = run_dir / "state"
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("Harness audit repeated read evidence.\n", encoding="utf-8")
    (state / "data").mkdir(parents=True, exist_ok=True)
    (state / "data" / "trusted_workspaces.json").write_text(json.dumps({"version": 1, "roots": [str(workspace)]}), encoding="utf-8")
    provider_port, backend_port = free_port(), free_port()
    server = ThreadingHTTPServer(("127.0.0.1", provider_port), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = {"llm": {"provider": "custom", "custom": {"base_url": f"http://127.0.0.1:{provider_port}/v1", "model": "audit-model", "wire_api": "chat"}}}
    (state / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1", MINICODE_STATE_ROOT=str(state),
               MINICODE_RUNTIME_TOKEN="", LLM_PROVIDER="custom", CUSTOM_API_KEY="audit-local-key",
               CUSTOM_BASE_URL=f"http://127.0.0.1:{provider_port}/v1", CUSTOM_MODEL="audit-model", CUSTOM_WIRE_API="chat")
    connections: list[dict] = []
    conversation = "conv_audit_" + str(time.time_ns())
    session = "session_audit_" + str(time.time_ns())
    fixture = {"conversation_id": conversation, "workspace_root": str(workspace), "expected_answer": LONG_ANSWER, "connections": connections}
    with (run_dir / "backend.log").open("w", encoding="utf-8") as log:
        print(f"backend_port={backend_port}", flush=True)
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", os.environ.get("MINICODE_AUDIT_APP", "backend.main:app"), "--app-dir", str(ROOT / ".tmp"), "--host", "127.0.0.1", "--port", str(backend_port)],
                                   cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(180):
                    if process.poll() is not None:
                        raise RuntimeError(f"backend exited: {process.returncode}; see {log.name}")
                    try:
                        response = await client.get(f"http://127.0.0.1:{backend_port}/api/health", timeout=1)
                        if response.status_code < 500:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.25)
                else:
                    raise TimeoutError("backend startup")

            uri = f"ws://127.0.0.1:{backend_port}/ws?session_id={session}&protocol=control_v1"
            async def send(ws, connection, command):
                command = dict(command, client_command_id=f"cmd_audit_{time.time_ns()}")
                connection["commands"].append({"sent_after_events": len(connection["events"]), "command": command})
                await ws.send(json.dumps(command))
                return command["client_command_id"]

            async def until(ws, connection, predicate):
                async with asyncio.timeout(turn_timeout):
                    while True:
                        raw = await ws.recv()
                        event = json.loads(raw)
                        connection["events"].append(event)
                        if snapshot_during_turn and event.get("type") == "agent.run.started":
                            await send(ws, connection, {"type": "conversation.list"})
                        if predicate(event):
                            return event

            connection = {"events": [], "commands": []}
            connections.append(connection)
            async with websockets.connect(uri, origin="file://", max_size=None) as ws:
                command_id = await send(ws, connection, {"type": "conversation.create", "conversation_id": conversation,
                    "title": "Harness audit", "workspace_root": str(workspace), "permission_mode": "bypass"})
                await until(ws, connection, lambda e: e.get("type") == "command.result" and e.get("client_command_id") == command_id)
                for turn in range(turns):
                    await send(ws, connection, {"type": "user_message", "content": "AUDIT_LONG" if turn == 0 else f"AUDIT_SHORT {turn}",
                        "conversation_id": conversation, "assistant_message_id": f"a_audit_{turn}", "user_message_id": f"u_audit_{turn}",
                        "workspace_root": str(workspace), "permission_mode": "bypass", "agent_mode": "build"})
                    await until(ws, connection, lambda e: e.get("type") == "done" and e.get("conversation_id") == conversation)
                    print(f"turn {turn + 1}/{turns}: {len(connection['events'])} events", flush=True)
                # The run-owned task settles after done; capture the terminal
                # runtime update too, as a connected renderer would receive it.
                try:
                    await asyncio.wait_for(until(ws, connection, lambda _e: False), timeout=2)
                except TimeoutError:
                    pass
                # Replay from the accepted first input, before its answer.
                cursor = next(e["seq"] for e in connection["events"] if e.get("type") == "agent.run.started")
            await asyncio.sleep(0.3)
            connection = {"events": [], "commands": []}
            connections.append(connection)
            async with websockets.connect(uri, origin="file://", max_size=None) as ws:
                await send(ws, connection, {"type": "session.restore", "last_seq": cursor, "last_conversation_id": conversation})
                await until(ws, connection, lambda e: e.get("type") == "session.replay" or e.get("type") == "conversation.switched")
                try:
                    async with asyncio.timeout(3):
                        while True:
                            connection["events"].append(json.loads(await ws.recv()))
                except TimeoutError:
                    pass
        finally:
            if os.environ.get("MINICODE_AUDIT_APP"):
                async with httpx.AsyncClient() as client:
                    profile = await client.get(f"http://127.0.0.1:{backend_port}/__audit_profile", timeout=10)
                    out.with_suffix(".profile.json").write_text(profile.text, encoding="utf-8")
            out.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
            out.with_suffix(".requests.json").write_text(json.dumps(Provider.requests, ensure_ascii=False, indent=2), encoding="utf-8")
            process.terminate()
            await asyncio.to_thread(process.wait, 15)
            server.shutdown()
            server.server_close()
    replay = [e for c in connections for e in c["events"] if e.get("type") == "session.replay"]
    truncated = [e for r in replay for e in r["events"] if e.get("replay_truncated_fields")]
    print(json.dumps({"requests": len(Provider.requests), "wire_events": sum(len(c["events"]) for c in connections),
                      "replayed_events": sum(len(r["events"]) for r in replay), "truncated_events": len(truncated),
                      "truncated_types": sorted(set(e["type"] for e in truncated)), "out": str(out)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--tool-calls", type=int, default=0)
    parser.add_argument("--snapshot-during-turn", action="store_true")
    parser.add_argument("--turn-timeout", type=float, default=60)
    args = parser.parse_args()
    Provider.tool_count = args.tool_calls
    async def main():
        async with asyncio.timeout(480):
            await capture(args.out.resolve(), args.turns, snapshot_during_turn=args.snapshot_during_turn, turn_timeout=args.turn_timeout)
    asyncio.run(main())
