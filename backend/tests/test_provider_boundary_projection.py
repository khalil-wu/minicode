from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.artifact.store import ArtifactStore
from backend.agent.error_withholding import ErrorWithholdingController
from backend.agent.policies.stream_retry import DefaultStreamRetryPolicy
from backend.agent.provider_stream_error_event import handle_provider_error_event, provider_error_details
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.turn_recovery_runtime import degrade_and_finish, recover_withheld_error
from backend.config import AgentSettings, LLMSettings
from backend.llm.anthropic_adapter import (
    _anthropic_declared_error_event,
    _anthropic_exception_error_event,
)
from backend.llm.base import LLMMessage, StreamEvent, StreamEventType, UsageInfo
from backend.llm.errors import classify_llm_error, llm_error_raw, sanitize_llm_error_message
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.web_tools import WebFetchTool


def test_structured_provider_kind_survives_event_reclassification() -> None:
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    response = httpx.Response(
        503,
        request=request,
        json={
            "error": {
                "code": "model_not_found",
                "message": "model is unavailable",
            }
        },
    )
    error = httpx.HTTPStatusError("service unavailable", request=request, response=response)
    initial = classify_llm_error(error)
    event = StreamEvent(
        type=StreamEventType.ERROR,
        content=sanitize_llm_error_message(error, initial),
        raw=llm_error_raw(error, "openai_responses"),
    )

    _, projected, _ = provider_error_details(event)

    assert initial.error_type == "model"
    assert initial.provider_error_type == "model"
    assert projected.error_type == "model"
    assert projected.provider_error_type == "model"
    assert projected.fatal is True
    assert projected.retryable is False


def test_provider_request_diagnostic_survives_into_runtime_evidence() -> None:
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    response = httpx.Response(400, request=request, json={"error": {
        "type": "invalid_request_error",
        "message": "No tool output found for tool search call search-owned.",
    }})
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    event = StreamEvent(type=StreamEventType.ERROR,
        content=sanitize_llm_error_message(error, classify_llm_error(error)),
        raw=llm_error_raw(error, "openai_responses"))
    _, _, evidence = provider_error_details(event)
    assert evidence["status_code"] == 400
    assert evidence["provider_error_schema_type"] == "invalid_request_error"
    assert evidence["provider_error_message"] == "No tool output found for tool search call search-owned."


@pytest.mark.asyncio
async def test_responses_http_400_keeps_safe_reason_through_adapter_trace_and_terminal_error(caplog):
    reason = ("Invalid Value: 'tools'. Function 'collaboration.followup_task' is reserved for use by this model "
        "and must match the configured schema.")
    secret = "sk-provider-projection-secret"
    body_only = "DO_NOT_LOG_THE_FULL_ERROR_BODY"
    body = json.dumps({"error": {"type": "invalid_request_error",
        "message": f"{reason} API key: {secret} " + "diagnostic " * 100}, "request_input": body_only}).encode()
    requests = []

    async def reject(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = next(int(line.split(b":", 1)[1]) for line in headers.splitlines()
            if line.lower().startswith(b"content-length:"))
        await reader.readexactly(length)
        requests.append(headers.splitlines()[0])
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    async with server:
        adapter = OpenAIAdapter(LLMSettings(api_key=secret, model="projection-fixture", wire_api="responses",
            base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", proxy_mode="direct"))
        try:
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="hi")])]
            errors = [event for event in events if event.type is StreamEventType.ERROR]
            assert len(errors) == 1 and not any(event.type is StreamEventType.DONE for event in events)
            error = errors[0]
            assert error.raw["status_code"] == 400 and error.raw["provider_error_type"] == "unknown"
            assert reason in error.content
            assert len(error.raw["provider_error_message"]) <= 400
            _, classification, evidence = provider_error_details(error)
            assert classification.retryable is False
            assert evidence["provider_error_message"] == error.raw["provider_error_message"]

            closed = AsyncMock()
            state = AgentState(user_message="hi")
            updates = [item async for item in handle_provider_error_event(error,
                state=state, context_builder=SimpleNamespace(hook_manager=None, _llm=adapter),
                turn_kernel=SimpleNamespace(close_provider_attempt=closed, run_context=RunContext()),
                provider_attempt=SimpleNamespace(span_id="actual-http-400"),
                stream_state=StreamAttemptState(), stream_text=StreamTextState(iteration_id="iter:1"),
                pending_tool_calls=[], usage=UsageInfo(), turn_usage=UsageInfo(),
                stream_retry_policy=DefaultStreamRetryPolicy(AgentSettings()), stream_attempt=0,
                stream_recovery_attempted=False, budget_runtime=SimpleNamespace(active_phase_deadline=lambda: None),
                error_controller=ErrorWithholdingController(), iteration_id_value="iter:1", cancel_event=None,
                degrade_and_finish=degrade_and_finish, recover_withheld_error=recover_withheld_error)]
        finally:
            await adapter.aclose()

    terminal = next(event for event in updates if getattr(event, "type", "") == "error")
    assert reason in terminal.data["message"] and "HTTP 400" in terminal.data["message"]
    assert terminal.data["provider_error_type"] == "unknown" and terminal.data["error_type"] == "api"
    assert state.terminal_status == "failed" and state.stopped_reason == "api_error"
    assert updates[-1].action == "finish" and updates[-1].stream_attempt == 0
    assert requests == [b"POST /v1/responses HTTP/1.1"]
    assert closed.await_count == 1
    assert closed.await_args.kwargs["status"] == "failed"
    assert closed.await_args.kwargs["data"]["provider_error_message"] == evidence["provider_error_message"]
    adapter_logs = "\n".join(record.getMessage() for record in caplog.records
        if record.name == "backend.llm.openai_adapter")
    assert reason in adapter_logs
    projected = adapter_logs + error.content + json.dumps(error.raw) + json.dumps(terminal.data)
    assert secret not in projected and body_only not in projected
    assert "[redacted]" in terminal.data["message"]


