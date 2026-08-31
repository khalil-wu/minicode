"""Loopback-only OpenAI Responses and Anthropic Messages test provider.

The fixture serves deterministic, protocol-shaped streams for browser, agent
loop, reconnect, and load tests.  It never forwards requests and deliberately
places sentinel provider payloads in code/arguments so UI tests can prove that
only safe character counts are projected.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


AUDIT_MARKER = "PROVIDER_ACTIVITY_BROWSER_AUDIT"
OPENAI_ARGUMENT_SENTINEL = "DO_NOT_PROJECT_ARGUMENT_BODY_openai_42"
OPENAI_CODE_SENTINEL = "DO_NOT_PROJECT_CODE_BODY_openai_84"
ANTHROPIC_INPUT_SENTINEL = "DO_NOT_PROJECT_INPUT_BODY_anthropic_21"
ANTHROPIC_MCP_INPUT_SENTINEL = "DO_NOT_PROJECT_MCP_INPUT_BODY_anthropic_63"
ANTHROPIC_FILE_ID_SENTINEL = "file_DO_NOT_PROJECT_ID_BODY_anthropic_77"
ANTHROPIC_CITED_TEXT_SENTINEL = "DO_NOT_PROJECT_CITED_TEXT_BODY_anthropic_99"
OPENAI_ANSWER = "MiniCode 托管活动与引用投影已完成。"
ANTHROPIC_ANSWER = "MiniCode 托管活动与引用投影已完成。"
# Per-provider citation URLs. They must stay distinct so a projection test can
# prove each adapter surfaced *its own* citation rather than the other's.
# (The previous ``codex``/``claude`` spellings were foreign-harness names.)
OPENAI_CITATION_URL = "https://example.test/minicode-openai-provider-source"
ANTHROPIC_CITATION_URL = "https://example.test/minicode-anthropic-provider-source"


class ProviderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[str] = Counter()
        self._active = 0
        self._peak_active = 0

    def begin(self, route: str) -> None:
        with self._lock:
            self._requests[route] += 1
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)

    def end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": dict(self._requests),
                "active": self._active,
                "peak_active": self._peak_active,
            }

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()
            self._active = 0
            self._peak_active = 0


STATE = ProviderState()


def _contains_marker(value: Any) -> bool:
    if isinstance(value, str):
        return AUDIT_MARKER in value
    if isinstance(value, list):
        return any(_contains_marker(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_marker(item) for item in value.values())
    return False


def _is_main_agent_request(payload: dict[str, Any]) -> bool:
    tools = payload.get("tools")
    return _contains_marker(payload) and isinstance(tools, list) and bool(tools)


def _openai_response(
    *,
    response_id: str,
    message_id: str,
    text: str,
    output: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": "gpt-5.5-audit",
        "output": output
        if output is not None
        else [
            {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 32,
            "output_tokens": 12,
            "input_tokens_details": {"cached_tokens": 7},
            "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 44,
        },
    }


def _openai_simple_events() -> list[dict[str, Any]]:
    text = "本地协议测试"
    response = _openai_response(
        response_id="resp_side_audit",
        message_id="msg_side_audit",
        text=text,
    )
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {
                **response,
                "status": "in_progress",
                "output": [],
                "usage": None,
            },
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "response_id": response["id"],
            "item_id": "msg_side_audit",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 2,
            "response_id": response["id"],
            "item_id": "msg_side_audit",
            "output_index": 0,
            "content_index": 0,
            "text": text,
            "annotations": [],
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": response,
        },
    ]


def _openai_audit_events() -> list[dict[str, Any]]:
    response_id = "resp_provider_projection_audit"
    web_item = {
        "id": "web_audit_1",
        "type": "web_search_call",
        "status": "in_progress",
        "action": {"type": "search", "query": "MiniCode provider projection"},
    }
    code_item = {
        "id": "code_audit_1",
        "type": "code_interpreter_call",
        "status": "in_progress",
        "code": "",
        "container_id": "container_audit_1",
        "outputs": [],
    }
    mcp_item = {
        "id": "mcp_audit_1",
        "type": "mcp_call",
        "status": "in_progress",
        "server_label": "audit-local",
        "name": "lookup_release_signal",
        "arguments": OPENAI_ARGUMENT_SENTINEL,
        "output": None,
        "error": None,
    }
    message_item = {
        "id": "msg_provider_projection_audit",
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "phase": "final_answer",
        "content": [],
    }
    citation = {
        "type": "url_citation",
        "url": OPENAI_CITATION_URL,
        "title": "MiniCode provider source",
        "start_index": 0,
        "end_index": 5,
    }
    completed_message = {
        **message_item,
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": OPENAI_ANSWER,
                "annotations": [citation],
            }
        ],
    }
    response = _openai_response(
        response_id=response_id,
        message_id=completed_message["id"],
        text=OPENAI_ANSWER,
        output=[completed_message],
    )
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {
                **response,
                "status": "in_progress",
                "output": [],
                "usage": None,
            },
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "response_id": response_id,
            "output_index": 0,
            "item": web_item,
        },
        {
            "type": "response.web_search_call.searching",
            "sequence_number": 2,
            "response_id": response_id,
            "item_id": web_item["id"],
            "output_index": 0,
        },
        {
            "type": "response.web_search_call.completed",
            "sequence_number": 3,
            "response_id": response_id,
            "item_id": web_item["id"],
            "output_index": 0,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 4,
            "response_id": response_id,
            "output_index": 0,
            "item": {**web_item, "status": "completed"},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 5,
            "response_id": response_id,
            "output_index": 1,
            "item": code_item,
        },
        {
            "type": "response.code_interpreter_call_code.delta",
            "sequence_number": 6,
            "response_id": response_id,
            "item_id": code_item["id"],
            "output_index": 1,
            "delta": OPENAI_CODE_SENTINEL,
        },
        {
            "type": "response.code_interpreter_call_code.done",
            "sequence_number": 7,
            "response_id": response_id,
            "item_id": code_item["id"],
            "output_index": 1,
            "code": OPENAI_CODE_SENTINEL,
        },
        {
            "type": "response.code_interpreter_call.interpreting",
            "sequence_number": 8,
            "response_id": response_id,
            "item_id": code_item["id"],
            "output_index": 1,
        },
        {
            "type": "response.code_interpreter_call.completed",
            "sequence_number": 9,
            "response_id": response_id,
            "item_id": code_item["id"],
            "output_index": 1,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 10,
            "response_id": response_id,
            "output_index": 1,
            "item": {**code_item, "status": "completed", "code": OPENAI_CODE_SENTINEL},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 11,
            "response_id": response_id,
            "output_index": 2,
            "item": mcp_item,
        },
        {
            "type": "response.mcp_call_arguments.delta",
            "sequence_number": 12,
            "response_id": response_id,
            "item_id": mcp_item["id"],
            "output_index": 2,
            "delta": OPENAI_ARGUMENT_SENTINEL,
        },
        {
            "type": "response.mcp_call_arguments.done",
            "sequence_number": 13,
            "response_id": response_id,
            "item_id": mcp_item["id"],
            "output_index": 2,
            "arguments": OPENAI_ARGUMENT_SENTINEL,
        },
        {
            "type": "response.mcp_call.completed",
            "sequence_number": 14,
            "response_id": response_id,
            "item_id": mcp_item["id"],
            "output_index": 2,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 15,
            "response_id": response_id,
            "output_index": 2,
            "item": {**mcp_item, "status": "completed", "output": "ok"},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 16,
            "response_id": response_id,
            "output_index": 3,
            "item": message_item,
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 17,
            "response_id": response_id,
            "item_id": message_item["id"],
            "output_index": 3,
            "content_index": 0,
            "delta": OPENAI_ANSWER,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 18,
            "response_id": response_id,
            "item_id": message_item["id"],
            "output_index": 3,
            "content_index": 0,
            "text": OPENAI_ANSWER,
            "annotations": [citation],
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 19,
            "response_id": response_id,
            "output_index": 3,
            "item": completed_message,
        },
        {
            "type": "response.completed",
            "sequence_number": 20,
            "response": response,
        },
    ]


def _anthropic_simple_events() -> list[dict[str, Any]]:
    text = "本地协议测试"
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_anthropic_side_audit",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-opus-audit",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]


def _anthropic_audit_events() -> list[dict[str, Any]]:
    citation = {
        "type": "web_search_result_location",
        "url": ANTHROPIC_CITATION_URL,
        "title": "MiniCode provider source",
        "cited_text": "provider projection",
    }
    document_citation = {
        "type": "page_location",
        "cited_text": ANTHROPIC_CITED_TEXT_SENTINEL,
        "document_index": 0,
        "document_title": "MiniCode release report",
        "file_id": ANTHROPIC_FILE_ID_SENTINEL,
        "start_page_number": 2,
        "end_page_number": 4,
    }
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_anthropic_provider_projection_audit",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-opus-audit",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 29,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 11,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 1,
                        "ephemeral_1h_input_tokens": 2,
                    },
                    "server_tool_use": {
                        "web_search_requests": 1,
                        "web_fetch_requests": 0,
                    },
                    "service_tier": "priority",
                    "inference_geo": "local",
                },
                "container": {
                    "id": "container_anthropic_audit_1",
                    "expires_at": "2026-08-17T00:00:00Z",
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "anthropic_web_audit_1",
                "name": "web_search",
                "input": {"query": ANTHROPIC_INPUT_SENTINEL},
            },
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "web_search_tool_result",
                "tool_use_id": "anthropic_web_audit_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": citation["url"],
                        "title": citation["title"],
                        "encrypted_content": "opaque-provider-result",
                        "page_age": "2026-08-16",
                    }
                ],
            },
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "container_upload",
                "file_id": ANTHROPIC_FILE_ID_SENTINEL,
            },
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {
                "type": "mcp_tool_use",
                "id": "anthropic_mcp_audit_1",
                "name": "lookup_release_signal",
                "server_name": "audit-local",
                "input": {"query": ANTHROPIC_MCP_INPUT_SENTINEL},
            },
        },
        {"type": "content_block_stop", "index": 3},
        {
            "type": "content_block_start",
            "index": 4,
            "content_block": {
                "type": "mcp_tool_result",
                "tool_use_id": "anthropic_mcp_audit_1",
                "is_error": False,
                "content": [{"type": "text", "text": "release signal ready"}],
            },
        },
        {"type": "content_block_stop", "index": 4},
        {
            "type": "content_block_start",
            "index": 5,
            "content_block": {
                "type": "text",
                "text": "",
                "citations": [citation, document_citation],
            },
        },
        {
            "type": "content_block_delta",
            "index": 5,
            "delta": {"type": "text_delta", "text": ANTHROPIC_ANSWER},
        },
        {
            "type": "content_block_delta",
            "index": 5,
            "delta": {"type": "citations_delta", "citation": citation},
        },
        {
            "type": "content_block_delta",
            "index": 5,
            "delta": {"type": "citations_delta", "citation": document_citation},
        },
        {"type": "content_block_stop", "index": 5},
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "container": {
                    "id": "container_anthropic_audit_1",
                    "expires_at": "2026-08-17T00:00:00Z",
                },
            },
            "usage": {
                "output_tokens": 14,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 11,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1,
                    "ephemeral_1h_input_tokens": 2,
                },
                "server_tool_use": {
                    "web_search_requests": 1,
                    "web_fetch_requests": 0,
                },
                "service_tier": "priority",
                "inference_geo": "local",
            },
        },
        {"type": "message_stop"},
    ]


class FakeProviderHandler(BaseHTTPRequestHandler):
    server_version = "MiniCodeFakeProvider/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _read_json(self) -> dict[str, Any] | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if content_length <= 0 or content_length > 8 * 1024 * 1024:
            return None
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _send_sse(self, events: list[dict[str, Any]]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.end_headers()
        for index, event in enumerate(events):
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.wfile.write(b"data: " + encoded + b"\n\n")
            self.wfile.flush()
            if index in {1, 5, 11}:
                # Long enough for a real renderer to observe the running state,
                # while still keeping the fixture fast for concentrated tests.
                time.sleep(0.35)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send_json({"ok": True, "service": "fake-provider"})
            return
        if self.path == "/stats":
            self._send_json(STATE.snapshot())
            return
        self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = self.path.split("?", 1)[0]
        if route not in {"/v1/responses", "/v1/messages"}:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        payload = self._read_json()
        if payload is None:
            self._send_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return

        STATE.begin(route)
        try:
            is_main = _is_main_agent_request(payload)
            if route == "/v1/responses":
                if payload.get("stream") is False:
                    response = _openai_response(
                        response_id="resp_nonstream_audit",
                        message_id="msg_nonstream_audit",
                        text="本地协议测试",
                    )
                    self._send_json(response)
                else:
                    self._send_sse(
                        _openai_audit_events() if is_main else _openai_simple_events()
                    )
                return

            if payload.get("stream") is False:
                self._send_json(
                    {
                        "id": "msg_anthropic_nonstream_audit",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-audit",
                        "content": [{"type": "text", "text": "本地协议测试"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 4},
                    }
                )
            else:
                self._send_sse(
                    _anthropic_audit_events() if is_main else _anthropic_simple_events()
                )
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            STATE.end()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MiniCode loopback fake provider")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), FakeProviderHandler)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
