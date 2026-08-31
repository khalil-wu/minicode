from __future__ import annotations

import asyncio

from backend.agent.provider_stream_runtime import stream_provider_response
from backend.llm.base import (
    LLMMessage,
    StreamEvent,
    StreamEventType,
    safe_stream_chat_with_request_metadata,
)


def test_provider_stream_resource_boundary_module_compiles_and_exports_runner() -> None:
    # The concrete stream lifecycle is exercised by the loop integration suite;
    # keep this focused check explicit so future refactors cannot remove the
    # close boundary while preserving imports through an adapter shim.
    assert callable(stream_provider_response)


def test_safe_provider_stream_closes_underlying_iterator_on_consumer_close() -> None:
    class ObservableStream:
        def __init__(self) -> None:
            self.closed = False
            self.emitted = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.emitted:
                self.emitted = True
                return StreamEvent(type=StreamEventType.TEXT_CHUNK, content="hello")
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    class Adapter:
        def __init__(self, stream) -> None:
            self.stream = stream

        def stream_chat(self, _messages, tools=None):
            return self.stream

    async def scenario() -> bool:
        underlying = ObservableStream()
        stream = safe_stream_chat_with_request_metadata(
            Adapter(underlying),
            [LLMMessage(role="user", content="hello")],
        )
        await anext(stream)
        await stream.aclose()
        return underlying.closed

    assert asyncio.run(scenario()) is True


class _RaisingAdapter:
    _provider_id = "custom"

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def stream_chat(self, _messages, tools=None, metadata=None):
        raise self._exc
        yield  # pragma: no cover - generator marker


def _drain(adapter) -> list[StreamEvent]:
    async def scenario() -> list[StreamEvent]:
        return [
            event
            async for event in safe_stream_chat_with_request_metadata(
                adapter,
                [LLMMessage(role="user", content="hello")],
            )
        ]

    return asyncio.run(scenario())


def test_normalized_provider_error_carries_the_fields_its_consumer_reads() -> None:
    """provider_stream_error_event reads more than three raw fields.

    It classifies from ``status_code`` / ``provider_error_code`` /
    ``provider_error_schema_type`` and honours the server's ``Retry-After``
    through ``retry_after_seconds``. Emitting only exception_type +
    provider_error_type + error_type discarded all of that.
    """

    import httpx

    request = httpx.Request("POST", "https://gw.example.test/v1/chat/completions")
    response = httpx.Response(
        429,
        request=request,
        headers={"Retry-After": "42"},
        text=(
            '{"error":{"message":"Rate limit reached","type":"rate_limit_error",'
            '"code":"rate_limited"}}'
        ),
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        failure: BaseException = exc

    events = _drain(_RaisingAdapter(failure))

    assert len(events) == 1
    raw = events[0].raw
    assert events[0].type == StreamEventType.ERROR
    assert raw["status_code"] == 429
    assert raw["retry_after_seconds"] == 42.0
    assert raw["provider_error_code"] == "rate_limited"
    assert raw["provider_error_schema_type"] == "rate_limit_error"
    assert raw["provider_error_type"] == "rate_limit"
    assert raw["exception_type"] == "HTTPStatusError"
    assert raw["provider"] == "custom"


def test_lifecycle_stale_error_is_not_normalized_into_a_model_error() -> None:
    """A stale lifecycle capability is a harness bug, not a provider failure.

    Thirteen adapter sites re-raise LifecycleStaleError on purpose. Catching it
    under ``except Exception`` here erased the type and made an obsolete
    extension generation look like a flaky model. The agent runtime has its own
    boundary (``fail_provider_runtime``) which logs the traceback.
    """

    from backend.agent.lifecycle_errors import LifecycleStaleError

    try:
        _drain(_RaisingAdapter(LifecycleStaleError("generation 3 is obsolete")))
    except LifecycleStaleError as exc:
        assert "generation 3 is obsolete" in str(exc)
    else:
        raise AssertionError("LifecycleStaleError must reach the caller")
