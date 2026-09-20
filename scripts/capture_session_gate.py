"""Capture a real backend WebSocket session for the renderer replay gate.

Drives a running backend through the lifecycle the desktop renderer performs:

    connection 0: launch -> conversation.create (workspace bound)
                  -> user_message (tool turn) -> user_message while running:
                  conversation.create (activates the new one) ->
                  conversation.switch back -> interrupt (turn fenced)
                  -> socket dropped
    connection 1: reconnect -> session.restore(last_seq, last_conversation_id)
                  -> user_message -> done

Every client command and every server event is recorded verbatim, per
connection, into a JSON fixture that
``frontend/src.v2/hooks/useWebSocket.session-gate.test.tsx`` feeds back to the
real renderer transport hook.

Usage (backend already listening; see docs/session-gate.md):

    python scripts/capture_session_gate.py --port 8123 \
        --workspace C:/path/to/workspace \
        --out frontend/src.v2/hooks/__fixtures__session_gate.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import websockets

# Mirror of frontend/src.v2/hooks/useWebSocket.ts NON_REPLAYABLE_CURSOR_EVENT_TYPES.
# scripts/check-protocol-sync.py keeps that set equal to the backend's; this
# copy only exists so the capture can predict the renderer's replay cursor.
NON_REPLAYABLE_CURSOR_EVENT_TYPES = {
    "agent_message.delta",
    "agent.item.delta",
    "artifact_content",
    "commands.list",
    "conversation.list",
    "conversation.switched",
    "llm.model.updated",
    "mcp_status",
    "pong",
    "runtime.capabilities",
    "session.restored",
    "session.synced",
    "stream_event",
    "stream_resume",
    "tool_output_delta",
}


def _is_transient_provider_reasoning(event: dict[str, Any]) -> bool:
    source = str(event.get("source") or "").strip().lower()
    reasoning_type = str(
        event.get("providerReasoningType") or event.get("provider_reasoning_type") or ""
    ).strip().lower()
    is_reasoning = (
        source in {"provider", "reasoning"}
        or bool(reasoning_type)
        or event.get("is_raw_provider_reasoning") is True
        or event.get("isRawProviderReasoning") is True
    )
    return is_reasoning and reasoning_type != "reasoning_summary_text"


def advances_replay_cursor(event: dict[str, Any]) -> bool:
    conversation_id = event.get("conversation_id")
    seq = event.get("seq")
    return (
        isinstance(conversation_id, str)
        and bool(conversation_id.strip())
        and isinstance(seq, int)
        and not isinstance(seq, bool)
        and seq >= 0
        and str(event.get("type")) not in NON_REPLAYABLE_CURSOR_EVENT_TYPES
        and not str(event.get("type")).startswith("session.")
        and not _is_transient_provider_reasoning(event)
    )


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if k in {"api_key", "image_api_key"} and v else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


class Connection:
    def __init__(self, index: int, ws: Any) -> None:
        self.index = index
        self.ws = ws
        self.commands: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.cursor = 0

    async def send(self, command: dict[str, Any]) -> str:
        command = dict(command)
        command.setdefault("client_command_id", "cmd_" + uuid.uuid4().hex)
        # Position lets the replay drive the renderer at the same point in the
        # event stream where the real client acted.
        self.commands.append({"sent_after_events": len(self.events), "command": command})
        await self.ws.send(json.dumps(command, ensure_ascii=False))
        return command["client_command_id"]

    async def recv_until(
        self,
        predicate: Any,
        *,
        timeout: float,
        label: str,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"[{label}] timeout after {timeout}s", file=sys.stderr)
                return None
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                print(f"[{label}] timeout after {timeout}s", file=sys.stderr)
                return None
            event = json.loads(raw)
            self.events.append(event)
            if advances_replay_cursor(event):
                self.cursor = max(self.cursor, int(event["seq"]))
            brief = {
                k: event.get(k)
                for k in ("seq", "type", "conversation_id", "status", "client_command_type", "turn_id")
                if k in event
            }
            print(f"[c{self.index}] {json.dumps(brief, ensure_ascii=False)}")
            if predicate(event):
                return event

    async def drain(self, seconds: float, label: str) -> None:
        await self.recv_until(lambda _e: False, timeout=seconds, label=label)


def _is_done_for(conversation_id: str) -> Any:
    return lambda e: e.get("type") == "done" and e.get("conversation_id") == conversation_id


def _is_result_for(command_id: str) -> Any:
    return lambda e: e.get("type") == "command.result" and e.get("client_command_id") == command_id


async def capture(port: int, workspace: str, out: Path, prompts: dict[str, str]) -> None:
    session_id = "session_" + uuid.uuid4().hex
    conv_a = "conv_gate_" + uuid.uuid4().hex[:8]
    conv_b = "conv_gate_" + uuid.uuid4().hex[:8]
    uri = f"ws://127.0.0.1:{port}/ws?session_id={session_id}&protocol=control_v1"
    connections: list[Connection] = []
    turn_id_second = ""
    assistant_ids = {"first": "a_gate_1", "second": "a_gate_2", "third": "a_gate_3"}
    user_ids = {"first": "u_gate_1", "second": "u_gate_2", "third": "u_gate_3"}

    def user_message(key: str, conversation_id: str) -> dict[str, Any]:
        return {
            "type": "user_message",
            "content": prompts[key],
            "workspace_root": workspace,
            "permission_mode": "bypass",
            "agent_mode": "build",
            "conversation_id": conversation_id,
            "assistant_message_id": assistant_ids[key],
            "user_message_id": user_ids[key],
        }

    # ── connection 0: launch, tool turn, create/switch during run, interrupt ──
    async with websockets.connect(uri, max_size=None, origin="file://") as ws:
        c0 = Connection(0, ws)
        connections.append(c0)
        # Fresh renderer launch: no persisted conversation, so no session.restore.
        await c0.send({"type": "conversation.list"})
        await c0.send({"type": "commands.list"})
        await c0.send({"type": "skills.list"})
        await c0.drain(3, "launch")

        create_a = await c0.send({
            "type": "conversation.create",
            "conversation_id": conv_a,
            "title": "New chat",
            "conversation_type": "main",
            "workspace_root": workspace,
            "permission_mode": "bypass",
        })
        assert await c0.recv_until(_is_result_for(create_a), timeout=20, label="create A")
        await c0.drain(2, "after create A")

        await c0.send(user_message("first", conv_a))
        assert await c0.recv_until(_is_done_for(conv_a), timeout=300, label="turn 1")
        await c0.drain(2, "after turn 1")

        await c0.send(user_message("second", conv_a))

        def _run_started(e: dict[str, Any]) -> bool:
            return (
                e.get("conversation_id") == conv_a
                and bool(e.get("turn_id"))
                and e.get("type") in {"agent.run.started", "item.started", "agent.progress", "tool_call"}
            )

        started = await c0.recv_until(_run_started, timeout=60, label="turn 2 start")
        assert started, "turn 2 never started"
        turn_id_second = str(started.get("turn_id"))
        await c0.drain(1.5, "turn 2 streaming")

        create_b = await c0.send({
            "type": "conversation.create",
            "conversation_id": conv_b,
            "title": "New chat",
            "conversation_type": "main",
            "workspace_root": workspace,
            "permission_mode": "bypass",
        })
        assert await c0.recv_until(_is_result_for(create_b), timeout=20, label="create B during run")
        await c0.drain(1, "after create B")

        switch_a = await c0.send({"type": "conversation.switch", "conversation_id": conv_a})
        await c0.recv_until(
            lambda e: e.get("type") == "conversation.switched" and e.get("client_command_id") == switch_a,
            timeout=20,
            label="switch back to A",
        )
        await c0.drain(1, "after switch A")

        await c0.send({
            "type": "interrupt",
            "conversation_id": conv_a,
            "turn_id": turn_id_second,
            "message_id": assistant_ids["second"],
        })
        assert await c0.recv_until(_is_done_for(conv_a), timeout=60, label="interrupt")
        await c0.drain(2, "after interrupt")

    # ── connection 1: reconnect with the renderer's durable cursor ──
    async with websockets.connect(uri, max_size=None, origin="file://") as ws:
        c1 = Connection(1, ws)
        connections.append(c1)
        restore = await c1.send({
            "type": "session.restore",
            "last_seq": c0.cursor,
            "last_conversation_id": conv_a,
            "last_workspace_root": workspace,
        })
        await c1.send({"type": "commands.list"})
        await c1.send({"type": "skills.list"})
        await c1.recv_until(
            lambda e: e.get("type") == "conversation.switched" and e.get("client_command_id") == restore,
            timeout=30,
            label="restore",
        )
        await c1.send({"type": "conversation.list"})
        await c1.drain(3, "after restore")

        await c1.send(user_message("third", conv_a))
        assert await c1.recv_until(_is_done_for(conv_a), timeout=300, label="turn 3")
        await c1.drain(2, "after turn 3")

    fixture = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session_id": session_id,
        "workspace_root": workspace,
        "conversation_a": conv_a,
        "conversation_b": conv_b,
        "turn_id_second": turn_id_second,
        "assistant_message_ids": assistant_ids,
        "user_message_ids": user_ids,
        "prompts": prompts,
        "connections": [
            {
                "index": c.index,
                "commands": scrub(c.commands),
                "events": scrub(c.events),
                "cursor_after": c.cursor,
            }
            for c in connections
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out}: " + ", ".join(f"c{c.index}={len(c.events)} events cursor={c.cursor}" for c in connections))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prompts = {
        "first": "Run pytest in this workspace, find why it fails, fix the bug in calc.py, then rerun pytest and report.",
        "second": "Read README.md, calc.py and test_calc.py one at a time with the read tool, then explain each function in detail and finally propose three additional tests.",
        "third": "Reply with the single word OK.",
    }
    asyncio.run(capture(args.port, args.workspace, args.out, prompts))


if __name__ == "__main__":
    main()
