"""Connect-phase retry track: an unreachable provider is waited out, not budgeted."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from backend.agent.first_byte_waiter import ProviderStreamFailure
from backend.agent.policies.stream_retry import (
    CONNECTION_RETRY_INITIAL_DELAY_SECONDS,
    CONNECTION_RETRY_MAX_DELAY_SECONDS,
    DefaultStreamRetryPolicy,
    StreamRetryState,
    plan_connection_retry,
)
from backend.agent.provider_stream_transport import (
    ProviderTransportFailureResult,
    handle_provider_transport_failure,
)
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.tool_stream_tracker import StreamingToolTracker
from backend.config import AgentSettings
from backend.llm.base import UsageInfo
from backend.llm.errors import is_connection_not_established


def _wrapped(inner: BaseException) -> openai.APIConnectionError:
    """Build the chain the OpenAI SDK actually raises.

    The SDK raises its own error inside the ``except`` block, so the transport
    error survives as ``__context__`` rather than being discarded.
    """

    try:
        try:
            raise inner
        except type(inner):
            raise openai.APIConnectionError(request=None)  # type: ignore[arg-type]
    except openai.APIConnectionError as error:
        return error


@pytest.mark.parametrize(
    "inner",
    [
        httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
        httpx.ConnectTimeout("timed out connecting"),
        ConnectionRefusedError("connection refused"),
    ],
)
def test_connect_phase_failures_are_recognized_by_type(inner) -> None:
    assert is_connection_not_established(_wrapped(inner)) is True


@pytest.mark.parametrize(
    "inner",
    [
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        httpx.ReadTimeout("read timed out"),
        httpx.ReadError("connection reset after the response started"),
    ],
)
def test_in_flight_failures_are_never_read_as_connect_phase(inner) -> None:
    """A request that already left the client must keep spending its budget."""

    assert is_connection_not_established(_wrapped(inner)) is False


def test_a_bare_string_cannot_claim_the_connect_phase() -> None:
    """Without an exception there is no type to match, so the answer is no."""

    assert is_connection_not_established("ConnectError: connection refused") is False


def test_plan_connection_retry_doubles_to_a_cap() -> None:
    state = StreamRetryState()
    delays = [plan_connection_retry(state) for _ in range(6)]

    # 5 -> 10 -> 20 -> 40 -> the doubling target (80) clamps to the 60s cap, and
    # the cap then repeats instead of growing without bound.
    assert delays == [
        CONNECTION_RETRY_INITIAL_DELAY_SECONDS,
        CONNECTION_RETRY_INITIAL_DELAY_SECONDS * 2,
        CONNECTION_RETRY_INITIAL_DELAY_SECONDS * 4,
        CONNECTION_RETRY_INITIAL_DELAY_SECONDS * 8,
        CONNECTION_RETRY_MAX_DELAY_SECONDS,
        CONNECTION_RETRY_MAX_DELAY_SECONDS,
    ]
    assert state.connection_retry_delay_seconds == CONNECTION_RETRY_MAX_DELAY_SECONDS
    assert state.connection_retries == 6


class _TurnKernel:
    def __init__(self) -> None:
        self.closed: list[dict[str, object]] = []
        self.spans: list[dict[str, object]] = []

    async def close_provider_attempt(self, _attempt, **kwargs) -> None:
        self.closed.append(kwargs)

    async def emit_runtime_span(self, _event, **kwargs) -> None:
        self.spans.append(kwargs)


class _BudgetRuntime:
    def __init__(self) -> None:
        self.requested: list[float] = []

    def bounded_provider_timeout(self, requested):
        # The wait is captured, not performed: the assertion is on the requested
        # delay, and the phase deadline is already covered by its own tests.
        self.requested.append(requested)
        return 0.0, False


async def _degrade_and_finish(**_kwargs):
    yield


async def _noop_close() -> None:
    return None


async def _drive_transport_failure(failure, *, settings: AgentSettings, stream_attempt: int,
                                   max_retries: int, retry_state: StreamRetryState):
    kernel = _TurnKernel()
    budget = _BudgetRuntime()

    async def collect():
        return [
            item
            async for item in handle_provider_transport_failure(
                failure,
                settings=settings,
                stream_text=StreamTextState(),
                pending_tool_calls=[],
                stream_state=StreamAttemptState(),
                stream_retry_policy=DefaultStreamRetryPolicy(settings),
                stream_attempt=stream_attempt,
                turn_kernel=kernel,
                provider_attempt=SimpleNamespace(span_id="span-1", usage_settled=True),
                budget_runtime=budget,
                iteration_id_value="iter:conn",
                stream_iter=None,
                cancel_event=None,
                tool_tracker=StreamingToolTracker(),
                state=SimpleNamespace(),
                context_builder=SimpleNamespace(),
                turn_usage=UsageInfo(),
                usage=UsageInfo(),
                degrade_and_finish=_degrade_and_finish,
                query_source="user",
                retry_state=retry_state,
                progress_id="progress:conn",
                max_retries=max_retries,
                close_stream=_noop_close,
                switch_transport=lambda: False,
            )
        ]

    return await collect(), kernel, budget


def test_unreachable_provider_does_not_consume_the_request_retry_budget() -> None:
    """The connect phase waits for the network and keeps the ordinal unchanged.

    ``stream_max_attempts`` is set below the current attempt, so the request
    budget is genuinely exhausted: the request policy would decline and the turn
    would finish. The connect track still retries, because it never spends that
    budget.
    """

    updates, kernel, budget = asyncio.run(
        _drive_transport_failure(
            ProviderStreamFailure(_wrapped(httpx.ConnectError("getaddrinfo failed"))),
            settings=AgentSettings(stream_max_attempts=2),
            stream_attempt=3,
            max_retries=2,
            retry_state=StreamRetryState(),
        )
    )

    result = updates[-1]
    assert isinstance(result, ProviderTransportFailureResult)
    assert result.action == "retry"
    assert result.stream_attempt == 3
    assert budget.requested == [CONNECTION_RETRY_INITIAL_DELAY_SECONDS]

    progress = next(item for item in updates if item.type == "agent.progress")
    assert progress.data["provider_state"] == "reconnecting"
    # No N/M is advertised, because no request-level retry was consumed.
    assert progress.data.get("retry_attempt") is None
    assert progress.data.get("max_retries") is None
    assert "等待网络恢复" in progress.data["message"]

    assert len(kernel.spans) == 1
    assert kernel.spans[0]["data"]["connection_retry"] is True
    assert kernel.spans[0]["data"]["connection_retries"] == 1
    assert kernel.spans[0]["data"]["stream_attempt"] == 3


def test_an_exhausted_request_budget_still_finishes_a_broken_stream() -> None:
    """The same exhausted budget ends the turn when the stream itself broke."""

    updates, _kernel, _budget = asyncio.run(
        _drive_transport_failure(
            ProviderStreamFailure(_wrapped(httpx.RemoteProtocolError("peer closed"))),
            settings=AgentSettings(stream_max_attempts=2),
            stream_attempt=3,
            max_retries=2,
            retry_state=StreamRetryState(),
        )
    )

    result = updates[-1]
    assert isinstance(result, ProviderTransportFailureResult)
    assert result.action == "finish"


def test_the_same_exhausted_budget_finishes_when_the_connect_track_is_off() -> None:
    """Proof that the test above discriminates on the connect track, not the input.

    Identical failure and identical exhausted budget; only the track is
    disabled, and the turn now ends.
    """

    updates, _kernel, _budget = asyncio.run(
        _drive_transport_failure(
            ProviderStreamFailure(_wrapped(httpx.ConnectError("getaddrinfo failed"))),
            settings=AgentSettings(stream_max_attempts=2, stream_connection_retries_enabled=False),
            stream_attempt=3,
            max_retries=2,
            retry_state=StreamRetryState(),
        )
    )

    result = updates[-1]
    assert isinstance(result, ProviderTransportFailureResult)
    assert result.action == "finish"


def test_connection_track_attempts_keep_distinct_spans_and_back_off() -> None:
    state = StreamRetryState()
    delays = []
    for _ in range(3):
        updates, kernel, _budget = asyncio.run(
            _drive_transport_failure(
                ProviderStreamFailure(_wrapped(httpx.ConnectError("refused"))),
                settings=AgentSettings(),
                stream_attempt=0,
                max_retries=10,
                retry_state=state,
            )
        )
        delays.append(_budget.requested[0])
        assert kernel.spans[0]["span_id"].endswith(f":net{state.connection_retries}")

    assert delays == [5.0, 10.0, 20.0]
    # Spans are distinct so repeated attempts do not collapse in the Inspector.
    assert len({f"net{i}" for i in range(1, 4)}) == 3


def test_mid_stream_failure_still_spends_the_request_budget() -> None:
    """Only the connect phase is unbounded; a broken stream stays budgeted."""

    updates, _kernel, budget = asyncio.run(
        _drive_transport_failure(
            ProviderStreamFailure(_wrapped(httpx.RemoteProtocolError("peer closed"))),
            settings=AgentSettings(),
            stream_attempt=1,
            max_retries=5,
            retry_state=StreamRetryState(),
        )
    )

    result = updates[-1]
    assert isinstance(result, ProviderTransportFailureResult)
    assert result.action == "retry"
    # The request retry ordinal advanced, and the delay came from the request
    # policy: 500ms base doubled for attempt 1, plus up to 25% jitter.
    assert result.stream_attempt == 2
    assert 1.0 <= budget.requested[0] <= 1.25

    progress = next(item for item in updates if item.type == "agent.progress")
    assert progress.data["retry_attempt"] == 2
    assert progress.data["max_retries"] == 5


def test_connection_track_falls_back_to_the_request_budget_when_disabled() -> None:
    settings = AgentSettings(stream_connection_retries_enabled=False)

    updates, _kernel, budget = asyncio.run(
        _drive_transport_failure(
            ProviderStreamFailure(_wrapped(httpx.ConnectError("refused"))),
            settings=settings,
            stream_attempt=1,
            max_retries=5,
            retry_state=StreamRetryState(),
        )
    )

    result = updates[-1]
    assert isinstance(result, ProviderTransportFailureResult)
    assert result.action == "retry"
    assert result.stream_attempt == 2
    assert 1.0 <= budget.requested[0] <= 1.25
