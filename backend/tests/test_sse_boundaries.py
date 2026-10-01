import asyncio

import pytest

from backend.llm.sse import (
    ProviderStreamLimitError,
    SSEMalformedBudget,
    iter_sse_data,
)


class _ByteResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _LineResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def _collect(response) -> list[str]:
    return [item async for item in iter_sse_data(response)]


def test_sse_parser_reassembles_fragmented_multiline_events() -> None:
    response = _ByteResponse(
        [
            b": keepalive\n",
            b"event: message\ndata: {\"type\":\"response.",
            b"output_text.delta\",\ndata: \"delta\":\"ok\"}\n\n",
        ]
    )

    assert asyncio.run(_collect(response)) == [
        '{"type":"response.output_text.delta",\n"delta":"ok"}'
    ]


def test_sse_parser_limits_one_unterminated_line(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_PROVIDER_SSE_MAX_LINE_BYTES", "1024")
    response = _ByteResponse([b"data: " + (b"x" * 2048)])

    with pytest.raises(ProviderStreamLimitError, match="line exceeded"):
        asyncio.run(_collect(response))


def test_sse_parser_limits_legacy_line_clients(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_PROVIDER_SSE_MAX_EVENT_BYTES", "1024")
    response = _LineResponse(["data: " + ("x" * 2048)])

    with pytest.raises(ProviderStreamLimitError, match="event exceeded"):
        asyncio.run(_collect(response))


def test_malformed_sse_budget_tolerates_one_bad_event_then_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_PROVIDER_SSE_MAX_CONSECUTIVE_MALFORMED", "1")
    budget = SSEMalformedBudget()
    budget.reject("not json")
    budget.accept()
    budget.reject("still not json")

    with pytest.raises(ProviderStreamLimitError, match="malformed event budget"):
        budget.reject("again not json")


@pytest.mark.parametrize("separator", [b"\n", b"\r", b"\r\n"])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1000])
def test_sse_all_line_endings_and_fragmented_utf8(separator, chunk_size):
    wire = separator.join([
        b": ping", b"event: message", b"data: {", 'data: "text":"中文"}'.encode(),
        b"", b"data: {\"second\":2}", b"", b"",
    ])
    response = _ByteResponse([wire[i:i + chunk_size] for i in range(0, len(wire), chunk_size)])
    assert asyncio.run(_collect(response)) == ['{\n"text":"中文"}', '{"second":2}']


def test_sse_trailing_cr_is_not_part_of_line_byte_budget(monkeypatch):
    monkeypatch.setenv("MINICODE_PROVIDER_SSE_MAX_LINE_BYTES", "1024")
    payload = b"data: " + b"x" * 1018
    assert asyncio.run(_collect(_ByteResponse([payload + b"\r", b"\r"]))) == ["x" * 1018]
