from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from backend.artifact.store import ArtifactStore
from backend.llm.anthropic_adapter import (
    _anthropic_declared_error_event,
    _anthropic_exception_error_event,
)
from backend.llm.errors import classify_llm_error
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.web_tools import WebFetchTool


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


@pytest.mark.asyncio
async def test_web_fetch_keeps_fetched_artifact_when_model_extraction_fails(tmp_path) -> None:
    tool = WebFetchTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
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
    assert result.provider == "custom_anthropic"
    assert result.provider_error_type == "protocol"
    assert result.error_kind == "provider_protocol"
    assert result.user_summary == "网页已抓取，但模型提取失败。"
    assert "HTTP 400" in (result.developer_detail or "")
    assert "gateway rejected extraction request" in (result.developer_detail or "")
