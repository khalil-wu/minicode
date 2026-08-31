from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args

from backend.agent.compaction import format_compaction_history
from backend.config import AgentSettings, PermissionSettings
from backend.agent.policies.stream_retry import DefaultStreamRetryPolicy, StreamRetryState
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.errors import classify_llm_error, retry_after_seconds
from backend.llm.openai_errors import (
    _is_blocked_gateway_error,
    _is_stream_options_unsupported_error,
    _is_transient_gateway_error,
)
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.health_service import build_health_payload
from backend.tools.base import PermissionLevel
from backend.tools.schedule_cron_tool import ScheduleCronTool
from backend.tools.write_file import WriteFileTool
from backend.ws.events import ServerEventType


def test_stream_idle_timeout_keeps_pi_codex_default_and_retry_matches_cc() -> None:
    settings = AgentSettings()
    # Idle timeout stays at the pi/codex 5-minute default; retry shape now
    # matches Claude Code (withRetry.ts: DEFAULT_MAX_RETRIES=10, BASE_DELAY_MS=500).
    assert settings.stream_timeout_seconds == 300.0
    assert settings.stream_max_attempts == 10
    assert settings.stream_retry_delay_seconds == 0.5
    assert AgentSettings().max_turn_seconds == 0.0


def test_stream_retry_uses_cc_jittered_backoff() -> None:
    policy = DefaultStreamRetryPolicy(AgentSettings())

    # cc getRetryDelay: base = min(500ms * 2**(attempt-1), 32s) + up to 25% jitter.
    assert 0.5 <= policy.decide_retry("503 service unavailable", 0).delay_seconds <= 0.625
    assert 1.0 <= policy.decide_retry("503 service unavailable", 1).delay_seconds <= 1.25
    assert 2.0 <= policy.decide_retry("503 service unavailable", 2).delay_seconds <= 2.5
    assert policy.decide_retry("503 service unavailable", 9).should_retry is True
    assert policy.decide_retry("503 service unavailable", 10).should_retry is False


def test_stream_retry_classifies_pi_and_claude_transient_provider_shapes() -> None:
    for message, provider_error_type in (
        ("overloaded_error", "busy"),
        ("HTTP 529", "busy"),
        ("HTTP 524", "network"),
        ("ResourceExhausted", "busy"),
    ):
        classification = classify_llm_error(message)
        assert classification.retryable is True
        assert classification.provider_error_type == provider_error_type


def test_cloudflare_525_is_transient_even_when_body_looks_blocked() -> None:
    error = RuntimeError(
        "Cloudflare SSL handshake failed; your request was blocked (cf-ray=abc)"
    )
    error.status_code = 525  # type: ignore[attr-defined]

    classification = classify_llm_error(error)

    assert classification.fatal is False
    assert classification.retryable is True
    assert classification.error_type == "api"
    assert classification.provider_error_type == "network"
    assert _is_transient_gateway_error(error) is True
    assert _is_blocked_gateway_error(error) is False


def test_stream_retry_retries_525_but_not_real_policy_block() -> None:
    policy = DefaultStreamRetryPolicy(AgentSettings())

    retry = policy.decide_retry(
        "provider_error_type=blocked status=525 Cloudflare SSL handshake failed",
        0,
    )
    blocked = policy.decide_retry(
        "provider_error_type=blocked status=403 request was blocked by policy",
        0,
    )

    assert retry.should_retry is True
    assert 0.5 <= retry.delay_seconds <= 0.625
    assert blocked.should_retry is False


