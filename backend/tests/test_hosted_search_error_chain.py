from __future__ import annotations

import json

import httpx
import pytest

from backend.config import LLMSettings
from backend.llm.base import LLMMessage, LLMTurnContext
from backend.llm.errors import classify_llm_error, llm_error_status_code
from backend.llm.openai_adapter import OpenAIAdapter, ResponsesStreamError
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.web_tools import WebSearchTool


def streamed_response(request, event):
    return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
        content=("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())


def settings():
    return LLMSettings(api_key="fixture", provider="custom", model="gpt-6.1-sol", wire_api="responses",
        base_url="https://search.invalid/v1", supports_hosted_web_search=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape,code,status,kind", [
    ("error", "server_error", 503, "network"),
    ("top-level", "invalid_api_key", 401, "auth"),
    ("response.error", "insufficient_quota", 429, "billing"),
    ("response.failed", "model_not_found", 503, "model"),
])
async def test_side_responses_keeps_structured_fields_and_full_redacted_message(shape, code, status, kind, caplog):
    secret = "sk-fixture-only-secret"
    message = "Diagnostic context " * 40 + f"Bearer {secret}; Request ID req-complete-tail"
    error = {"message": message, "code": code, "type": "provider_failure", "status_code": status,
        "request_id": "req-complete-tail", "param": "tools"}
    if shape == "response.failed":
        event = {"type": shape, "response": {"status": "failed", "error": error}}
    elif shape == "top-level":
        event = {**error, "type": "error"}
    else:
        event = {"type": shape, "error": error}

    async def respond(request):
        return streamed_response(request, event)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        try:
            with pytest.raises(ResponsesStreamError) as caught:
                await adapter.simple_chat([LLMMessage(role="user", content="Inspect the search error")])
        finally:
            await adapter.aclose()
    failure = caught.value
    assert failure.body["code"] == code
    assert failure.body["type"] == ("error" if shape == "top-level" else "provider_failure")
    assert failure.body["param"] == "tools"
    assert failure.request_id == "req-complete-tail"
    assert llm_error_status_code(failure) == status
    assert classify_llm_error(failure).provider_error_type == kind
    assert "Request ID req-complete-tail" in str(failure)
    assert len(str(failure)) > 700
    assert secret not in str(failure) + json.dumps(failure.body) + json.dumps(failure.raw) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,code,kind", [(200, "insufficient_quota", "billing"), (503, "model_not_found", "model")])
async def test_hosted_search_keeps_provider_failure_in_the_tool_result_without_an_extra_fallback(http_status, code, kind):
    requests = []
    provider_error = {"code": code, "type": "provider_failure", "status_code": 503 if code == "model_not_found" else 429,
        "message": "Controlled rejection; full diagnosis remains available.", "request_id": "req-search-visible"}

    async def respond(request):
        requests.append(json.loads(request.content))
        if http_status != 200:
            return httpx.Response(http_status, request=request, json={"error": provider_error})
        return streamed_response(request, {"type": "error", "error": provider_error})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        try:
            result = await WebSearchTool(adapter).execute({"query": "Current news"},
                ToolExecutionContext(PermissionContext(mode="bypass"), llm=adapter))
        finally:
            await adapter.aclose()
    assert len(requests) == 1
    assert result.is_error and result.status == "failed"
    assert result.provider == "custom"
    assert result.provider_error_type == kind
    assert result.error_kind == kind
    assert result.recoverable is False
    assert result.result_kind == "search"
    assert "req-search-visible" in result.developer_detail
    assert code in result.content
    assert result.runtime_metadata["provider_error"]["provider_error"]["request_id"] == "req-search-visible"


@pytest.mark.asyncio
async def test_hosted_stream_server_error_uses_the_existing_auxiliary_retry_owner(monkeypatch):
    monkeypatch.setattr("backend.llm.base._SIDE_QUERY_BASE_DELAY_SECONDS", 0)
    requests = []
    turn = LLMTurnContext()

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return streamed_response(request, {"type": "error", "error": {
                "code": "server_error", "type": "server_error", "message": "The provider could not complete this request.",
                "request_id": "req-first-attempt",
            }})
        return streamed_response(request, {"type": "response.completed", "response": {"id": "search-completed", "status": "completed", "output": [
            {"type": "web_search_call", "id": "search-1", "status": "completed"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Verified search result", "annotations": []}]},
        ]}})

    from backend.agent.run_context import RunContext
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        try:
            result = await WebSearchTool(adapter).execute({"query": "Current news"}, ToolExecutionContext(
                PermissionContext(mode="bypass"), llm=adapter, run_context=RunContext(llm_turn_context=turn)))
        finally:
            await adapter.aclose()
    assert not result.is_error
    assert "Verified search result" in result.content
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert turn.side_call_records[0]["retry_count"] == 1
    assert turn.side_call_records[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_hosted_error_reaches_query_state_public_event_and_cold_journal(tmp_path, monkeypatch):
    from backend.agent.execution_journal import ExecutionJournal
    from backend.api.models import ToolCallRecord as ApiToolCallRecord
    from backend.tests.test_code_execution import ScriptModel, run_model

    monkeypatch.setattr("backend.llm.base._SIDE_QUERY_BASE_DELAY_SECONDS", 0)
    requests = []
    secret = "sk-query-error-fixture-secret"

    async def respond(request):
        requests.append(json.loads(request.content))
        return streamed_response(request, {"type": "error", "error": {
            "code": "server_error", "type": "server_error", "status_code": 503,
            "message": "Full provider diagnostic " * 25 + f"Bearer {secret}; final evidence tail",
            "request_id": f"req-query-attempt-{len(requests)}",
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)

        class SearchModel(ScriptModel):
            def supports_hosted_web_search(self):
                return True

            def hosted_web_search_supports_blocked_domains(self):
                return False

            async def side_query(self, messages, *, options, turn_context=None):
                return await adapter.side_query(messages, options=options, turn_context=turn_context)

        try:
            state, _builder, _journal, events, _runtime = await run_model(tmp_path,
                SearchModel('text(await tools.web_search({query:"current news"}));'), [WebSearchTool()])
        finally:
            await adapter.aclose()

    assert len(requests) == 4  # Existing auxiliary policy: one attempt plus three retries.
    record = next(item for item in state.tool_calls if item.tool_name == "web_search")
    assert record.status == "failed"
    assert record.user_summary == "网页搜索失败。"
    assert record.tool_output.startswith("网页搜索失败。\n")
    assert "Hosted web search failed" not in record.tool_output
    detail = record.developer_detail
    assert "final evidence tail" in detail and "req-query-attempt-4" in detail
    assert '"code": "server_error"' in detail and '"type": "server_error"' in detail
    assert '"status_code": 503' in detail

    public = ApiToolCallRecord.from_internal(record).model_dump()
    event = next(item for item in events if item.type == "tool_result" and item.data.get("id") == record.tool_call_id)
    cold = ExecutionJournal("code", base_dir=tmp_path / "journals")
    persisted = next(item.payload for item in cold.read_events()
        if item.event_type == "tool_result" and item.payload.get("tool_call_id") == record.tool_call_id)
    assert public["developer_detail"] == detail
    assert event.data["error_info"]["developer_detail"] == detail
    assert persisted["error_info"]["developer_detail"] == detail
    assert persisted["developer_detail"] == detail
    assert event.data["provider_error_type"] == persisted["provider_error_type"] == "network"
    serialized = json.dumps([public, event.data, persisted], ensure_ascii=False)
    assert secret not in serialized
    assert "runtime_metadata" not in serialized
