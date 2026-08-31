from __future__ import annotations

import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

from backend.agent.provider_protocol import provider_raw_for_projection
from backend.config import LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, StreamEvent, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tools.base import ToolSchema
from tests.fixtures.fake_provider_server import (
    ANTHROPIC_CITATION_URL,
    ANTHROPIC_CITED_TEXT_SENTINEL,
    ANTHROPIC_FILE_ID_SENTINEL,
    ANTHROPIC_INPUT_SENTINEL,
    ANTHROPIC_MCP_INPUT_SENTINEL,
    AUDIT_MARKER,
    FakeProviderHandler,
    OPENAI_ARGUMENT_SENTINEL,
    OPENAI_CITATION_URL,
    OPENAI_CODE_SENTINEL,
    STATE,
)


async def _collect(adapter, messages, tools) -> list[StreamEvent]:
    return [event async for event in adapter.stream_chat(messages, tools)]


def test_loopback_provider_streams_survive_concurrent_load_and_project_only_safe_trace() -> None:
    """Exercise real raw-HTTP adapters without using any external credential."""

    STATE.reset()
    before = STATE.snapshot()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    base_url = f"http://127.0.0.1:{port}/v1"

    openai = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="loopback-fake-key",
            model="gpt-5.5-audit",
            base_url=base_url,
            wire_api="responses",
        )
    )
    anthropic = AnthropicAdapter(
        api_key="loopback-fake-key",
        model="claude-opus-audit",
        base_url=base_url,
            )
    messages = [LLMMessage(role="user", content=AUDIT_MARKER)]
    tools = [
        ToolSchema(
            name="read_file",
            description="Read one workspace file.",
            parameters={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ).to_openai_tool()
    ]

    async def exercise() -> tuple[list[list[StreamEvent]], list[list[StreamEvent]]]:
        try:
            openai_runs, anthropic_runs = await asyncio.gather(
                asyncio.gather(*[_collect(openai, messages, tools) for _ in range(12)]),
                asyncio.gather(*[_collect(anthropic, messages, tools) for _ in range(12)]),
            )
            return list(openai_runs), list(anthropic_runs)
        finally:
            await asyncio.gather(openai.aclose(), anthropic.aclose())

    try:
        openai_runs, anthropic_runs = asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    after = STATE.snapshot()
    assert after["requests"].get("/v1/responses", 0) - before["requests"].get(
        "/v1/responses", 0
    ) == 12
    assert after["requests"].get("/v1/messages", 0) - before["requests"].get(
        "/v1/messages", 0
    ) == 12
    assert after["active"] == 0
    assert after["peak_active"] >= 8

    sentinels = (
        OPENAI_ARGUMENT_SENTINEL,
        OPENAI_CODE_SENTINEL,
        ANTHROPIC_INPUT_SENTINEL,
        ANTHROPIC_MCP_INPUT_SENTINEL,
        ANTHROPIC_FILE_ID_SENTINEL,
        ANTHROPIC_CITED_TEXT_SENTINEL,
    )
    # MiniCode-owned fixture URLs; the old ``codex``/``claude`` spellings were
    # foreign-harness names. They stay distinct per provider so each adapter is
    # proven to project its own citation.
    for runs, expected_source in (
        (openai_runs, OPENAI_CITATION_URL),
        (anthropic_runs, ANTHROPIC_CITATION_URL),
    ):
        assert len(runs) == 12
        for events in runs:
            assert not [event for event in events if event.type == StreamEventType.ERROR]
            done = [event for event in events if event.type == StreamEventType.DONE]
            assert len(done) == 1
            public_trace = provider_raw_for_projection(done[0].raw)
            serialized = json.dumps(public_trace, ensure_ascii=False, sort_keys=True)
            assert expected_source in serialized
            assert all(sentinel not in serialized for sentinel in sentinels)
