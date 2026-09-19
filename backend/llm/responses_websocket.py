"""Responses WebSocket connections leased to individual provider requests.

One idle connection is retained per adapter. Concurrent requests own separate
connections; only the request parser can publish a successful replay baseline.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State


WEBSOCKET_BETA = "responses_websockets=2026-02-06"
TURN_STATE_HEADER = "x-codex-turn-state"
Owner = tuple[str, str, str, str]


def _replay_item(item: Any) -> Any:
    """Compare the message representation MiniCode actually sends on replay.

    Responses message IDs/status and output-text annotations aren't replayed
    by LLMMessage. Opaque reasoning and tool arguments retain exact equality.
    Unrecognized output types remain unmatched rather than being discarded.
    """
    if not isinstance(item, dict):
        return item
    if item.get("type") == "message" or item.get("role") == "assistant":
        content = item.get("content")
        if isinstance(content, list) and all(
            isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
            for part in content
        ):
            content = "".join(part["text"] for part in content)
        if isinstance(content, str):
            result = {"role": item.get("role", "assistant"), "content": content}
            if item.get("phase"):
                result["phase"] = item["phase"]
            return result
    if item.get("type") == "reasoning" and item.get("summary") == []:
        return {key: value for key, value in item.items() if key != "summary"}
    return item


def _request_properties(payload: dict[str, Any]) -> dict[str, Any]:
    # These metadata fields describe the current request, not model context.
    return {key: value for key, value in payload.items()
            if key not in {"input", "client_metadata", "metadata"}}


@dataclass
class _Connection:
    socket: ClientConnection
    identity: tuple[Any, ...]
    properties: dict[str, Any] = field(default_factory=dict)
    baseline: list[dict[str, Any]] = field(default_factory=list)
    response_id: str = ""
    turn_state: str = ""


class ResponsesWebSocketPool:
    def __init__(self) -> None:
        self.idle: _Connection | None = None
        self.active: set[ResponsesWebSocketRequest] = set()
        self.closed = False
        self.unavailable_status: int | None = None
        self.fallback_to_http = False

    @property
    def http_only(self) -> bool:
        return self.fallback_to_http or self.unavailable_status is not None

    def request(self, owner: Owner | None) -> ResponsesWebSocketRequest:
        if self.closed:
            raise RuntimeError("Responses WebSocket adapter is closed")
        request = ResponsesWebSocketRequest(self, owner)
        self.active.add(request)
        return request

    async def aclose(self) -> None:
        self.closed = True
        idle, self.idle = self.idle, None
        await asyncio.gather(
            *(request.aclose() for request in tuple(self.active)),
            *([idle.socket.close()] if idle is not None else []),
        )


class ResponsesWebSocketRequest:
    def __init__(self, pool: ResponsesWebSocketPool, owner: Owner | None) -> None:
        self.pool = pool
        self.owner = owner
        self.connection: _Connection | None = None
        self.closed = False
        self.accepted = False
        self.payload: dict[str, Any] = {}
        self.response: dict[str, Any] | None = None
        self.info: dict[str, Any] = {"mode": "websocket", "incremental": False, "connection_reused": False}

    async def events(
        self, payload: dict[str, Any], *, url: str, headers: dict[str, str],
        proxy: str | None, wire_payload_sink: dict[str, Any] | None,
        on_handshake: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        # The sticky turn token is response state, so it must not invalidate
        # the connection identity or force a fresh socket on the next sample.
        identity = (self.owner, url, proxy, tuple(sorted(
            (key, value) for key, value in headers.items()
            if key.lower() != TURN_STATE_HEADER
        )))
        connection, self.pool.idle = self.pool.idle, None
        # No suspension occurs while taking the idle connection. A second
        # request therefore opens its own socket instead of sharing recv().
        if connection is not None and (
            self.owner is None or connection.identity != identity or connection.socket.state != State.OPEN
        ):
            self.connection = connection
            await connection.socket.close()
            self.connection = None
            connection = None
        self.info["connection_reused"] = connection is not None
        if connection is None:
            socket = await connect(
                url, additional_headers=headers, proxy=proxy,
                max_size=None, max_queue=1, open_timeout=10, close_timeout=1,
            )
            if self.closed or self.pool.closed:
                await socket.close()
                raise RuntimeError("Responses WebSocket request was closed during connection setup")
            connection = _Connection(socket, identity)
        # The token received from the previous handshake belongs on this
        # request. A token learned from the current handshake starts with the
        # next request, matching the upstream turn-state contract.
        request_turn_state = connection.turn_state
        self.connection = connection
        handshake_headers = (
            {TURN_STATE_HEADER: connection.turn_state}
            if self.info["connection_reused"] and connection.turn_state
            else {} if self.info["connection_reused"]
            else dict(connection.socket.response.headers)
        )
        turn_state = str(handshake_headers.get(TURN_STATE_HEADER) or "").strip()
        if turn_state:
            connection.turn_state = turn_state
            self.info["turn_state_present"] = True
        await on_handshake(connection.socket.response.status_code, handshake_headers)
        self.payload = payload
        inputs = payload.get("input", [])
        comparable = [_replay_item(item) for item in inputs]
        baseline = connection.baseline
        incremental = bool(
            connection.response_id
            and "previous_response_id" not in payload and "conversation" not in payload
            and connection.properties == _request_properties(payload)
            and len(comparable) >= len(baseline)
            and comparable[:len(baseline)] == baseline
        )
        wire = {**payload, "type": "response.create"}
        if request_turn_state:
            client_metadata = dict(wire.get("client_metadata") or {})
            client_metadata[TURN_STATE_HEADER] = request_turn_state
            wire["client_metadata"] = client_metadata
        if incremental:
            wire["previous_response_id"] = connection.response_id
            wire["input"] = inputs[len(baseline):]
        encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
        self.info.update({
            "incremental": incremental, "input_items_logical_len": len(inputs),
            "input_items_sent_len": len(wire.get("input", [])),
            "request_json_bytes": len(encoded.encode("utf-8")),
        })
        if wire_payload_sink is not None:
            wire_payload_sink.clear()
            wire_payload_sink.update(wire)
        try:
            await connection.socket.send(encoded)
            while True:
                raw = await connection.socket.recv()
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("provider_error_type=protocol: Responses WebSocket event must be an object")
                event_type = event.get("type", "")
                response = event.get("response")
                error = event.get("error") or (response.get("error") if isinstance(response, dict) else None) or event
                if incremental and error.get("code") == "previous_response_id_not_found":
                    # The server rejected this continuation. Clearing this
                    # lease lets the harness's existing retry use full input.
                    raise httpx.ReadError("Responses WebSocket connection lost continuation state; retry with full context")
                if event_type == "response.completed":
                    self.response = event.get("response")
                yield event
                if event_type in {"response.completed", "response.incomplete", "response.failed", "error", "response.error"}:
                    break
        except ConnectionClosed as exc:
            close_code = exc.rcvd.code if exc.rcvd is not None else 1006
            failure = f"Responses WebSocket connection closed before request completion (WebSocket code {close_code})"
        else:
            return
        # Translate at the transport boundary. Generic provider-error parsing
        # must not interpret the deprecated WebSocket .code as an API code.
        raise httpx.ReadError(failure)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pool.active.discard(self)
        connection, self.connection = self.connection, None
        if connection is None:
            return
        if (self.accepted and self.owner is not None and not self.pool.closed
                and self.response and isinstance(self.response.get("id"), str)
                and self.response["id"] and connection.socket.state == State.OPEN):
            connection.properties = _request_properties(self.payload)
            connection.baseline = [_replay_item(item) for item in
                                   [*self.payload.get("input", []), *self.response.get("output", [])]]
            connection.response_id = self.response["id"]
            previous, self.pool.idle = self.pool.idle, connection
            if previous is not None:
                await previous.socket.close()
        else:
            await connection.socket.close()