def test_stream_retry_529_is_source_aware_and_consecutive_bounded() -> None:
    policy = DefaultStreamRetryPolicy(AgentSettings())
    state = StreamRetryState()

    assert policy.decide_retry(
        "HTTP 529 overloaded", 0, query_source="background", retry_state=state
    ).should_retry is False

    state = StreamRetryState()
    assert policy.decide_retry(
        "HTTP 529 overloaded", 0, query_source="user", retry_state=state
    ).should_retry is True
    assert policy.decide_retry(
        "HTTP 529 overloaded", 1, query_source="user", retry_state=state
    ).should_retry is True
    assert policy.decide_retry(
        "HTTP 529 overloaded", 2, query_source="user", retry_state=state
    ).should_retry is False

    # A non-529 failure breaks the consecutive sequence; the next busy response
    # gets the first 529 retry slot again.
    assert policy.decide_retry(
        "HTTP 503 unavailable", 3, query_source="user", retry_state=state
    ).should_retry is True
    assert policy.decide_retry(
        "HTTP 529 overloaded", 4, query_source="user", retry_state=state
    ).should_retry is True


def test_stream_retry_keeps_legacy_two_argument_policy_compatible() -> None:
    class LegacyPolicy:
        def decide_retry(self, error_message: str, attempt_index: int):
            assert error_message == "503 service unavailable"
            assert attempt_index == 0
            return SimpleNamespace(
                should_retry=True,
                delay_seconds=0.0,
                max_attempts=1,
            )

    from backend.agent.loop_runtime_helpers import plan_stream_retry

    assert plan_stream_retry(
        LegacyPolicy(),
        "503 service unavailable",
        0,
        query_source="background",
        retry_state=StreamRetryState(),
    ) == (1, 0.0)


def test_retry_after_parser_accepts_response_header_and_projected_error() -> None:
    response_error = RuntimeError("HTTP 429")
    response_error.response = SimpleNamespace(  # type: ignore[attr-defined]
        headers={"Retry-After": "11"}
    )
    assert retry_after_seconds(response_error) == 11.0

    projected_error = RuntimeError("rate limited")
    projected_error.retry_after_seconds = 7.5  # type: ignore[attr-defined]
    assert retry_after_seconds(projected_error) == 7.5


def test_auto_does_not_silently_accept_workspace_edits(tmp_path) -> None:
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    tool = WriteFileTool()
    args = {"file_path": "new.txt", "content": "hello"}

    assert checker.check(
        "write_file",
        args,
        context=PermissionContext(mode="auto"),
        tool=tool,
    ) == PermissionLevel.DIFF_REVIEW
    assert checker.check(
        "write_file",
        args,
        context=PermissionContext(mode="bypass"),
        tool=tool,
    ) == PermissionLevel.AUTO


def test_schedule_cron_does_not_expose_permission_bypass_to_the_model() -> None:
    schema = ScheduleCronTool().get_schema()

    assert set(schema.parameters["properties"]) == {"name", "prompt", "cron"}
    assert schema.parameters["required"] == ["name", "prompt", "cron"]


def test_compaction_serializes_assistant_tool_calls() -> None:
    transcript = format_compaction_history([
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCallEvent(
                id="call-1",
                name="read_file",
                arguments={"file_path": "README.md"},
            )],
        ),
    ])

    assert transcript == (
        '[Assistant tool calls]: read_file(file_path="README.md")'
    )


def test_health_reports_starting_until_bootstrap_is_ready() -> None:
    starting = build_health_payload(bootstrap=None, active_sessions=0)
    assert starting["status"] == "starting"
    assert starting["ready"] is False

    bootstrap = SimpleNamespace(
        config=object(),
        file_memory=object(),
        skill_manager=SimpleNamespace(list_all=lambda: []),
        mcp_manager=None,
    )
    ready = build_health_payload(bootstrap=bootstrap, active_sessions=1)
    assert ready["ready"] is True
    assert ready["status"] in {"ok", "degraded"}


def test_openai_compatibility_detects_unsupported_stream_usage_option() -> None:
    error = RuntimeError("400 unknown parameter: stream_options.include_usage")
    error.status_code = 400  # type: ignore[attr-defined]

    assert _is_stream_options_unsupported_error(error) is True


def test_mailbox_event_is_part_of_the_server_protocol() -> None:
    assert "subagent.mailbox" in get_args(ServerEventType)