def test_anthropic_messages_exception_preserves_safe_provider_diagnostics() -> None:
    request = httpx.Request("POST", "https://gateway.example/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "type": "new_api_error",
                "code": "convert_request_failed",
                "message": "messages payload cannot be converted",
            }
        },
    )
    event = _anthropic_exception_error_event(
        httpx.HTTPStatusError("bad request", request=request, response=response),
        provider="custom_anthropic",
    )

    assert event.type.value == "error"
    assert "MiniCode Anthropic Messages 请求失败" in event.content
    assert "messages payload cannot be converted" in event.content
    assert "Claude API 调用失败" not in event.content
    assert event.raw["provider"] == "custom_anthropic"
    assert event.raw["provider_error_code"] == "convert_request_failed"
    assert event.raw["provider_error_schema_type"] == "new_api_error"
    assert event.raw["provider_error_message"] == "messages payload cannot be converted"
    classification = classify_llm_error(
        f"{event.content} provider_error_code={event.raw['provider_error_code']} status={event.raw['status_code']}"
    )
    assert classification.fatal is True
    assert classification.retryable is False
    assert classification.error_type == "provider_protocol"
    assert classification.provider_error_type == "protocol"


def test_anthropic_messages_declared_error_has_the_same_projection_contract() -> None:
    event = _anthropic_declared_error_event(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "bad_messages",
                "message": "invalid message block",
            },
        },
        provider="custom_anthropic",
    )

    assert "MiniCode Anthropic Messages 请求失败" in event.content
    assert "invalid message block" in event.content
    assert event.raw["provider_error_code"] == "bad_messages"
    assert event.raw["provider_error_message"] == "invalid message block"


@pytest.mark.parametrize("http_error", [False, True])
def test_anthropic_error_code_and_schema_type_do_not_reintroduce_secrets(http_error):
    import json
    secret = "sk-" + "AuditSyntheticSecretValue01234567890"
    body = {"error": {"code": secret, "type": "Bearer " + secret, "message": "failed"}}
    if http_error:
        request = httpx.Request("POST", "https://fixture.invalid/v1/messages")
        response = httpx.Response(400, request=request, json=body)
        event = _anthropic_exception_error_event(
            httpx.HTTPStatusError("failure", request=request, response=response), provider="fixture",
        )
    else:
        event = _anthropic_declared_error_event(body, provider="fixture")
    assert secret not in event.content
    assert secret not in json.dumps(event.raw)
    _, _, projected = provider_error_details(event)
    assert secret not in json.dumps(projected)


@pytest.mark.asyncio
async def test_web_fetch_keeps_fetched_artifact_when_model_extraction_fails(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)
    request = httpx.Request("POST", "https://gateway.example/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "type": "new_api_error",
                "code": "convert_request_failed",
                "message": "gateway rejected extraction request",
            }
        },
    )

    async def fake_fetch(_url: str, *, enforce_network: bool = True):
        return (
            httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"Fetched source text that must remain available.",
                request=httpx.Request("GET", "https://example.test/page"),
            ),
            None,
        )

    async def failed_extract(_text: str, _prompt: str, _context) -> str:
        raise httpx.HTTPStatusError(
            "bad request",
            request=request,
            response=response,
        )

    tool._get_with_permitted_redirects = fake_fetch  # type: ignore[method-assign]
    tool._extract_with_prompt = failed_extract  # type: ignore[method-assign]
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        llm=SimpleNamespace(_provider_id="custom_anthropic"),
    )

    async with aclosing(store):
        result = await tool.execute(
            {"url": "https://example.test/page", "prompt": "summarize it"},
            context,
        )

    assert result.is_error is True
    assert result.extraction_status == "failed"
    assert result.evidence_type == "fetched"
    assert result.artifact_id
    assert result.artifact_preview
    assert "模型二次提取失败" in result.content
    assert result.content == f"网页已抓取；模型二次提取失败。原始清洗内容保存在 artifact {result.artifact_id}。"
    assert result.provider == "custom_anthropic"
    assert result.provider_error_type == "protocol"
    assert result.error_kind == "provider_protocol"
    assert result.user_summary == "网页已抓取，但模型提取失败。"
    assert "HTTP 400" in (result.developer_detail or "")
    assert "gateway rejected extraction request" in (result.developer_detail or "")
